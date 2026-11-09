# Handoff: the 2026-11-09 batch (branch `move-refresh-stage-locks`)

Where this batch stood when the session paused, for the agent who picks it up. Delete this note when the batch is merged.

## What the batch is

All on the branch `move-refresh-stage-locks`, off `main` (which already has `1aa1aeb`, the `move_note` display-text rename, pushed, deployed and live-tested by the owner). Nothing on the branch is pushed. The owner asked for one full test run and one push for the whole batch.

| Commit | What |
| --- | --- |
| `f888be7` | `move_note(rewrite_links=True)` runs the index stage for the caller's scope before reading `note_links` (`indexer.refresh_index_for_move`), or refuses with `index_not_ready`; the index and embed stages get separate locks (`index_pass_lock`, `embed_pass_lock`) |
| `a3f1494` | Fixes from the adversarial review of `f888be7` (unreadable notes don't refuse, `expected_hash` re-checked after the refresh, the refresh doesn't feed `indexer_health`, cancellation not swallowed, sweep-record clear after a backstop commit) |
| `bef2d04` | `FORK.md`: expected test run times, and the machine they were measured on |
| `66ff467` | The pass's "Found N markdown files" log line counts why each note was read; the `INDEX_STAT_SHORTCUT` advice no longer says "all FUSE mounts" |
| `3a3a0b3` | Two loops: `run_indexer_loop` (startup index pass, then the index stage every `INDEX_INTERVAL_SECONDS`) and `run_embed_loop` (first tick at once, then every `EMBED_INTERVAL_SECONDS`, new, 300; budget re-tick; pre-warm), supervised together; idle index ticks write no `indexer_runs` row; run rows start their clock after the lock; `/health` gets `embedding_stalled` |

The working tree is clean apart from this note.

## What is left, in order

1. **Choose the index interval.** Leading proposal: **120 s** for `INDEX_INTERVAL_SECONDS` (default in `src/config.py`, `.env.example`, `README.md`). Why: on the owner's server the index scan takes about 4 s since the stat shortcut is on (owner's log, 2026-11-09), so 120 s keeps it about 3 % busy and cuts search/backlink staleness from 5 to 2 minutes; 60 s doubles the cost for one minute less. Moves no longer depend on it (they refresh first). Not yet confirmed by the owner. Texts that promise "five minutes" and must change with it: `src/mcp_server/server.py` (~272 `list_notes`, ~863), `src/mcp_server/tools.py` (~4833, the `index_not_ready` stale-marker refusal), `src/services/indexer.py` (~7240 and other comments that say a five-minute loop), `src/config.py` (~456, the embed budget's "matches one `INDEX_INTERVAL_SECONDS`": the budget now belongs to the embed loop, whose interval stays 300).
2. **Adversarial review of `3a3a0b3`** (the loop split touches the embedding path, so the repo's review rule applies; `f888be7` already had one). Brief it with docs/architecture/indexing-and-embeddings.md, sections "Two stage locks" and "Two loops".
3. **Panel item 1 (durations including queue wait)**: largely done by `3a3a0b3` (run rows per stage, clock after the lock). Check with the owner whether that is enough; the panel could also show index and embed rows apart (`indexer_runs` has no stage column; the trigger CHECK is closed, so a new trigger value needs a migration).
4. **Panel item 2 (a pass in the server log ~5 min after startup missing from the panel's history for 20+ minutes)**: quick look done, no obvious cause. Leads, untested: with the old single loop a run row was written only when the whole pass (index + embed) finished, so a long embed stage delays the row (`3a3a0b3` changes this); otherwise the insert failed, and the server log would say "Could not record indexer run". The owner said to drop it if not obvious.
5. **The full test run** (container, `-n 4`, see `FORK.md`), then **push to `main`** (fast-forward the branch), per CLAUDE.md "Git". Then the live checks after deploy: call `move_note(rewrite_links=True)` on a note linked from a just-written note (expect the link rewritten), and read the server log's "Found … read because:" line and `/health` (`embedding_stalled` present, `false`).

## Found today and not written elsewhere

- The owner's deployment (SGL PyInfra, `D:\work\sgl-pyinfra`) set `INDEX_STAT_SHORTCUT=false`, so every pass re-read all ~6,070 notes (13 s). The owner removed it on 2026-11-09 (scan now ~4 s, "almost every note unchanged by stat"). The SGL PyInfra README line was changed locally by an agent; the owner reviews and commits it.
- gocryptfs 2.5.1 mounted `-allow_other -acl` keeps size, mtime, ctime and inode stable across passes and remounts (measured on tmpfs-backed cipher dir; the server's is presumably ext4).
- In plain WSL (no Postgres container), some loop tests wait on a database connection rather than fail (`tests/test_issue_160_indexer_runs.py`, `tests/test_issue_78_panel_labels.py`, beside the three `FORK.md` lists); run them in the container. `pytest -o faulthandler_timeout=20` names where a test hangs.
- On this PC the WSL VM, Podman's machine included, went down once on 2026-11-09 while tests ran in WSL; `podman machine start` brings it back.
