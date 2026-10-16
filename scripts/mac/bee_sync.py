#!/usr/bin/env python3
"""Scheduled Bee sync for macOS (launchd). Port of bee-sync-scheduled.ps1 + bee-lib.ps1.

Runs `bee sync --only conversations` into the vault's raw folder, applies the completeness
gate (COMPLETED + real end_time + settled), and writes one sentinel per capture whose
content changed since the last sentinel fired for it. When it writes sentinels it
kickstarts the consumer launchd job so notes publish right away instead of waiting
for the consumer's own interval. The other Bee data (facts, todos, daily) syncs after
that as a best effort, so a failure there never holds back conversations.

Config:  ~/.config/bee-sync/config.json  (override with $BEE_SYNC_CONFIG)
State:   ~/.local/state/bee-sync/        (override with $BEE_STATE_DIR)
         seen-hashes.json    convId -> hash we last fired a sentinel for
         stuck-captures.json convId -> {reason, first_seen, raw_path}
         duplicate-captures.json  convId -> stale copies already warned about
         sync-failures, sync-partial  counts of failed / partial syncs in a row
         bee-sync.log        trimmed to the last 500 lines

Notifications (macOS, at most one per kind every 4 hours): a failing sync (at once when
the Bee login has expired, otherwise after 3 failures in a row), conversations that bee
sync skipped on 6 runs in a row, a sentinel waiting longer than stale_sentinel_minutes
(catches a stuck consumer), an unreadable hash cache, and a crash of this script.
$BEE_NOTIFY_LOG, for tests only, writes them to a file instead.

Standard library only, Python 3.9+ (macOS ships 3.9 with the Command Line Tools).

Usage:
  bee_sync.py                         normal scheduled run
  bee_sync.py --no-sync               skip `bee sync`, just re-scan the raw folder
  bee_sync.py --catch-up-since DATE   also fire sentinels for every ready capture dated DATE
                                      (YYYY-MM-DD) or later, even ones already fired; the
                                      consumer skips captures it has already done. On a
                                      first run, older captures are seeded silently.
  bee_sync.py --dry-run               report what would fire; write nothing (log lines go
                                      to stderr only, and no notification is sent)

A run with a flag, or started from a terminal, is a manual run: it waits for a scheduled run
to finish instead of skipping, echoes its log lines to stderr, and prints a summary.
"""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

HOME = os.path.expanduser("~")
CONFIG = os.environ.get("BEE_SYNC_CONFIG", os.path.join(HOME, ".config/bee-sync/config.json"))
STATE = os.environ.get("BEE_STATE_DIR", os.path.join(HOME, ".local/state/bee-sync"))
LOG = os.path.join(STATE, "bee-sync.log")
HASHES = os.path.join(STATE, "seen-hashes.json")
STUCK = os.path.join(STATE, "stuck-captures.json")
DUPES = os.path.join(STATE, "duplicate-captures.json")
SYNC_FAILS = os.path.join(STATE, "sync-failures")
SYNC_PARTIAL = os.path.join(STATE, "sync-partial")
LOCK = os.path.join(STATE, "bee-sync.lock")
NOTIFY_HOURS = 4
CORRUPT_CATCH_UP_DAYS = 2
PARTIAL_NOTIFY_RUNS = 6
SKIPPED_RE = re.compile(r"Warning: (\d+) conversation\(s\) failed to sync")
LOGIN_RE = re.compile(r"not logged in|unauthori[sz]ed|login", re.I)

DEFAULTS = {
    "settle_minutes": 10,
    "stuck_hours": 24,
    "sync_args": [],
    "aux_sync_targets": ["facts", "todos", "daily"],
    "sync_timeout_seconds": 900,
    "aux_sync_timeout_seconds": 300,
    "stale_sentinel_minutes": 120,
    "process_label": "",
}

DRY_RUN = False  # --dry-run: log to stderr only, send no notifications
ECHO = False     # manual run: also echo log lines to stderr


