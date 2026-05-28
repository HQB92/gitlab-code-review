"""
review_mr.py

Fetches a GitLab MR diff and runs static pattern checks on the added lines.
Returns structured findings that Claude then uses to post inline comments.

Usage:
    python review_mr.py \
        --mr-url https://gitlab.com/myorg/myproject/-/merge_requests/42 \
        --token glpat-xxxx \
        --output /tmp/findings.json

Output JSON:
{
  "mr": { "title": "...", "url": "...", "author": "..." },
  "diff_refs": { "base_sha": "...", "head_sha": "...", "start_sha": "..." },
  "findings": [
    {
      "file": "src/Login.tsx",
      "line_no": 18,
      "severity": "critical|high|frontend",
      "category": "typing|console_log|bug|i18n|dead_code|security|standards",
      "message": "Human-readable description",
      "snippet": "the offending line content"
    }
  ],
  "summary": { "critical": 2, "high": 3, "frontend": 1 }
}
"""

import json
import os
import re
import sys
import argparse
import urllib.request
import urllib.parse
import urllib.error
from parse_diff import process_diffs


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

CONSOLE_PATTERN = re.compile(
    r'console\.(log|debug|warn|info|dir|table)\s*\(',
)

# Matches `any` used as type annotation (not as variable name)
IMPLICIT_ANY_PATTERNS = [
    re.compile(r':\s*any\b'),           # : any
    re.compile(r'as\s+any\b'),          # as any
    re.compile(r'\(\s*any\s*\)'),       # (any)
    re.compile(r'Array<any>'),          # Array<any>
    re.compile(r'<any>'),               # generics
]

# Detect common bug patterns
BUG_PATTERNS = [
    (re.compile(r'\.forEach\s*\([^)]*\)\s*\{[^}]*return\b'), 'forEach con return (usar .map o .filter)'),
    (re.compile(r'==\s*null\b|==\s*undefined\b'), 'Usar === en lugar de == para comparar null/undefined'),
    (re.compile(r'await\s+[a-zA-Z_$][a-zA-Z0-9_$]*\s*\((?![^)]*await)'), None),  # async pattern, needs context
]

# Hardcoded credentials
SECRET_PATTERNS = [
    re.compile(r'(password|passwd|secret|api_key|apikey|token|auth)\s*[=:]\s*["\'][^"\']{6,}["\']', re.IGNORECASE),
    re.compile(r'glpat-[a-zA-Z0-9_-]{20,}'),  # GitLab PAT
    re.compile(r'ghp_[a-zA-Z0-9]{36}'),        # GitHub PAT
]

# i18n: hardcoded user-visible strings in JSX/TSX
I18N_JSX_TEXT = re.compile(
    r'>\s*[A-ZÀ-ÿa-z][^<>{}\n]{3,}[a-zA-ZÀ-ÿ]\s*<'  # text between tags
)
I18N_ATTR_PATTERNS = [
    re.compile(r'placeholder\s*=\s*"[^"]{3,}"'),
    re.compile(r'label\s*=\s*"[^"]{3,}"'),
    re.compile(r'title\s*=\s*"[^"]{3,}"'),
    re.compile(r'aria-label\s*=\s*"[^"]{3,}"'),
    re.compile(r'tooltip\s*=\s*"[^"]{3,}"'),
]
I18N_TOAST_PATTERN = re.compile(
    r'(toast|notify|notification|alert|message)\s*[\.(]\s*\w+\s*\(\s*"[^"]{3,}"'
)

# Strings that are NOT i18n violations (technical, not user-visible)
I18N_IGNORE_PATTERN = re.compile(
    r'^(https?://|#|\.|\w+-\w+|[a-z]+_[a-z]+|test|data-|aria-|className|id=)',
    re.IGNORECASE
)


def check_line(line_content: str, is_frontend: bool) -> list:
    """Run all checks on a single added line. Returns list of findings."""
    findings = []
    # Strip the leading '+' from diff format
    code = line_content[1:] if line_content.startswith('+') else line_content
    stripped = code.strip()

    # Skip blank lines and comments
    if not stripped or stripped.startswith('//') or stripped.startswith('*') or stripped.startswith('#'):
        return findings

    # --- console.log ---
    if CONSOLE_PATTERN.search(code):
        findings.append({
            'severity': 'high',
            'category': 'console_log',
            'message': f'`console.{CONSOLE_PATTERN.search(code).group(1)}` detectado — remover antes de mergear',
            'snippet': stripped
        })

    # --- TypeScript any ---
    for pattern in IMPLICIT_ANY_PATTERNS:
        if pattern.search(code):
            findings.append({
                'severity': 'critical',
                'category': 'typing',
                'message': 'Uso de `any` detectado. Definir un tipo específico mejora la seguridad del código.',
                'snippet': stripped
            })
            break  # One finding per line for this category

    # --- Hardcoded secrets ---
    for pattern in SECRET_PATTERNS:
        match = pattern.search(code)
        if match:
            findings.append({
                'severity': 'critical',
                'category': 'security',
                'message': 'Posible credencial o token hardcodeado. Usar variables de entorno.',
                'snippet': stripped[:80] + '...' if len(stripped) > 80 else stripped
            })
            break

    # --- Frontend-specific checks ---
    if is_frontend:
        # i18n: hardcoded text in JSX
        if I18N_JSX_TEXT.search(code):
            match_text = I18N_JSX_TEXT.search(code).group(0)
            clean = match_text.strip('> <')
            if not I18N_IGNORE_PATTERN.match(clean) and len(clean) > 3:
                findings.append({
                    'severity': 'frontend',
                    'category': 'i18n',
                    'message': f'Texto hardcodeado en JSX: "{clean[:50]}". Usar clave de traducción con `t()`.',
                    'snippet': stripped
                })

        # i18n: hardcoded strings in attributes
        for pattern in I18N_ATTR_PATTERNS:
            if pattern.search(code):
                findings.append({
                    'severity': 'frontend',
                    'category': 'i18n',
                    'message': 'Atributo con string hardcodeado. Usar clave de traducción.',
                    'snippet': stripped
                })
                break

        # i18n: toast/notify with hardcoded string
        if I18N_TOAST_PATTERN.search(code):
            findings.append({
                'severity': 'frontend',
                'category': 'i18n',
                'message': 'Mensaje de notificación hardcodeado. Usar clave de traducción.',
                'snippet': stripped
            })

    return findings


