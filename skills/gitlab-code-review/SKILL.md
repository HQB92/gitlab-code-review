---
name: gitlab-code-review
description: >
  Automated code review skill triggered from Slack. Use this skill whenever
  someone asks to review a GitLab merge request, posts a #CodeReview message
  in Slack, or asks Claude to check a GitLab MR for bugs, typing, console.log,
  i18n, or code quality issues. Trigger on any message that contains a GitLab
  MR URL combined with a code review request, even if phrased casually like
  "can you check this MR", "review this merge request", or "dale review a esto".
  The skill reads the Slack channel for #CodeReview triggers, fetches the MR
  diff via GitLab API, reviews the code, posts inline comments on GitLab, and
  posts a summary reply in the Slack thread.
---

# GitLab Code Review via Slack

This skill automates code reviews triggered from a Slack channel. When a dev
posts `#CodeReview <gitlab-mr-url>`, Claude fetches the MR diff, analyzes it,
posts inline comments directly on GitLab, and replies with a summary in the
Slack thread.

## How to trigger this skill

In your Slack channel, post a message with this format:

```
#CodeReview https://gitlab.com/<namespace>/<project>/-/merge_requests/<iid>
```

Claude will detect this pattern and start the review automatically.

---

## Step 1 — Identify the Slack channel to monitor

Before doing anything, determine which channel to watch. There are three ways
this can be provided — check them in order:

1. **Already in the user's message** — e.g. "revisa #data-governance-devs" or
   a channel URL like `https://latam-ado.slack.com/archives/C09S65XTEDT`.
   Parse the channel name or ID directly from there.

2. **A GitLab MR URL was given directly** — the user bypassed Slack entirely
   (e.g. "dale review a este MR: https://..."). Skip to Step 2 to get the
   token, then jump to Step 3 to fetch the diff. No Slack channel needed.

3. **Neither was provided** — ask the user:

   > "¿De qué canal de Slack debo leer los mensajes de `#CodeReview`?
   > Puedes pasarme el nombre (ej. `#data-governance-devs`) o el ID del canal."

Once you have the channel, use `slack_search_channels` or `slack_read_channel`
to resolve the channel ID if only a name was given.

---

## Step 1b — Read the Slack trigger message

Use `slack_read_channel` with the resolved channel ID to fetch recent messages.
Look for messages matching the pattern:

```
#CodeReview <url>
```

Where `<url>` is a GitLab MR URL like:
`https://gitlab.com/<namespace>/<project>/-/merge_requests/<iid>`

Extract:
- The **full MR URL**
- The **Slack message timestamp** (`ts`) and **channel ID** — needed to reply in the thread
- The **Slack user** who requested the review

If no `#CodeReview` message is found, tell the user and show the expected format:
```
#CodeReview https://gitlab.com/<org>/<proyecto>/-/merge_requests/<número>
```

## Step 1c — Check if already reviewed (deduplication)

Before processing any `#CodeReview` message, check if the review was already
done by reading the thread of that message using `slack_read_thread` with the
message's `ts` and `channel`.

If the thread already contains a reply that starts with `✅ *Code Review completado*`,
**skip this message entirely** — it was already processed. Move on to the next
`#CodeReview` message if there are more, or finish if there are none.

Only proceed to Step 2 if the thread has **no** such reply yet. This prevents
duplicate reviews when multiple users or scheduled runs encounter the same
message.

---

## Step 2 — Get the GITLAB_TOKEN

Each developer uses their own GitLab personal access token. First, check if
it's already available in the shell environment — the user may have it exported
in their `.zshrc`, `.bashrc`, or equivalent:

```bash
echo $GITLAB_TOKEN
```

- **If the variable is set and non-empty**: use it silently. No need to ask.
- **If the variable is empty or not set**: ask the user:

> "Para hacer el review necesito tu GitLab token. Puedes exportarlo
> permanentemente en tu `.zshrc` con:
> `export GITLAB_TOKEN=glpat-xxxx`
> O pégalo aquí y lo uso solo para esta sesión.
> Puedes generarlo en GitLab → User Settings → Access Tokens (permisos `api`)."

Store it as `GITLAB_TOKEN` for all subsequent API calls in this session.
Never log, save, or expose this token.

---

## Step 3 — Fetch the MR metadata and diff

Parse the MR URL to extract:
- `gitlab_host` (e.g., `gitlab.com` or your self-hosted domain)
- `namespace/project` → URL-encode as the project ID (replace `/` with `%2F`)
- `mr_iid` (the merge request number)

### 3a. Get the authenticated user

Before fetching the MR, identify who is running this review:

```
GET https://<gitlab_host>/api/v4/user
Headers: PRIVATE-TOKEN: <GITLAB_TOKEN>
```

Save `id` and `username` as `reviewer_id` and `reviewer_username`.

### 3b. Get MR metadata and check authorship
```
GET https://<gitlab_host>/api/v4/projects/<encoded_project>/merge_requests/<iid>
Headers: PRIVATE-TOKEN: <GITLAB_TOKEN>
```

Save from the response:
- `title`, `description`, `author.name`, `author.id`
- `target_branch`
- `diff_refs.base_sha`, `diff_refs.head_sha`, `diff_refs.start_sha`
- `web_url`

**Author check**: if `author.id` matches `reviewer_id`, stop here and tell
the user — in Slack thread if available, otherwise directly in the conversation:

> "🚫 No puedes hacer code review de tu propio MR (`<MR title>`).
> Pide a un compañero del equipo que lo revise."

Do not proceed further.

**Branch filter**: if `target_branch` is not `develop`, stop here and reply
in the Slack thread:

> "⚠️ Este MR apunta a `<target_branch>`, no a `develop`. Solo proceso MRs
> que van hacia `develop`."

Do not proceed further for this MR.

### 3b. Get the file changes (diff)
```
GET https://<gitlab_host>/api/v4/projects/<encoded_project>/merge_requests/<iid>/diffs?per_page=50
Headers: PRIVATE-TOKEN: <GITLAB_TOKEN>
```

For large MRs (many files), paginate using `?page=2`, `?page=3`, etc.

Each diff entry contains:
- `new_path` / `old_path`: file path
- `diff`: the unified diff text
- `new_file`, `renamed_file`, `deleted_file`: boolean flags

Use the script at `scripts/parse_diff.py` to extract added/changed lines with
their line numbers from the raw diff text.

---

## Step 4 — Analyze the code

Review **only the added or modified lines** (lines starting with `+` in the
diff, excluding the `+++` header line). For deleted lines (starting with `-`),
note them for context but don't comment on them unless they reveal something
important about what was removed.

Run the analysis script:
```bash
python scripts/review_mr.py \
  --diff-file /tmp/mr_diff.json \
  --output /tmp/review_findings.json
```

Or perform the analysis directly using the criteria below.

### What to check

**🔴 Critical (always comment)**
- **TypeScript typing**: implicit `any`, missing return types on exported functions,
  untyped function parameters, `as any` casts without justification
- **Bugs**: null/undefined dereference without guards, wrong variable used,
  off-by-one errors, async/await misuse (missing `await`, unhandled promises),
  mutations of props or function arguments
- **Security**: hardcoded credentials, tokens, passwords, or API keys in code

**🟡 High (comment when found)**
- **`console.log` / `console.debug` / `console.warn`** left in production code
  (note: `console.error` in error handlers is usually fine)
- **Dead code**: commented-out blocks of more than 3 lines, unused imports,
  unreachable code

**🔵 Clean Code (comment when found)**
- **Nombres poco descriptivos**: variables como `data`, `temp`, `x`, `res`,
  funciones como `handleThing`, `doStuff`. Los nombres deben revelar intención.
