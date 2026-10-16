#!/bin/bash
# Headless runner for the Claude Code Bee consumer on macOS (launchd). Port of
# scripts/run-bee-process.ps1.
#
# Drains pending sentinels by running `claude -p '/process-bee-inbox runner-removes-sentinels'`
# from the repo root, so Claude picks up .claude/agents and .claude/commands. Cheap early-out when
# nothing is pending, so it is safe to run often; bee_sync.py also kickstarts it the moment it
# writes a sentinel.
#
# The agent deletes nothing. When it finishes a capture it writes <sentinel_dir>/_done/<id>.md,
# and this script removes each sentinel whose done marker is newer than the sentinel. A sentinel
# rewritten after its marker (the capture changed during the run) stays for the next pass.
#
# The agent reads untrusted transcripts, so Claude runs in dontAsk mode: anything not allowed
# below is denied. It may read inside the repo and the vault, run `ls`, `wc` and `date` (the shell
# is confined to those two folders), use Glob and Grep, and write only Markdown files directly
# inside the output folders named in .claude/bee-paths.local.json, the voice observations file,
# and the _done folder. Glob and Grep can only be allowed for every folder, so the home folder's
# dot-folders (.ssh, .aws, .config, ...), Library, Desktop, Documents and Downloads are denied.
#
# Reads ~/.config/bee-sync/config.json (shared with bee_sync.py). Keys used here:
#   vault_raw          path of the vault's raw Bee folder (vault root is derived by stripping
#                      raw_subpath from .claude/bee-paths.local.json); a leading ~/ is expanded
#   sentinel_dir       defaults to <repo>/.kiro/bee-inbox
#   claude_path_dirs   colon-separated dirs prepended to PATH so `claude` resolves
#   claude_model       optional --model for the headless session (e.g. "sonnet")
#   preflight_command  optional command that must exit 0 within 60 seconds before Claude runs
#                      (e.g. an auth freshness check); its first output line is logged and notified
#   timeout_seconds    watchdog for the Claude run (default 1800)
#   deny_dirs          optional list of vault-relative folders the agent may not read or write
#
# Exit code: 0 when the run finished or had nothing to do; otherwise the failing claude exit
# (124 = watchdog timeout) or 1 for a setup problem, so `bee-launchd.sh status` shows it.
# Setup problems and two failed runs in a row raise a macOS notification (at most one per kind
# every 4 hours; $BEE_NOTIFY_LOG, for tests only, writes them to a file instead).
#
# Logs: ~/.local/state/bee-sync/bee-process.log (last 500 lines) and bee-process.last.txt
# (full output of the most recent Claude run).

set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd -P)"
CONFIG="${BEE_SYNC_CONFIG:-$HOME/.config/bee-sync/config.json}"
STATE="${BEE_STATE_DIR:-$HOME/.local/state/bee-sync}"
LOG="$STATE/bee-process.log"
LAST="$STATE/bee-process.last.txt"
LOCK="$STATE/bee-process.lock"
FAILS="$STATE/bee-process.failures"
mkdir -p "$STATE"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }
trim() { [ -f "$LOG" ] && tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; }
finish() { trim; exit "$1"; }
cfg() { /usr/bin/plutil -extract "$1" raw -o - "$CONFIG" 2>/dev/null; }
cfg_path() { # a config path with a leading ~/ expanded and trailing slashes dropped
  local v; v="$(cfg "$1")"
  case "$v" in "~/"*) v="$HOME/${v#\~/}" ;; esac
  while [ "${v%/}" != "$v" ]; do v="${v%/}"; done
  printf '%s' "$v"
}
notify() { # kind message: at most one notification per kind every 4 hours
  local mark="$STATE/.notified-$1"
  [ -n "$(find "$mark" -mmin -240 2>/dev/null)" ] && return 0
  if [ -n "${BEE_NOTIFY_LOG:-}" ]; then
    printf '%s\t%s\n' "$1" "$2" >> "$BEE_NOTIFY_LOG"
  else
    /usr/bin/osascript -e 'on run argv' -e 'display notification (item 1 of argv) with title (item 2 of argv)' \
      -e 'end run' "$2" "Bee notes" >/dev/null 2>&1
  fi
  touch "$mark"
}
fatal() { log "FATAL $*"; notify fatal "Bee processing is stopped: $*"; finish 1; }

[ -f "$CONFIG" ] || fatal "config missing at $CONFIG"

