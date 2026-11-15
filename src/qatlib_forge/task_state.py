"""Task-card status observations; no Unit/Block completion or legacy role routing."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

SHA = re.compile(r"[0-9a-f]{40}\Z")
STATES = frozenset({"working", "partial-solved", "complete-solved"})
STATE_LABELS = {"working": "进行中", "partial-solved": "部分求解", "complete-solved": "完全求解"}
MARKER = re.compile(r"^<!-- qatlas-task (\{[^\n]*\}) -->$")
STATUS_LINE = re.compile(r"任务状态：([^（()]{1,20})（([a-z][a-z-]*)）[ \t]*")


def marker(head: str, state: str) -> str:
    if not isinstance(head, str) or not SHA.fullmatch(head) or not isinstance(state, str) or state not in STATES:
        raise ValueError("expected an exact head and a v2 task-card state")
    return "<!-- qatlas-task " + json.dumps({"head": head, "state": state}, sort_keys=True) + " -->"


def status_line(state: str) -> str:
    if not isinstance(state, str) or state not in STATES:
        raise ValueError("unsupported v2 task-card state")
    return f"任务状态：{STATE_LABELS[state]}（{state}）"


def _visible_controls(body: str) -> list[tuple[int, dict]]:
    """Read visible one-line controls; examples and nested comments are inert."""
    if not isinstance(body, str):
        raise ValueError("task body must be text")
    result, fence, comment = [], None, False
    for position, line in enumerate(body.splitlines()):
        if fence:
            end = re.match(r"^ {0,3}(`{3,}|~{3,})\s*$", line)
            if end and end[1][0] == fence[0] and len(end[1]) >= fence[1]:
                fence = None
            continue
        if comment:
            if "-->" in line:
                comment = False
            continue
        start = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if start:
            fence = (start[1][0], len(start[1]))
            continue
        if line.startswith("<!-- qatlas-task "):
            match = MARKER.fullmatch(line)
            if not match:
                raise ValueError("malformed task control")
            value = json.loads(match[1])
            if (not isinstance(value, dict) or set(value) != {"head", "state"}
                    or not isinstance(value['head'], str) or not SHA.fullmatch(value['head'])
                    or not isinstance(value['state'], str) or value['state'] not in STATES):
                raise ValueError("unsupported task control; legacy controls require manual migration")
            result.append((position, value))
        elif "<!--" in line:
            comment = "-->" not in line.split("<!--", 1)[1]
    return result


def controls(body: str) -> list[dict]:
    return [value for _, value in _visible_controls(body)]


def classify(snapshot: dict) -> dict:
    """Observe only the current card body, never old comments as a status queue."""
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get('head'), str)
            or not SHA.fullmatch(snapshot['head']) or type(snapshot.get('draft')) is not bool
            or not isinstance(snapshot.get('body'), str)):
        raise ValueError("expected head, draft and task-card body")
    base = {'head': snapshot['head'], 'state': None, 'route': 'manual-review',
            'draft': snapshot['draft'], 'mathematical_acceptance': False}
    try:
        values = controls(snapshot['body'])
    except (ValueError, TypeError) as error:
        return {**base, 'reason': str(error)}
    current = [v['state'] for v in values if v['head'] == snapshot['head']]
    if len(current) != 1:
        return {**base, 'reason': 'missing-or-conflicting-current-card-control'}
    state = current[0]
    first = snapshot['body'].splitlines()[0] if snapshot['body'].splitlines() else ''
    display = STATUS_LINE.fullmatch(first)
    if display and first != status_line(state):
        return {**base, 'reason': 'displayed-status-disagrees-with-control'}
    return {**base, 'state': state, 'route': 'wait' if state == 'working' else 'reviewer',
            'reason': 'current-task-card-status', 'stale_controls': len(values) - 1}


def replace_control(body: str, head: str, state: str) -> str:
    """Explicitly replace the card summary, preserving all non-control text."""
    control = marker(head, state)
    # Refuse legacy/malformed controls instead of silently dropping history.
    values = _visible_controls(body)
    if len(values) > 1:
        raise ValueError('multiple controls require manual reconciliation')
    lines = body.splitlines()
    if values:
        lines.pop(values[0][0])
    if lines and STATUS_LINE.fullmatch(lines[0]):
        lines.pop(0)
    return status_line(state) + '\n' + control + '\n' + '\n'.join(lines).lstrip('\n')


def read_status(body: str) -> list[dict]:
    """Return visible task-card controls without interpreting stale history."""
    return controls(body)


def write_status(body: str, head: str, state: str) -> str:
    """Replace the current task-card control and display line."""
    return replace_control(body, head, state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(classify(json.loads(args.snapshot.read_text())), ensure_ascii=False, indent=2))
    except (OSError, ValueError, TypeError) as error:
        print(f'task_state: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
