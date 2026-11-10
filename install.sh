#!/usr/bin/env bash
# Point this machine's Claude Code at a VB AI Hub: writes the hook config, installs the hooks, backfills sessions.
# Settings > Connect Claude in the hub shows this as one command. From a copy of the repo:
#   HUB_URL=http://hub:8787 HUB_TOKEN=... ./install.sh [--no-backfill]
set -euo pipefail
URL="${HUB_URL:?set HUB_URL, e.g. http://hub:8787}"
URL="${URL%/}"
case "$URL" in
  http://localhost*|http://127.0.0.1*|http://\[::1\]*|https://*) ;;
  *) echo "Warning: $URL isn't https, so the access token and your sessions cross the network unencrypted." >&2 ;;
esac
SCRIPT="${BASH_SOURCE[0]:-}"
if [[ -f "$SCRIPT" ]]; then
  HOOK="$(cd "$(dirname "$SCRIPT")" && pwd)/hook.py"
else  # piped straight from the hub, so fetch the hook from it, whatever folder this runs in
  HOOK="$HOME/.local/share/vb-ai-hub/hook.py"
  mkdir -p "$(dirname "$HOOK")"
  # The token reaches curl on its standard input, so other users on this machine never see it in the process list.
  printf 'header = "Authorization: Bearer %s"\n' "${HUB_TOKEN:-}" | curl -fsS -K - -o "$HOOK.new" "$URL/connect/hook.py"
  if [[ -n "${HUB_HOOK_SHA256:-}" ]]; then  # the Connect tools page puts the expected checksum in the command
    got="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$HOOK.new")"
    if [[ "$got" != "$HUB_HOOK_SHA256" ]]; then
      rm -f "$HOOK.new"
      echo "hook.py from $URL doesn't match the checksum on the Connect tools page; nothing was installed." >&2
      exit 1
    fi
  fi
  mv "$HOOK.new" "$HOOK"
fi
mkdir -p ~/.config/vb-ai-hub
HUB_TOKEN="${HUB_TOKEN:-}" python3 - "$URL" <<'PY'
import json, os, sys, pathlib
p = pathlib.Path.home() / ".config/vb-ai-hub/config.json"
p.touch(mode=0o600)
p.chmod(0o600)
p.write_text(json.dumps({"url": sys.argv[1], "token": os.environ["HUB_TOKEN"]}, indent=2))
PY
# Merge hooks into ~/.claude/settings.json (backs up the original and replaces earlier entries).
python3 - "$HOOK" <<'PY'
import json, shlex, shutil, sys, pathlib, time
hook = sys.argv[1]
p = pathlib.Path.home() / ".claude/settings.json"
p.parent.mkdir(parents=True, exist_ok=True)
s = json.loads(p.read_text()) if p.exists() else {}
if p.exists():
    shutil.copy(p, p.with_name(f"settings.json.bak-{int(time.time())}"))
cmd = f"python3 {shlex.quote(hook)}"  # the command runs in a shell, so the path is quoted for one
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