SENTINELS="$(cfg_path sentinel_dir)"; SENTINELS="${SENTINELS:-$REPO/.kiro/bee-inbox}"
DONE="$SENTINELS/_done"
pending() { find "$SENTINELS" -maxdepth 1 -name '*.sentinel.md' -type f 2>/dev/null | wc -l | tr -d ' '; }
clear_done() { # remove each sentinel whose done marker is newer than it
  local s id
  for s in "$SENTINELS"/*.sentinel.md; do
    [ -f "$s" ] || continue
    id="$(basename "$s" .sentinel.md)"
    if [ -f "$DONE/$id.md" ] && [ "$DONE/$id.md" -nt "$s" ]; then
      rm -f "$s" && log "DONE $id: sentinel removed"
    fi
  done
}

clear_done
# Cheap early-out: nothing to do, do not spin up Claude.
if [ "$(pending)" -eq 0 ]; then log "SKIP no pending sentinels"; finish 0; fi

# Single instance: bee_sync.py may kickstart us while a scheduled run is in flight. A lock left
# behind by a crash is dropped once its PID no longer belongs to this script.
old="$(cat "$LOCK" 2>/dev/null)"
if [ -n "$old" ] && ! ps -p "$old" -o command= 2>/dev/null | grep -q 'run-bee-process'; then rm -f "$LOCK"; fi
if ! /usr/bin/shlock -f "$LOCK" -p $$; then log "SKIP another run holds $LOCK"; finish 0; fi
trap 'rm -f "$LOCK"' EXIT
mkdir -p "$DONE" || fatal "cannot create $DONE"

# Watchdog (macOS has no `timeout`). The command starts in its own process group because a
# `claude` launcher may run the real binary as a child process and ignore SIGALRM; on timeout the whole group
# gets TERM, then KILL after 10 seconds, and the supervisor exits 124. TERM/HUP/INT to the
# supervisor (or to this script, which passes them on) stop the group the same way.
WATCHDOG='use POSIX ":sys_wait_h";
my ($limit, $grace) = (shift @ARGV, 10);
my $pid = fork() // die "fork: $!";
if ($pid == 0) { setpgrp(0, 0); exec @ARGV or die "exec: $!" }
setpgrp($pid, $pid);
my ($st, $timed_out);
sub stop { kill "TERM", -$pid;
  for (1 .. $grace) { if (waitpid($pid, WNOHANG) == $pid) { $st = $?; last } sleep 1 }
  kill "KILL", -$pid }
$SIG{ALRM} = sub { $timed_out = 1; stop() };
$SIG{$_} = sub { stop(); exit 143 } for qw(TERM HUP INT);
alarm $limit;
while (!defined $st) { my $r = waitpid($pid, 0); if ($r == $pid) { $st = $? } elsif (!$!{EINTR}) { last } }
kill "KILL", -$pid if $timed_out;
exit 124 if $timed_out;
exit(defined $st ? (($st & 127) ? 128 + ($st & 127) : $st >> 8) : 125);'

EXTRA_PATH=""; set -f; OLDIFS="$IFS"; IFS=:
for d in $(cfg claude_path_dirs); do # each entry may start with ~/
  case "$d" in "~/"*) d="$HOME/${d#\~/}" ;; esac
  [ -n "$d" ] && EXTRA_PATH="${EXTRA_PATH:+$EXTRA_PATH:}$d"
done
IFS="$OLDIFS"; set +f
export PATH="${EXTRA_PATH:+$EXTRA_PATH:}$HOME/.local/bin:$HOME/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
command -v claude >/dev/null 2>&1 || fatal "claude not found on PATH ($PATH)"

PRE="$(cfg preflight_command)"
if [ -n "$PRE" ]; then
  PRE_OUT="$(/usr/bin/perl -e "$WATCHDOG" 60 /bin/bash -c "$PRE" < /dev/null 2>&1)"; prc=$?
  if [ $prc -ne 0 ]; then
    MSG="$(printf '%s' "$PRE_OUT" | head -n 1)"
    [ $prc -eq 124 ] && MSG="Preflight check timed out after 60 seconds."
    log "SKIP preflight failed: ${MSG:-exit $prc}; $(pending) sentinel(s) wait for the next run"
    notify blocked "$(pending) Bee capture(s) pending. ${MSG:-Preflight check failed.}"
    finish 0
  fi
fi

# Vault root = vault_raw minus raw_subpath (from the consumer's path map).
VAULT_RAW="$(cfg_path vault_raw)"
RAW_SUB="$(/usr/bin/plutil -extract raw_subpath raw -o - "$REPO/.claude/bee-paths.local.json" 2>/dev/null)"
RAW_SUB="${RAW_SUB%/}"
VAULT_ROOT="${VAULT_RAW%/"$RAW_SUB"}"
if [ -z "$RAW_SUB" ] || [ "$VAULT_ROOT" = "$VAULT_RAW" ] || [ ! -d "$VAULT_ROOT" ]; then
  fatal "cannot derive vault root (vault_raw=$VAULT_RAW raw_subpath=$RAW_SUB)"
fi

MODEL="$(cfg claude_model)"
TIMEOUT="$(cfg timeout_seconds)"; TIMEOUT="${TIMEOUT:-1800}"
# Permissions (see the header). Tools and shell commands go in ALLOWED; the read, write and deny
# rules are paths, which may contain spaces, so they go in a --settings JSON built from the path
# map. A bare Read allow would reach every file on the Mac, so Read is scoped to the repo and the
# vault. Glob and Grep have no path scope: they are allowed bare, and the deny rules keep them
# (and Read) away from the sensitive parts of the home folder and the vault's deny_dirs.
ALLOWED='Glob,Grep,Agent,Task,Bash(ls:*),Bash(wc:*),Bash(date:*)'
RULES="$(/usr/bin/python3 -I - "$VAULT_ROOT" "$DONE" "$REPO/.claude/bee-paths.local.json" "$CONFIG" "$REPO" "$HOME" 2>&1 <<'PY'
import json, sys
vault, done, pathmap, config, repo, home = sys.argv[1:]
paths, cfg = json.load(open(pathmap)), json.load(open(config))
def rule(p):  # "//" marks an absolute path in a permission rule
    if any(c in p for c in "*?[]!#\\"):
        sys.exit("path has a character permission rules cannot express: " + p)
    return "//" + p.strip("/")
def vpath(rel):
    return vault.rstrip("/") + "/" + rel.strip("/")
allow = ["Read(%s/**)" % rule(vault), "Read(%s/**)" % rule(repo)]
allow += ["Edit(%s/*.md)" % rule(vpath(paths[k])) for k in ("tasks_dir", "meeting_notes_dir", "people_dir") if paths.get(k)]
if paths.get("voice_observations_file"):
    allow.append("Edit(%s)" % rule(vpath(paths["voice_observations_file"])))
allow.append("Edit(%s/*.md)" % rule(done))
deny = []
for rel in (".*", ".*/**", "Library/**", "Desktop/**", "Documents/**", "Downloads/**"):
    deny.append("Read(%s/%s)" % (rule(home), rel))
for where, what in ((vault, "the vault"), (repo, "the repo")):
    inside = where.rstrip("/")[len(home.rstrip("/")) + 1:] if where.startswith(home.rstrip("/") + "/") else ""
    if inside.split("/")[0] in ("Library", "Desktop", "Documents", "Downloads") or inside.startswith("."):
        sys.exit("%s is at %s, inside a folder of the home folder that the agent is denied" % (what, where))
for d in cfg.get("deny_dirs") or []:
    deny += ["Read(%s/**)" % rule(vpath(d)), "Edit(%s/**)" % rule(vpath(d))]
print(json.dumps({"permissions": {"allow": allow, "deny": deny}}))
PY
)" || fatal "cannot build permission rules: $(printf '%s' "$RULES" | tail -n 1)"

cd "$REPO" || fatal "cannot cd to $REPO"
rc=0
for pass in 1 2 3; do
  START_MARK="$STATE/.pass-start"; touch "$START_MARK"
  log "START pass $pass: $(pending) sentinel(s) pending; claude -p /process-bee-inbox"
  /usr/bin/perl -e "$WATCHDOG" "$TIMEOUT" \
    claude -p '/process-bee-inbox runner-removes-sentinels' \
      --permission-mode dontAsk \
      --settings "$RULES" \
      --add-dir "$VAULT_ROOT" \
      --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
      --allowedTools "$ALLOWED" \
      ${MODEL:+--model "$MODEL"} \
      < /dev/null > "$LAST" 2>&1 &
  SUP=$!
  # launchd stops a job by signalling this script, so pass the signal on to the supervisor and
  # wait while it stops Claude.
  trap 'STOPPED=1; kill -TERM "$SUP" 2>/dev/null' TERM HUP INT
  wait "$SUP"; rc=$?
  while kill -0 "$SUP" 2>/dev/null; do wait "$SUP"; rc=$?; done
  trap - TERM HUP INT
  clear_done
  if [ -n "${STOPPED:-}" ]; then
    log "STOPPED by a signal during pass $pass; $(pending) sentinel(s) remaining"; finish 143
  fi
  if [ $rc -ne 0 ]; then
    log "FAIL claude exit=$rc (124 = watchdog timeout after ${TIMEOUT}s); $(pending) sentinel(s) remaining; see $LAST"
    tail -n 5 "$LAST" | sed 's/^/  | /' >> "$LOG"
    break
  fi
  log "OK claude run complete; $(pending) sentinel(s) remaining"
  log "  summary: $(tail -n 3 "$LAST" | tr '\n' ' ' | cut -c1-300)"
  # Another pass only if new sentinels landed during this run (partials legitimately stay).
  [ -n "$(find "$SENTINELS" -maxdepth 1 -name '*.sentinel.md' -newer "$START_MARK" 2>/dev/null)" ] || break
done

if [ $rc -ne 0 ]; then
  n="$(cat "$FAILS" 2>/dev/null)"; case "$n" in ''|*[!0-9]*) n=0 ;; esac
  n=$((n + 1)); echo "$n" > "$FAILS"
  [ "$n" -ge 2 ] && notify failed "Bee processing failed $n runs in a row (last exit $rc). See bee-process.log."
else
  rm -f "$FAILS"
fi
finish $rc
