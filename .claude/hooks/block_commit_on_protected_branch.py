#!/usr/bin/env python3
"""
Claude Code PreToolUse-hook: blokkeert `git commit` op main of dev.

Afspraak: elke wijziging gebeurt op een feature branch (feat/ fix/ chore/).
Deze hook is de deterministische vangnet naast de instructie in CLAUDE.md.

De hook krijgt via stdin een JSON met het tool en het commando. Bij een
git-commit op main/dev geeft hij exitcode 2 (blokkeren) met uitleg; anders 0.
"""
import json
import re
import subprocess
import sys


def current_branch() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0   # geen bruikbare input: niet blokkeren

    tool = payload.get("tool_name") or payload.get("tool") or ""
    command = ""
    tool_input = payload.get("tool_input") or payload.get("input") or {}
    if isinstance(tool_input, dict):
        command = tool_input.get("command", "")

    # Alleen shell-commando's met een git commit interesseren ons.
    if "git" not in command or "commit" not in command:
        return 0
    # Laat 'git commit' binnen een feature branch ongemoeid.
    if not re.search(r"\bgit\b.*\bcommit\b", command):
        return 0

    branch = current_branch()
    if branch in ("main", "dev"):
        sys.stderr.write(
            f"\n⛔ Commit geweigerd: je staat op '{branch}'.\n"
            "Werk volgens de projectafspraak op een feature branch:\n"
            "    git switch dev && git switch -c feat/<korte-naam>\n"
            "en commit daar. Merge nadien met --no-ff terug naar dev.\n"
        )
        return 2   # exitcode 2 = blokkeer de tool-actie in Claude Code

    return 0


if __name__ == "__main__":
    sys.exit(main())