def analyze_files(parsed_files: list) -> list:
    """Run checks on all files and return findings list."""
    all_findings = []
    for file_entry in parsed_files:
        file_path = file_entry['file']
        is_frontend = file_entry['is_frontend']

        for line in file_entry['lines']:
            if line['type'] != 'added' or line['line_no'] is None:
                continue
            line_findings = check_line(line['content'], is_frontend)
            for f in line_findings:
                f['file'] = file_path
                f['line_no'] = line['line_no']
                all_findings.append(f)

    return all_findings


def gitlab_get(url: str, token: str) -> dict:
    """Make a GET request to the GitLab API."""
    req = urllib.request.Request(url, headers={'PRIVATE-TOKEN': token})
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f'GitLab API error {e.code}: {body}')


def fetch_mr_data(mr_url: str, token: str) -> tuple:
    """
    Fetch MR metadata and all diff pages from GitLab.
    Returns (mr_metadata, diffs_list).
    """
    # Parse URL: https://gitlab.com/namespace/project/-/merge_requests/42
    match = re.match(
        r'(https?://[^/]+)/(.+?)/-/merge_requests/(\d+)',
        mr_url.rstrip('/')
    )
    if not match:
        raise ValueError(f'Invalid GitLab MR URL: {mr_url}')

    host = match.group(1)
    project_path = match.group(2)
    mr_iid = match.group(3)
    encoded_project = urllib.parse.quote(project_path, safe='')

    base = f'{host}/api/v4/projects/{encoded_project}'

    # Fetch MR metadata
    mr_data = gitlab_get(f'{base}/merge_requests/{mr_iid}', token)

    # Fetch diffs (paginated)
    all_diffs = []
    page = 1
    while True:
        url = f'{base}/merge_requests/{mr_iid}/diffs?per_page=50&page={page}'
        diffs = gitlab_get(url, token)
        if not diffs:
            break
        all_diffs.extend(diffs)
        if len(diffs) < 50:
            break
        page += 1
        if page > 10:  # Safety limit: max 500 files
            break

    return mr_data, all_diffs


def main():
    parser = argparse.ArgumentParser(description='Review a GitLab MR')
    parser.add_argument('--mr-url', required=True, help='Full GitLab MR URL')
    parser.add_argument('--token', help='GitLab personal access token (falls back to $GITLAB_TOKEN env var)')
    parser.add_argument('--output', help='Output JSON file path (default: stdout)')
    args = parser.parse_args()

    # Resolve token: CLI arg → env var → error
    token = args.token or os.environ.get('GITLAB_TOKEN', '').strip()
    if not token:
        print(
            'Error: GitLab token not found.\n'
            'Set it with: export GITLAB_TOKEN=glpat-xxxx  (in your .zshrc)\n'
            'Or pass it with: --token glpat-xxxx',
            file=sys.stderr
        )
        sys.exit(1)
    args.token = token

    print(f'Fetching MR data...', file=sys.stderr)
    mr_data, raw_diffs = fetch_mr_data(args.mr_url, args.token)

    print(f'Parsing {len(raw_diffs)} file diffs...', file=sys.stderr)
    parsed_files = process_diffs(raw_diffs)

    print(f'Analyzing code...', file=sys.stderr)
    findings = analyze_files(parsed_files)

    summary = {
        'critical': sum(1 for f in findings if f['severity'] == 'critical'),
        'high': sum(1 for f in findings if f['severity'] == 'high'),
        'frontend': sum(1 for f in findings if f['severity'] == 'frontend'),
    }

    diff_refs = mr_data.get('diff_refs', {})
    result = {
        'mr': {
            'title': mr_data.get('title'),
            'url': mr_data.get('web_url'),
            'author': mr_data.get('author', {}).get('name'),
            'iid': mr_data.get('iid'),
        },
        'diff_refs': {
            'base_sha': diff_refs.get('base_sha'),
            'head_sha': diff_refs.get('head_sha'),
            'start_sha': diff_refs.get('start_sha'),
        },
        'findings': findings,
        'summary': summary,
        'files_reviewed': len(parsed_files),
    }

    output_str = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, 'w') as f:
            f.write(output_str)
        print(f'Results written to {args.output}', file=sys.stderr)
    else:
        print(output_str)


if __name__ == '__main__':
    main()