def now_local():
    return dt.datetime.now().astimezone()


def log(msg):
    line = "[%s] %s\n" % (now_local().strftime("%Y-%m-%d %H:%M:%S"), msg)
    if ECHO or DRY_RUN:
        sys.stderr.write(line)
    if DRY_RUN:
        return
    os.makedirs(STATE, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line)


def trim_log(keep=500):
    if DRY_RUN:
        return
    try:
        with open(LOG, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > keep:
            atomic_write(LOG, "".join(lines[-keep:]))
    except FileNotFoundError:
        pass


def atomic_write(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (ValueError, OSError):
        return None  # corrupt


def touch(path):
    with open(path, "a"):
        pass
    os.utime(path, None)


def mark_is_fresh(path, hours):
    try:
        return time.time() - os.path.getmtime(path) < hours * 3600
    except OSError:
        return False


def read_count(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def notify(kind, message):
    """macOS notification, at most one per kind every NOTIFY_HOURS hours."""
    if DRY_RUN:
        log("NOTIFY (dry run, not sent) %s" % message)
        return
    mark = os.path.join(STATE, ".notified-" + kind)
    if mark_is_fresh(mark, NOTIFY_HOURS):
        return
    test_log = os.environ.get("BEE_NOTIFY_LOG")
    if test_log:
        with open(test_log, "a", encoding="utf-8") as f:
            f.write("%s\t%s\n" % (kind, message))
    else:
        subprocess.run(["/usr/bin/osascript", "-e", "on run argv", "-e",
                        "display notification (item 1 of argv) with title (item 2 of argv)",
                        "-e", "end run", message, "Bee sync"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    touch(mark)


def load_config():
    """Load config; self-heal from the .bak copy if the config vanished (a dry run only reads it)."""
    bak = CONFIG + ".bak"
    path = CONFIG
    if not os.path.exists(CONFIG):
        if os.path.exists(bak) and DRY_RUN:
            path = bak
            log("config is missing; a real run would restore it from %s" % bak)
        elif os.path.exists(bak):
            shutil.copy2(bak, CONFIG)
            log("RECOVERED config was missing; restored from %s" % bak)
        else:
            log("FATAL config missing at %s and no backup. Run scripts/mac/bee-launchd.sh install --vault-raw <dir>." % CONFIG)
            notify("fatal", "Bee sync is stopped: its config is missing. See bee-sync.log.")
            sys.exit(1)
    cfg = load_json(path, None)
    if not isinstance(cfg, dict) or not cfg.get("vault_raw") or not cfg.get("sentinel_dir"):
        log("FATAL config at %s is unreadable or missing vault_raw/sentinel_dir" % path)
        notify("fatal", "Bee sync is stopped: its config is unreadable. See bee-sync.log.")
        sys.exit(1)
    if not DRY_RUN:
        try:
            shutil.copy2(CONFIG, bak)
        except OSError:
            pass
    merged = dict(DEFAULTS)
    merged.update(cfg)
    for k in ("vault_raw", "sentinel_dir", "bee_bin"):
        if merged.get(k):
            merged[k] = os.path.expanduser(merged[k]).rstrip("/") or "/"
    return merged


# ---------------------------------------------------------------- completeness gate

META_RE = re.compile(r"^\s*-\s*([A-Za-z_]+):\s*(.*)$")


def bee_metadata(path):
    """Parse the `- key: value` bullets above the first `## ` section (first value wins)."""
    meta = {}
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if re.match(r"^\s*##\s", line):
                    break
                m = META_RE.match(line)
                if m:
                    meta.setdefault(m.group(1).lower(), m.group(2).strip())
    except OSError:
        pass
    return meta


def bee_time(value):
    """Parse a Bee ISO-8601 timestamp to an aware UTC datetime, or None."""
    if not value or value.strip().lower() == "n/a":
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(v)
    except ValueError:
        # Normalize odd fractional-second widths, which 3.9's fromisoformat rejects
        m = re.match(r"^(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)$", v)
        if not m:
            return None
        frac = (m.group(2) or "0")[:6].ljust(6, "0")
        try:
            t = dt.datetime.fromisoformat("%s.%s%s" % (m.group(1), frac, m.group(3)))
        except ValueError:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


def capture_readiness(path, settle_minutes, stuck_hours, now=None):
    """Return (status, reason). Status: ready | capturing | unsettled | stuck | unknown."""
    now = now or dt.datetime.now(dt.timezone.utc)
    meta = bee_metadata(path)
    state = meta.get("state", "").upper()
    end, upd, start = bee_time(meta.get("end_time")), bee_time(meta.get("updated_at")), bee_time(meta.get("start_time"))

    if state == "COMPLETED":
        if not end:
            return "unsettled", "COMPLETED but no end_time yet"
        if upd and (now - upd).total_seconds() < settle_minutes * 60:
            mins = round((now - upd).total_seconds() / 60, 1)
            return "unsettled", "COMPLETED but updated %sm ago (< %sm settle window)" % (mins, settle_minutes)
        return "ready", "COMPLETED and settled"
    if state == "CAPTURING":
        if start and (now - start).total_seconds() >= stuck_hours * 3600:
            hrs = round((now - start).total_seconds() / 3600, 1)
            return "stuck", "CAPTURING for %sh (>= %sh) - likely abandoned" % (hrs, stuck_hours)
        return "capturing", "still CAPTURING"
    if start and (now - start).total_seconds() >= stuck_hours * 3600:
        return "stuck", "state=%r unresolved for >= %sh" % (state, stuck_hours)
    return "unknown", "unrecognized state %r" % state


# ---------------------------------------------------------------- sync + sentinels

def bee_command(cfg):
    bee = cfg.get("bee_bin") or shutil.which("bee")
    return bee if bee and os.path.exists(bee) else None


def stop_group(p, grace=10):
    """TERM bee's process group, KILL whatever is left after `grace` seconds, reap the leader."""
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        p.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.wait()


def run_bee(cfg, bee, target, timeout):
    """One `bee sync --only <target>`; returns (exit code or None on timeout, output).

    The npm launcher runs a native worker as its child, so bee gets its own process group
    and a timeout (or a signal to this script) stops the whole group. Output goes to a temp
    file, not a pipe, so a leftover worker cannot hold the read open."""
    cmd = [bee, "sync", "--output", cfg["vault_raw"], "--only", target] + list(cfg.get("sync_args") or [])
    # The npm `bee` launcher is `#!/usr/bin/env node`; node is installed beside it.
    env = dict(os.environ, PATH=os.path.dirname(os.path.realpath(bee)) + os.pathsep
               + os.path.dirname(bee) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"))
    with tempfile.TemporaryFile() as out:
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 env=env, start_new_session=True)
        except OSError as e:
            return 126, "cannot run %s: %s" % (bee, e)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            rc = None
            stop_group(p)
        except BaseException:
            stop_group(p)
            raise
        out.seek(0)
        return rc, out.read().decode("utf-8", errors="replace").strip()


def sync_failed(reason, out=""):
    log("FAIL bee sync %s" % reason)
    for line in out.splitlines()[-8:]:
        log("  | " + line)
    n = read_count(SYNC_FAILS) + 1
    atomic_write(SYNC_FAILS, str(n))
    login = LOGIN_RE.search(out)
    if login or n >= 3:
        notify("sync", "Bee sync has failed %d time(s) in a row%s. See bee-sync.log."
               % (n, "; run `bee login`" if login else ""))
    return False


def note_skipped(out):
    """bee sync exits 0 even when single conversations fail. It retries them on the next run,
    but after 6 days without a clean run every sync becomes a full resync, so count the runs
    in a row that skipped some, log at most hourly, and notify once it has lasted an hour."""
    lines = out.splitlines()
    hit = next((i for i, line in enumerate(lines) if SKIPPED_RE.search(line)), None)
    if hit is None:
        if os.path.exists(SYNC_PARTIAL):
            os.remove(SYNC_PARTIAL)
        return
    n = read_count(SYNC_PARTIAL) + 1
    atomic_write(SYNC_PARTIAL, str(n))
    mark = os.path.join(STATE, ".skipped-warned")
    if not mark_is_fresh(mark, 1):
        log("WARN bee sync skipped some conversations (%d run(s) in a row): %s" % (n, lines[hit].strip()))
        for line in lines[hit + 1:hit + 11]:
            log("  | " + line)
        touch(mark)
    if n >= PARTIAL_NOTIFY_RUNS:
        notify("partial", "Bee sync has skipped some conversations %d runs in a row. See bee-sync.log." % n)


def run_bee_sync(cfg):
    """Sync conversations, the data the gate needs. False stops this run before the scan."""
    bee = bee_command(cfg)
    if not bee:
        return sync_failed("cannot start: bee CLI not found (bee_bin=%r). Install with: npm install -g @beeai/cli"
                           % cfg.get("bee_bin"))
    rc, out = run_bee(cfg, bee, "conversations", cfg["sync_timeout_seconds"])
    if rc is None:
        return sync_failed("timed out after %ss" % cfg["sync_timeout_seconds"], out)
    if rc != 0:
        hint = " (not logged in: run `bee login`)" if LOGIN_RE.search(out) else ""
        return sync_failed("exit=%s%s" % (rc, hint), out)
    log("OK sync completed")
    if os.path.exists(SYNC_FAILS):
        os.remove(SYNC_FAILS)
    note_skipped(out)
    return True


def run_aux_syncs(cfg):
    """Best effort for the other Bee data; a failure is logged at most once an hour per target."""
    bee = bee_command(cfg)
    if not bee:
        return
    for target in cfg.get("aux_sync_targets") or []:
        rc, out = run_bee(cfg, bee, target, cfg["aux_sync_timeout_seconds"])
        if rc == 0:
            continue
        mark = os.path.join(STATE, ".aux-warned-" + re.sub(r"[^A-Za-z0-9_-]", "_", target))
        if not mark_is_fresh(mark, 1):
            last = out.splitlines()[-1] if out else ""
            log("WARN bee sync --only %s %s%s" % (target, "timed out" if rc is None else "exit=%s" % rc,
                                                 ": " + last[:200] if last else ""))
            touch(mark)


def conversation_files(vault_raw):
    root = os.path.join(vault_raw, "conversations")
    for dirpath, _, files in os.walk(root):
        for name in files:
            if name.endswith(".md"):
                yield os.path.join(dirpath, name)


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def pick_copies(paths):
    """One file per conversation id. bee sync files a capture under its start date and never
    removes an older copy, so a changed start time or timezone can leave two; keep the one
    with the latest updated_at (the newest file on a tie)."""
    by_id = {}
    for p in paths:
        by_id.setdefault(os.path.splitext(os.path.basename(p))[0], []).append(p)
    chosen, stale = {}, {}
    for conv_id, ps in by_id.items():
        if len(ps) > 1:
            def newest(p):
                u = bee_time(bee_metadata(p).get("updated_at"))
                return (u.timestamp() if u else 0.0, mtime(p))
            ps = sorted(ps, key=newest, reverse=True)
            stale[conv_id] = ps[1:]
        chosen[conv_id] = ps[0]
    return chosen, stale


def warn_duplicates(stale):
    seen = load_json(DUPES, {}) or {}
    if not isinstance(seen, dict):
        seen = {}
    for conv_id, paths in sorted(stale.items()):
        for p in paths:
            if p not in seen.get(conv_id, []):
                log("WARN %s has more than one raw file; using the newest and ignoring %s" % (conv_id, p))
    if DRY_RUN:
        return
    if stale:
        atomic_write(DUPES, json.dumps(stale, indent=1))
    elif os.path.exists(DUPES):
        os.remove(DUPES)


def check_stale_sentinels(cfg):
    """Catch-all for a stuck consumer: any sentinel older than stale_sentinel_minutes."""
    d, limit, now, old = cfg["sentinel_dir"], cfg["stale_sentinel_minutes"] * 60, time.time(), 0
    try:
        names = os.listdir(d)
    except OSError:
        return
    for name in names:
        if name.endswith(".sentinel.md"):
            try:
                if now - os.path.getmtime(os.path.join(d, name)) > limit:
                    old += 1
            except OSError:
                pass
    if old:
        notify("stale", "%d Bee capture(s) have waited more than %d minutes. See bee-process.log."
               % (old, cfg["stale_sentinel_minutes"]))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def capture_date(path):
    """The YYYY-MM-DD folder a conversation file sits in, or ''."""
    d = os.path.basename(os.path.dirname(path))
    return d if re.match(r"^\d{4}-\d{2}-\d{2}$", d) else ""


def write_sentinel(sentinel_dir, conv_id, raw_path, source):
    body = (
        "---\n"
        "source: %s\n"
        "conversation_id: %s\n"
        "raw_path: %s\n"
        "written_at: %s\n"
        "auto_process: true\n"
        "---\n\n"
        "# Bee capture ready for processing\n\n"
        "New or updated raw capture detected: conversation **%s**\n\n"
        "**Read the raw file at:** `%s`\n\n"
        "Process per `.claude/agents/bee-processor.md` and auto-write all outputs (tasks, meeting notes, "
        "people notes) without prompting. When processing is complete, write the done marker it describes.\n"
    ) % (source, conv_id, raw_path, now_local().isoformat(timespec="seconds"), conv_id, raw_path)
    atomic_write(os.path.join(sentinel_dir, "%s.sentinel.md" % conv_id), body)


def kickstart(label):
    if not label:
        return
    target = "gui/%d/%s" % (os.getuid(), label)
    p = subprocess.run(["/bin/launchctl", "kickstart", target], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    if p.returncode == 0:
        log("KICK started consumer %s" % label)
    else:
        log("WARN could not kickstart %s (exit=%s): %s" % (label, p.returncode, (p.stdout or "").strip()[:200]))


def load_cache():
    """Return (cache, first_run, catch_up_since). A missing cache is a first run (seed
    silently); an unreadable one is set aside and recent captures fire again."""
    if not os.path.exists(HASHES):
        return {}, True, None
    cache = load_json(HASHES, None)
    if isinstance(cache, dict):
        return cache, False, None
    since = (dt.date.today() - dt.timedelta(days=CORRUPT_CATCH_UP_DAYS)).isoformat()
    if DRY_RUN:
        log("hash cache is unreadable; a real run would set it aside and fire ready captures since %s" % since)
    else:
        aside = "%s.corrupt-%s" % (HASHES, now_local().strftime("%Y%m%d-%H%M%S"))
        os.replace(HASHES, aside)
        log("ERROR hash cache was unreadable; moved it to %s and firing ready captures since %s" % (aside, since))
        notify("cache", "Bee sync's hash cache was unreadable, so it was reset and recent captures were queued again.")
    return {}, True, since


def main():
    global DRY_RUN, ECHO
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-sync", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--catch-up-since", metavar="YYYY-MM-DD")
    args = ap.parse_args()
    if args.catch_up_since:
        try:
            ok = bool(re.match(r"^\d{4}-\d{2}-\d{2}$", args.catch_up_since)) and dt.date.fromisoformat(args.catch_up_since)
        except ValueError:
            ok = False
        if not ok:
            print("--catch-up-since needs a date written YYYY-MM-DD, for example 2026-10-14", file=sys.stderr)
            return 2
    DRY_RUN = args.dry_run
    manual = bool(args.no_sync or args.catch_up_since or sys.stderr.isatty())
    ECHO = manual
    # launchd stops a job with SIGTERM; exit through Python so a running bee is stopped too.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))

    # A dry run writes nothing, so it needs no lock.
    if not DRY_RUN:
        os.makedirs(STATE, exist_ok=True)
        lock = open(LOCK, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if not manual:
                log("SKIP another bee_sync run holds the lock")
                return 0
            print("Waiting for the running bee_sync to finish (it can take a few minutes)...", file=sys.stderr)
            fcntl.flock(lock, fcntl.LOCK_EX)

    cfg = load_config()
    if not os.path.isdir(cfg["vault_raw"]):
        log("FATAL vault_raw does not exist: %s" % cfg["vault_raw"])
        notify("fatal", "Bee sync is stopped: the vault's raw folder is missing. See bee-sync.log.")
        return 1

    if not DRY_RUN:
        check_stale_sentinels(cfg)
    if not args.no_sync and not DRY_RUN and not run_bee_sync(cfg):
        trim_log()
        return 1

    cache, first_run, since = load_cache()
    since = args.catch_up_since or since
    stuck = load_json(STUCK, {}) or {}
    source = "bee-scheduled-sync"

    chosen, stale = pick_copies(conversation_files(cfg["vault_raw"]))
    warn_duplicates(stale)

    to_fire, seeded, caught_up = [], 0, 0
    for conv_id, path in sorted(chosen.items(), key=lambda kv: kv[1]):
        status, reason = capture_readiness(path, cfg["settle_minutes"], cfg["stuck_hours"])
        if status == "ready":
            stuck.pop(conv_id, None)
            try:
                digest = sha256(path)
            except OSError as e:
                log("WARN cannot read %s (%s); trying again next run" % (path, e))
                continue
            if since and capture_date(path) >= since:
                to_fire.append((conv_id, path, digest))
                caught_up += 1
            elif first_run:
                cache[conv_id] = digest
                seeded += 1
            elif cache.get(conv_id) != digest:
                to_fire.append((conv_id, path, digest))
        elif status == "stuck" and conv_id not in stuck:
            stuck[conv_id] = {"reason": reason, "first_seen": now_local().isoformat(timespec="seconds"), "raw_path": path}
            log("STUCK %s - %s" % (conv_id, reason))
        # capturing / unsettled / unknown: skip quietly, re-evaluate next run

    summary = "first_run=%s seeded=%d catch_up_since=%s %s=%d" % (
        first_run, seeded, since or "-", "would_fire" if DRY_RUN else "fired", len(to_fire))
    if DRY_RUN:
        print(summary)
        for conv_id, path, _ in to_fire:
            print("  %s  %s" % (conv_id, path))
        return 0

    if first_run:
        log("SEED hash cache seeded with %d ready conversations" % seeded)
    if caught_up:
        log("CATCHUP firing %d ready capture(s) dated %s or later" % (caught_up, since))
    if to_fire:
        os.makedirs(cfg["sentinel_dir"], exist_ok=True)
        for conv_id, path, digest in to_fire:
            write_sentinel(cfg["sentinel_dir"], conv_id, path, source)
            cache[conv_id] = digest
            log("SENTINEL wrote %s" % conv_id)
    if first_run or to_fire:
        atomic_write(HASHES, json.dumps(cache, separators=(",", ":")))
    if stuck:
        atomic_write(STUCK, json.dumps(stuck, indent=1))
    elif os.path.exists(STUCK):
        os.remove(STUCK)

    if to_fire:
        kickstart(cfg.get("process_label"))
    if not args.no_sync:
        run_aux_syncs(cfg)
    trim_log()
    if manual:
        print(summary)
        for conv_id, path, _ in to_fire:
            print("  %s  %s" % (conv_id, path))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            lines = traceback.format_exc().rstrip().splitlines()
            log("CRASH " + lines[-1])
            for line in lines[:-1]:
                log("  | " + line)
            notify("crash", "Bee sync crashed. See bee-sync.log.")
        except Exception:
            pass
        raise
