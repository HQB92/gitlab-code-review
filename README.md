# gitlab-code-review

A Claude plugin that automates GitLab MR code reviews triggered from Slack.

## How it works

1. A dev posts `#CodeReview https://gitlab.com/org/project/-/merge_requests/42` in a Slack channel
2. Claude reads the channel, fetches the MR diff via GitLab API
3. Reviews the code for issues and posts **inline comments** directly on the MR
4. Replies with a **summary** in the Slack thread

## What it checks

- 🔴 **Critical**: TypeScript `any`, hardcoded secrets, potential bugs
- 🟡 **High**: `console.log` left in code, dead code, standards violations
- 🟢 **Frontend**: Hardcoded strings not wrapped in `t()` (i18n), missing translation keys

## Requirements

- Slack MCP connected in Claude
- A GitLab personal access token with `api` scope

## Setup

Export your GitLab token in your shell (optional — Claude will ask if not set):

```bash
export GITLAB_TOKEN=glpat-xxxx   # add to ~/.zshrc for permanent use
```

## Usage

In Claude, say:

```
revisa el canal #my-channel por nuevos code reviews
```

Or give an MR URL directly:

```
dale review a este MR: https://gitlab.com/org/project/-/merge_requests/42
```
