# Bee pipeline on macOS (launchd)

macOS port of the Windows scripts one folder up. Same contract: `bee sync` writes raw captures
into the vault, a completeness gate decides which captures are ready, and one sentinel per
ready capture lands in `.kiro/bee-inbox/` for the Claude Code `bee-processor` subagent.

| File | Replaces | What it does |
|------|----------|--------------|
| `bee_sync.py` | `bee-sync-scheduled.ps1` + `bee-lib.ps1` | Runs `bee sync --only conversations`, applies the gate (COMPLETED, real `end_time`, untouched for 10 min), writes sentinels for changed ready captures, then kickstarts the consumer job so notes publish right away. Facts, todos and daily summaries sync after that as a best effort, so a failure there never holds back meeting notes. Python 3.9+ standard library only. |
| `run-bee-process.sh` | `run-bee-process.ps1` | Headless `claude -p '/process-bee-inbox runner-removes-sentinels'` from the repo root with no MCP servers loaded, an optional preflight command (for example an auth check), and a watchdog that stops the whole Claude process group. Early-outs when nothing is pending. See Permissions below. |
| `bee-launchd.sh` | the three `install-*.ps1` | `install`, `uninstall`, `status` for two per-user LaunchAgents: sync every 10 min, consumer every 30 min as a safety net. |

There is no Layer 2 stream watcher. With a 10-minute settle window in the gate, a capture
cannot fire before a sync that runs 10 minutes after its last update anyway, so a 10-minute
sync interval gets nearly the same latency with one fewer long-running process.

**Done markers.** The headless agent deletes nothing. When it finishes a capture (written,
already in the vault, dropped as private, or closed as sparse) it writes
`.kiro/bee-inbox/_done/<id>.md`, and the runner removes each sentinel whose marker is newer
than the sentinel. A partial capture gets no marker, so its sentinel waits for the next run.
A manual `/process-bee-inbox` writes the marker and deletes the sentinel itself.

**Permissions.** The agent reads untrusted transcripts, so the runner starts Claude in
`dontAsk` mode, where anything not allowed is denied. It may read inside the repo and the
vault and run `ls`, `wc` and `date` on paths there. It may write only Markdown files directly
inside the `tasks_dir`, `meeting_notes_dir` and `people_dir` folders of
`.claude/bee-paths.local.json` (not their subfolders), the `voice_observations_file`, and
`.kiro/bee-inbox/_done/`. The optional `deny_dirs` config key lists vault-relative folders it
may not read or write at all; keep folder names that should not appear in the repo there.

Two limits to know. Glob and Grep cannot be scoped to a folder, so they are allowed everywhere
except the home folder's dot-folders (`.ssh`, `.aws`, `.config`, ...), `Library`, `Desktop`,
`Documents` and `Downloads`; they can still list and search system paths such as `/etc`. And the
agent cannot see a folder in `deny_dirs`, so a capture about a person whose folder is listed
there makes it create a top-level `<people_dir>/<name>.md` instead of updating that folder.

## Setup

```bash
npm install -g @beeai/cli && bee login        # needs Node 22+
cp .claude/bee-paths.example.json .claude/bee-paths.local.json   # fill in vault folders
scripts/mac/bee-launchd.sh install --vault-raw "<vault>/<raw folder>"
scripts/mac/bee-launchd.sh status
```

The installer writes `~/.config/bee-sync/config.json` on first run. Optional keys there:
`claude_path_dirs` (extra dirs to prepend; `~/.local/bin`, `~/bin` and Homebrew
are already searched), `claude_model`, `preflight_command`, `timeout_seconds` (Claude watchdog,
default 1800), `sync_args`, `settle_minutes`, `stuck_hours`, `sync_timeout_seconds` (default 900),
`aux_sync_targets` (default `["facts", "todos", "daily"]`; `[]` turns them off),
`aux_sync_timeout_seconds` (default 300), `stale_sentinel_minutes` (default 120) and
`deny_dirs` (see Permissions).

**Keep the vault outside `~/Desktop`, `~/Documents` and `~/Downloads`.** macOS privacy
controls block background jobs from those folders unless you grant access by hand.

**Notifications.** A macOS notification, at most one per kind every 4 hours, says when the
preflight blocks processing, the config or vault folder is missing, `bee sync` keeps failing
(at once when the Bee login has expired, otherwise after 3 failures in a row), `bee sync` has
skipped some conversations 6 runs in a row, Claude fails two runs in a row, a sentinel has
waited longer than `stale_sentinel_minutes`, the hash cache was unreadable and had to be
reset, or `bee_sync.py` crashed.

**Moving from another machine:** the first run seeds the hash cache silently so history does
not flood the inbox. To process captures this machine never published, run
`scripts/mac/bee_sync.py --catch-up-since YYYY-MM-DD` by hand, any time after install. It fires
a sentinel for every ready capture dated that day or later, even ones already fired, and the
agent skips anything with a done marker or a meeting note. A run by hand (any flag, or from a
terminal) waits for a scheduled run to finish, shows its log lines, and ends with a summary;
`--dry-run` shows what would fire and writes nothing.

**Restoring the vault from a backup:** notes written after that backup are gone, but their done
markers are not. Move the markers written since the backup out of `.kiro/bee-inbox/_done/`
(`ls -lt` lists them newest first). Then run the catch-up from the earliest `capture_date` in
the markers you moved (a late capture can be dated well before the backup).

## Logs and state

`~/.local/state/bee-sync/`: `bee-sync.log`, `bee-process.log`, `bee-process.last.txt` (full
output of the last Claude run), `seen-hashes.json`, `stuck-captures.json`,
`duplicate-captures.json` (captures with more than one raw file), the counters
`sync-failures`, `sync-partial` and `bee-process.failures`, and the `.notified-*` rate-limit
marks.
