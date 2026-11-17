# Claude Code-configuratie

Deze map stuurt hoe Claude Code in dit project werkt.

## `settings.json`
- **permissions.allow** — git- en Python-commando's die Claude zonder vragen mag
  uitvoeren (branchen, committen, mergen, testen).
- **permissions.deny** — geblokkeerd: `git push` (jij beheert de remote),
  `git reset --hard` en `git clean` (vernietigen werk), en lezen van `.env` en
  `settings.json` (bevatten secrets/persoonlijke paden).
- **hooks.PreToolUse** — draait vóór elk Bash-commando de commit-guard hieronder.

## `hooks/block_commit_on_protected_branch.py`
Deterministische vangnet voor de projectafspraak "elke wijziging op een feature
branch". Weigert een `git commit` wanneer de huidige branch `main` of `dev` is
(exitcode 2), en laat commits op `feat/`·`fix/`·`chore/`-branches gewoon door.
Zo kan een commit op een beschermde branch niet per ongeluk gebeuren, ook niet
als de instructie in CLAUDE.md over het hoofd wordt gezien.

## Werkwijze per taak (samengevat)
```bash
git switch dev && git switch -c feat/<korte-naam>   # start
# ... werk + commits op de feature branch ...
git switch dev && git merge --no-ff feat/<korte-naam>
git branch -d feat/<korte-naam>
```
Pushen naar GitHub doe je zelf (`git push`).
