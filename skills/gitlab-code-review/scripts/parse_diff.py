"""
parse_diff.py

Parses unified diff text from GitLab MR diffs API and returns
a structured list of added/changed lines with their line numbers.

Usage:
    python parse_diff.py --diff '...'
    python parse_diff.py --file diff.json

Output JSON:
[
  {
    "file": "src/components/Login.tsx",
    "is_frontend": true,
    "lines": [
      { "line_no": 42, "content": "+  console.log('user', user)", "type": "added" }
    ]
  }
]
"""

import json
import re
import sys
import argparse

FRONTEND_EXTENSIONS = {'.tsx', '.jsx', '.vue', '.svelte', '.ts', '.js'}
FRONTEND_DIRS = {'components', 'pages', 'views', 'screens', 'layouts', 'ui'}


def is_frontend_file(path: str) -> bool:
    ext = '.' + path.rsplit('.', 1)[-1] if '.' in path else ''
    if ext in FRONTEND_EXTENSIONS:
        # Be more specific: check if it's in a frontend dir or has JSX extension
        if ext in {'.tsx', '.jsx', '.vue', '.svelte'}:
            return True
        # For .ts/.js, check if it's in a frontend directory
        parts = path.lower().split('/')
        return any(d in parts for d in FRONTEND_DIRS)
    return False


def parse_unified_diff(diff_text: str, file_path: str) -> list:
    """
    Parse unified diff and return list of added lines with line numbers.
    Only tracks new_line numbers (right side of the diff).
    """
    lines = []
    new_line_no = 0

    for line in diff_text.split('\n'):
        # Hunk header: @@ -old_start,old_count +new_start,new_count @@
        hunk_match = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
        if hunk_match:
            new_line_no = int(hunk_match.group(1)) - 1
            continue

        if line.startswith('+++) ') or line.startswith('---'):
            # Diff header lines, skip
            continue

        if line.startswith('+++'):
            continue

        if line.startswith('+'):
            new_line_no += 1
            lines.append({
                'line_no': new_line_no,
                'content': line,
                'type': 'added'
            })
        elif line.startswith('-'):
            # Deleted line — don't increment new_line_no
            lines.append({
                'line_no': None,
                'content': line,
                'type': 'deleted'
            })
        else:
            # Context line
            new_line_no += 1

    return lines


def process_diffs(diffs: list) -> list:
    """Process a list of GitLab diff objects."""
    result = []
    for diff_entry in diffs:
        path = diff_entry.get('new_path') or diff_entry.get('old_path', '')
        diff_text = diff_entry.get('diff', '')

        if diff_entry.get('deleted_file'):
            continue  # Skip deleted files

        parsed_lines = parse_unified_diff(diff_text, path)
        added_lines = [l for l in parsed_lines if l['type'] == 'added']

        if not added_lines:
            continue

        result.append({
            'file': path,
            'is_frontend': is_frontend_file(path),
            'is_new_file': diff_entry.get('new_file', False),
            'is_renamed': diff_entry.get('renamed_file', False),
            'lines': added_lines,
            'all_lines': parsed_lines  # includes deleted for context
        })

    return result


def main():
    parser = argparse.ArgumentParser(description='Parse GitLab MR diffs')
    parser.add_argument('--diff', help='Raw diff JSON string')
    parser.add_argument('--file', help='Path to JSON file with diffs array')
    parser.add_argument('--output', help='Output file path (default: stdout)')
    args = parser.parse_args()

    if args.file:
        with open(args.file) as f:
            diffs = json.load(f)
    elif args.diff:
        diffs = json.loads(args.diff)
    else:
        # Read from stdin
        diffs = json.load(sys.stdin)

    result = process_diffs(diffs)

    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
    else:
        print(output)


if __name__ == '__main__':
    main()
