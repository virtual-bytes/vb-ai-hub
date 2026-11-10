#!/usr/bin/env bash
# Point this machine's Cursor at a VB AI Hub: writes the hook config and installs the hooks in ~/.cursor/hooks.json.
# Settings > Connect tools in the hub shows this as one command. From a copy of the repo:
#   HUB_URL=http://hub:8787 HUB_TOKEN=... ./install-cursor.sh [--no-backfill]
set -euo pipefail
URL="${HUB_URL:?set HUB_URL, e.g. http://hub:8787}"
URL="${URL%/}"
case "$URL" in
  http://localhost*|http://127.0.0.1*|http://\[::1\]*|https://*) ;;
  *) echo "Warning: $URL isn't https, so the access token and your sessions cross the network unencrypted." >&2 ;;
esac
SCRIPT="${BASH_SOURCE[0]:-}"
if [[ -f "$SCRIPT" ]]; then
  CURSOR="$(cd "$(dirname "$SCRIPT")" && pwd)/cursor.py"
else  # piped straight from the hub, so fetch the hook from it, whatever folder this runs in
  CURSOR="$HOME/.local/share/vb-ai-hub/cursor.py"
  mkdir -p "$(dirname "$CURSOR")"
  curl -fsS -H "Authorization: Bearer ${HUB_TOKEN:-}" -o "$CURSOR.new" "$URL/connect/cursor.py"
  if [[ -n "${HUB_CURSOR_SHA256:-}" ]]; then  # the Connect tools page puts the expected checksum in the command
    got="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$CURSOR.new")"
    if [[ "$got" != "$HUB_CURSOR_SHA256" ]]; then
      rm -f "$CURSOR.new"
      echo "cursor.py from $URL doesn't match the checksum on the Connect tools page; nothing was installed." >&2
      exit 1
    fi
  fi
  mv "$CURSOR.new" "$CURSOR"
fi
mkdir -p ~/.config/vb-ai-hub
python3 - "$URL" "${HUB_TOKEN:-}" <<'PY'
import json, sys, pathlib
p = pathlib.Path.home() / ".config/vb-ai-hub/config.json"
p.write_text(json.dumps({"url": sys.argv[1], "token": sys.argv[2]}, indent=2))
p.chmod(0o600)
PY
# Merge hooks into ~/.cursor/hooks.json (backs up the original and replaces earlier entries).
python3 - "$CURSOR" <<'PY'
import json, shutil, sys, pathlib, time
cursor = sys.argv[1]
p = pathlib.Path.home() / ".cursor/hooks.json"
p.parent.mkdir(parents=True, exist_ok=True)
s = json.loads(p.read_text()) if p.exists() else {}
if p.exists():
    shutil.copy(p, p.with_name(f"hooks.json.bak-{int(time.time())}"))
s["version"] = 1
cmd = f"python3 {json.dumps(cursor)}"
hooks = s.setdefault("hooks", {})
for event in ("sessionStart", "beforeSubmitPrompt", "stop", "sessionEnd"):
    entries = hooks.setdefault(event, [])
    entries[:] = [h for h in entries if "vb-ai-hub/cursor.py" not in h.get("command", "")]
    entries.append({"command": cmd, "timeout": 5})
p.write_text(json.dumps(s, indent=2) + "\n")
print(f"Hooks installed in {p}")
PY
curl -fsS -o /dev/null "$URL/healthz" && echo "Hub reachable at $URL"
[[ "${1:-}" == "--no-backfill" ]] || python3 "$CURSOR" --backfill | tail -1