- **Funciones largas**: más de 30 líneas haciendo múltiples cosas. Una función
  debe hacer una sola cosa.
- **Magic numbers**: números literales sin nombre (`if (status === 3)` →
  debería ser `if (status === STATUS.PENDING)`).
- **Nesting profundo**: más de 3 niveles de if/for anidados. Sugerir early
  returns o extracción de funciones.
- **DRY violations**: lógica duplicada que debería estar en una función o
  utilidad compartida.
- **Comentarios que explican el qué en lugar del por qué**: el código debe ser
  autoexplicativo; los comentarios deben explicar decisiones no obvias.

**🟣 SOLID (comment when found)**
- **S — Single Responsibility**: una clase o función que hace demasiadas cosas
  no relacionadas. Sugerir separar responsabilidades.
- **O — Open/Closed**: lógica con múltiples `if/else` o `switch` que crecería
  con cada nuevo caso. Sugerir polimorfismo o estrategia.
- **L — Liskov Substitution**: subclases o implementaciones que rompen el
  contrato del tipo base (lanzan excepciones inesperadas, ignoran parámetros,
  retornan tipos incompatibles).
- **I — Interface Segregation**: interfaces o tipos con demasiados métodos
  donde los implementadores solo usan algunos. Sugerir dividir la interfaz.
- **D — Dependency Inversion**: clases que instancian sus dependencias
  directamente (`new ServiceX()`) en lugar de recibirlas por parámetro o
  inyección. Dificulta el testing y el reemplazo.

**⚫ High Standards (comment when found)**
- **Error handling**: funciones async sin try/catch, errores silenciados con
  catch vacío (`catch (e) {}`), errores genéricos sin contexto útil.
- **Inmutabilidad**: mutaciones directas de objetos/arrays cuando debería
  usarse spread, `map`, `filter`, o `reduce`.
- **Separación de concerns**: lógica de negocio mezclada con lógica de UI o
  de acceso a datos en el mismo componente/función.
- **Manejo de estados de carga y error**: llamadas a APIs sin manejar el estado
  de loading ni el caso de error en la UI.
- **Tests**: si el MR agrega nueva lógica de negocio sin tests correspondientes,
  mencionarlo como recomendación.
- **Performance**: re-renders innecesarios en React (objetos/funciones creados
  inline en props sin `useMemo`/`useCallback`), loops dentro de renders,
  llamadas a API dentro de loops.

**🟢 Frontend-specific (when files are .tsx, .jsx, .vue, .svelte)**
- **i18n**: hardcoded user-visible strings not wrapped in `t()`, `i18n.t()`,
  `$t()`, `useTranslation`, or equivalent. Examples of violations:
  - `<p>Hello world</p>` → should be `<p>{t('hello_world')}</p>`
  - `placeholder="Enter name"` → should use a translation key
  - `toast.error("Something went wrong")` → should use i18n key
  Exception: strings that are purely technical (log messages, CSS class names,
  test IDs, etc.) don't need i18n.
- **Missing translation keys**: a `t('some.key')` call where the key doesn't
  appear in locale files (if locale files are in the diff)

---

## Step 5 — Post inline comments on GitLab

For each finding, post a **Discussion** (inline comment) on the MR using:

```
POST https://<gitlab_host>/api/v4/projects/<encoded_project>/merge_requests/<iid>/discussions
Headers:
  PRIVATE-TOKEN: <GITLAB_TOKEN>
  Content-Type: application/json

Body:
{
  "body": "<comment text>",
  "position": {
    "position_type": "text",
    "base_sha": "<diff_refs.base_sha>",
    "start_sha": "<diff_refs.start_sha>",
    "head_sha": "<diff_refs.head_sha>",
    "new_path": "<file path>",
    "old_path": "<file path>",
    "new_line": <line number in the new version of the file>
  }
}
```

