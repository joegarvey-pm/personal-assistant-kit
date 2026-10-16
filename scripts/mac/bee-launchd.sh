#!/bin/bash
# Install, remove, or inspect the macOS launchd jobs for the Bee pipeline.
#
#   bee-launchd.sh install [--vault-raw <dir>] [--sync-interval SEC] [--process-interval SEC]
#   bee-launchd.sh uninstall
#   bee-launchd.sh status
#
# Two per-user LaunchAgents (no admin needed):
#   com.personal-assistant-kit.bee-sync     bee_sync.py every --sync-interval (default 600s);
#                                           kickstarts the consumer when it writes sentinels
#   com.personal-assistant-kit.bee-process  run-bee-process.sh every --process-interval
#                                           (default 1800s), the safety net for anything the
#                                           kickstart missed (e.g. an expired login)
#
# launchd counts StartInterval only while the Mac is awake and does not replay runs missed
# during sleep, so after a wake the next sync comes within one interval. Nothing is lost:
# each sync fetches everything new since the last one.
# Keep the vault outside ~/Desktop, ~/Documents and ~/Downloads: macOS privacy controls
# (TCC) block background jobs there unless you grant access by hand.

set -u
HERE="$(cd "$(dirname "$0")" && pwd -P)"
REPO="$(cd "$HERE/../.." && pwd -P)"
CONFIG="${BEE_SYNC_CONFIG:-$HOME/.config/bee-sync/config.json}"
STATE="${BEE_STATE_DIR:-$HOME/.local/state/bee-sync}"
AGENTS="$HOME/Library/LaunchAgents"
SYNC_LABEL=com.personal-assistant-kit.bee-sync
PROC_LABEL=com.personal-assistant-kit.bee-process
DOMAIN="gui/$(id -u)"

plist() { # label interval program-args...
  local label="$1" interval="$2"; shift 2
  local args="" a
  for a in "$@"; do args="$args    <string>$a</string>
"; done
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
$args  </array>
  <key>StartInterval</key><integer>$interval</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key><string>$STATE/$label.out</string>
  <key>StandardErrorPath</key><string>$STATE/$label.out</string>
</dict>
</plist>
EOF
}

load() { # label file
  launchctl bootout "$DOMAIN/$1" 2>/dev/null
  launchctl bootstrap "$DOMAIN" "$2" && echo "loaded $1"
}

case "${1:-}" in
  install)
    shift
    VAULT_RAW=""; SYNC_INT=600; PROC_INT=1800
    while [ $# -gt 0 ]; do
      case "$1" in
        --vault-raw) VAULT_RAW="$2"; shift 2 ;;
        --sync-interval) SYNC_INT="$2"; shift 2 ;;
        --process-interval) PROC_INT="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
      esac
    done
    if [ -n "$VAULT_RAW" ]; then # expand a leading ~/ and resolve symlinks (for example a Desktop alias)
      case "$VAULT_RAW" in "~/"*) VAULT_RAW="$HOME/${VAULT_RAW#\~/}" ;; esac
      RAW_IN="$VAULT_RAW"
      VAULT_RAW="$(cd "$RAW_IN" 2>/dev/null && pwd -P)" || { echo "--vault-raw: no such folder: $RAW_IN" >&2; exit 2; }
    fi
    mkdir -p "$STATE" "$AGENTS" "$(dirname "$CONFIG")"
    if [ ! -f "$CONFIG" ]; then
      [ -n "$VAULT_RAW" ] || { echo "no config yet: pass --vault-raw <vault>/<raw folder>" >&2; exit 2; }
      /usr/bin/python3 -I - "$CONFIG" "$VAULT_RAW" "$REPO/.kiro/bee-inbox" "$PROC_LABEL" <<'PY'
import json, shutil, sys
path, raw, inbox, label = sys.argv[1:]
cfg = {"vault_raw": raw, "sentinel_dir": inbox, "bee_bin": shutil.which("bee") or "",
       "process_label": label, "sync_args": [], "settle_minutes": 10, "stuck_hours": 24}
json.dump(cfg, open(path, "w"), indent=2)
PY
      echo "wrote $CONFIG"
    fi
    [ -f "$REPO/.claude/bee-paths.local.json" ] || echo "WARN missing $REPO/.claude/bee-paths.local.json (copy the .example and fill it in)"
    chmod +x "$HERE/run-bee-process.sh" "$HERE/bee_sync.py" "$HERE/bee-launchd.sh"
    plist "$SYNC_LABEL" "$SYNC_INT" /usr/bin/python3 -I "$HERE/bee_sync.py" > "$AGENTS/$SYNC_LABEL.plist"
    plist "$PROC_LABEL" "$PROC_INT" /bin/bash "$HERE/run-bee-process.sh" > "$AGENTS/$PROC_LABEL.plist"
    plutil -lint "$AGENTS/$SYNC_LABEL.plist" "$AGENTS/$PROC_LABEL.plist" || exit 1
    load "$PROC_LABEL" "$AGENTS/$PROC_LABEL.plist"
    load "$SYNC_LABEL" "$AGENTS/$SYNC_LABEL.plist"
    ;;
  uninstall)
    for l in "$SYNC_LABEL" "$PROC_LABEL"; do
      launchctl bootout "$DOMAIN/$l" 2>/dev/null && echo "unloaded $l"
      rm -f "$AGENTS/$l.plist"
    done
    echo "config ($CONFIG) and state ($STATE) left in place"
    ;;
  status)
    for l in "$SYNC_LABEL" "$PROC_LABEL"; do
      echo "== $l"
      out="$(launchctl print "$DOMAIN/$l" 2>/dev/null)" || { echo "  not loaded"; continue; }
      printf '%s\n' "$out" | awk '/^\t(state|runs|last exit code|run interval) = /'
    done
    SENT="$(/usr/bin/plutil -extract sentinel_dir raw -o - "$CONFIG" 2>/dev/null)"
    case "$SENT" in "~/"*) SENT="$HOME/${SENT#\~/}" ;; esac
    while [ "${SENT%/}" != "$SENT" ]; do SENT="${SENT%/}"; done
    SENT="${SENT:-$REPO/.kiro/bee-inbox}"
    echo "== pending sentinels in $SENT: $(find "$SENT" -maxdepth 1 -name '*.sentinel.md' 2>/dev/null | wc -l | tr -d ' ')"
    OLDEST="$(ls -tr "$SENT"/*.sentinel.md 2>/dev/null | head -n 1)"
    [ -z "$OLDEST" ] || echo "   oldest: $(basename "$OLDEST" .sentinel.md), written $(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$OLDEST")"
    for f in bee-sync.log bee-process.log; do echo "== $f (last 5)"; tail -n 5 "$STATE/$f" 2>/dev/null; done
    ;;
  *) sed -n '2,8p' "$0"; exit 2 ;;
esac
