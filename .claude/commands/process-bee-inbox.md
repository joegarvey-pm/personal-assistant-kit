---
description: Process all pending Bee sentinels into redacted Obsidian notes (the reliable manual path)
---

Process my Bee inbox - drain every pending sentinel in `.kiro/bee-inbox/` in one pass.

Run the `bee-processor` subagent in the foreground and wait for it to finish; never start it in the background. Have it:
1. Enumerate every `*.sentinel.md` directly in `.kiro/bee-inbox/` (ignore `.gitkeep`, `_done/`, `_staging/`, `_output/`). If none, report "Bee inbox is clear" and stop. Otherwise report the count.
2. For each sentinel: read `conversation_id` + `raw_path` (tolerate a leading UTF-8 BOM; a POSIX path from the macOS/Linux sync is used as-is, a Windows path is translated to its WSL `/mnt/c/...` form), run the idempotency guard (a done marker in `.kiro/bee-inbox/_done/` or a meeting note with the id means done; a recent tasks file with no meeting note means resume), and apply the completeness gate (process only COMPLETED + settled captures; leave partial/sparse ones and their sentinels, with no marker).
3. Process each ready capture per the redaction policy and write the outputs directly to the vault, using the folders from `.claude/bee-paths.local.json`, in the agent's order: tasks, selective People notes, optional voice observation, and the meeting note last. Every task/meeting-note output carries `bee_conversation_id`.
4. Write `.kiro/bee-inbox/_done/<id>.md` as the last step for every finished capture (processed, resumed, already done, private, or closed as sparse), then delete its sentinel. If the arguments include `runner-removes-sentinels`, delete nothing: the headless runner removes each sentinel whose marker is newer.
5. Summarize: N found -> M processed (slugs), K already done, P left pending, markers written, sentinels remaining. Do not recap meeting contents.

If the user passed arguments ($ARGUMENTS), pass `runner-removes-sentinels` through to the subagent when it is present, and treat anything else as a scope hint (e.g. a specific conversation id, or "dry run" to enumerate and gate-check without writing anything, markers included).