**Comment format** — use emoji prefix for severity:
```
🔴 [Typing] Missing return type on exported function `fetchUsers`.
Add the return type: `Promise<User[]>`

🟡 [console.log] Remove before merging: `console.log('user data', userData)`

🟢 [i18n] Hardcoded string detectada: `"Guardar cambios"`.
Usar clave de traducción: `{t('common.save_changes')}`
```

If a finding spans multiple lines, anchor the comment to the first relevant line.

If the inline comment API returns an error (e.g., line not in diff), fall back
to posting a general MR note instead:
```
POST .../merge_requests/<iid>/notes
```

---

## Step 5b — Ask to approve if clean

After posting all inline comments, evaluate the findings:

- **If there are 🔴 Critical or 🟡 High findings** → do NOT ask to approve.
  The MR needs corrections first. Skip to Step 6.

- **If there are zero 🔴 Critical and zero 🟡 High findings** → ask the user:

  > "El código está limpio — sin issues críticos ni altos.
  > ¿Quieres que apruebe este MR?
  > Responde **sí** para aprobar o **no** para dejarlo pendiente."

  - **If the user says yes** → call the approve API:
    ```
    POST https://<gitlab_host>/api/v4/projects/<encoded_project>/merge_requests/<iid>/approve
    Headers:
      PRIVATE-TOKEN: <GITLAB_TOKEN>
      Content-Type: application/json
    ```
    If the API returns 403 ("author cannot approve"), inform the user:
    > "⚠️ No puedo aprobar tu propio MR — pide a un compañero que lo apruebe."

  - **If the user says no** → skip the approval, continue to Step 6.

Store the approval result (`approved: true/false/skipped`) for the Slack summary.

---

## Step 6 — Post summary in Slack

Reply to the original Slack message thread using `slack_send_message` with
`thread_ts` set to the original message's `ts`.

**Summary format:**

```
✅ *Code Review completado* — <MR title>
🔗 <MR URL>
👤 Revieweado por Claude en nombre de <requester>

*Hallazgos por categoría:*
🔴 Critical: X
🟡 High: X
🔵 Clean Code: X
🟣 SOLID: X
⚫ Standards: X
🟢 Frontend/i18n: X

<Si hay hallazgos críticos>
*Top issues:*
• `archivo.ts:42` — [Typing] Missing type on param `userId`
• `Component.tsx:18` — [i18n] Hardcoded: "Bienvenido"

<Si no hay hallazgos 🔴 ni 🟡 y el usuario aprobó>
Sin hallazgos críticos. ¡Buen trabajo! 🎉
✅ *MR aprobado — listo para merge.*

<Si no hay hallazgos 🔴 ni 🟡 y el usuario eligió no aprobar>
Sin hallazgos críticos. ¡Buen trabajo! 🎉
⏸ *Aprobación pendiente — el reviewer decidió no aprobar por ahora.*

<Si hay hallazgos 🔴 o 🟡>
❌ *MR NO aprobado — requiere correcciones antes de mergear.*

_Los comentarios están inline en el MR de GitLab._
```

---

## Step 7 — React to the original message

After posting the summary, add a ✅ reaction to the original Slack message
using `slack_add_reaction` to visually confirm the review was completed.

---

## Configuration reference

The skill is generic — it works in any Slack channel. Users configure it by:

1. Posting `#CodeReview <mr-url>` in their desired channel
2. Providing their personal `GITLAB_TOKEN` when prompted

No channel-specific configuration is needed. Each user's token is used only
for the duration of that review session.

---

## Error handling

| Situation | Action |
|-----------|--------|
| GitLab API 401 | Tell user the token is invalid or lacks `api` scope |
| GitLab API 403 | User doesn't have access to that project |
| GitLab API 404 | MR URL is wrong or MR was deleted |
| MR is too large (>100 files) | Review the first 50 files and note it in the Slack summary |
| Inline comment fails | Fall back to a general MR note with the file:line reference |
| Slack send fails | Output the summary directly in the Claude conversation |
