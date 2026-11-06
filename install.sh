#!/usr/bin/env bash
# Point this machine's Claude Code at a VB AI Hub: writes the hook config, installs the hooks, backfills sessions.
# Settings > Connect Claude in the hub shows this as one command. From a copy of the repo:
#   HUB_URL=http://hub:8787 HUB_TOKEN=... ./install.sh [--no-backfill]
set -euo pipefail
URL="${HUB_URL:?set HUB_URL, e.g. http://hub:8787}"
URL="${URL%/}"
SCRIPT="${BASH_SOURCE[0]:-}"
if [[ -f "$SCRIPT" ]]; then
  HOOK="$(cd "$(dirname "$SCRIPT")" && pwd)/hook.py"
else  # piped straight from the hub, so fetch the hook from it, whatever folder this runs in
  HOOK="$HOME/.local/share/vb-ai-hub/hook.py"
  mkdir -p "$(dirname "$HOOK")"
  curl -fsS -H "Authorization: Bearer ${HUB_TOKEN:-}" -o "$HOOK" "$URL/connect/hook.py"
fi
mkdir -p ~/.config/vb-ai-hub
python3 - "$URL" "${HUB_TOKEN:-}" <<'PY'
import json, sys, pathlib
p = pathlib.Path.home() / ".config/vb-ai-hub/config.json"
p.write_text(json.dumps({"url": sys.argv[1], "token": sys.argv[2]}, indent=2))
p.chmod(0o600)
PY
# Merge hooks into ~/.claude/settings.json (backs up the original and replaces earlier entries).
python3 - "$HOOK" <<'PY'
import json, shutil, sys, pathlib, time
hook = sys.argv[1]
p = pathlib.Path.home() / ".claude/settings.json"
p.parent.mkdir(parents=True, exist_ok=True)
s = json.loads(p.read_text()) if p.exists() else {}
if p.exists():
    shutil.copy(p, p.with_name(f"settings.json.bak-{int(time.time())}"))
cmd = f"python3 {json.dumps(hook)}"
hooks = s.setdefault("hooks", {})
for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
    groups = hooks.setdefault(event, [])
    for g in groups:
        g["hooks"] = [h for h in g.get("hooks", []) if not any(x in h.get("command", "") for x in ("claude-kanban/hook.py", "vb-ai-hub/hook.py"))]
    groups[:] = [g for g in groups if g.get("hooks")]
    groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
p.write_text(json.dumps(s, indent=2) + "\n")
print(f"Hooks installed in {p}")
PY
curl -fsS -o /dev/null "$URL/healthz" && echo "Hub reachable at $URL"
[[ "${1:-}" == "--no-backfill" ]] || python3 "$HOOK" --backfill | tail -1
