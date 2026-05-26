# md-writers audit (2026-05-26)

Grep: `_atomic_write`, `Path.write_text`, `open(..., 'w')`, `os.fdopen(fd, 'w')`.

## SoT-wired writers (record_block after write)

- `marrow/inserter.py:138` — subpage `.md` cold-start bootstrap.
- `marrow/inserter.py:166` — subpage `.md` rebootstrap (`force_sort_consistency`).
- `marrow/inserter.py:191` — subpage `.md` incremental append (Outcome 1 fix; previously recorded before).
- `marrow/dashboard.py:206` — `dashboard.md` top region (Outcome 1 fix).

## Indirect or bypassed writers

- `marrow/handover_render.py:252` — `handover.md` main path. `record_user_deletes` runs on prior; new blocks not directly recorded. Watcher ≤200 ms then `sync_file_observe` lays baseline. Race: crash between write/observe → next refresh treats auto-content as user edit. Low impact — regenerates each session.
- `marrow/handover_render.py:219` — `handover.md.partial.<sid>` fallback. Never records hashes. Partial renamed to `handover.md` → md_index has no baseline. Confirm intent.
- `marrow/subpages.py:161` — legacy full-render subpages. Free-form text outside markers preserved by `_split`. Reachable only if `spec_builder` missing/raises (every flat key wires inserter today). Silent SoT bypass → add assert `cfg.inserter is not None` for non-`read_only` paths.
- `marrow/subpages.py:152` — cheatsheet `.md`, `read_only=True` full overwrite. Generated-only; no user edits by design. Bypass correct if assumption holds.

## Out-of-scope writers

- `marrow/aging.py:152` — `~/.config/marrow/goose_log/<YYYY-MM>.md` monthly rewrites.
- `marrow/sessionend_writers.py:279` — `PROGRESS.md`, append-only, flock-guarded.

## Helpers

Three identical `_atomic_write` at `marrow/dashboard.py:55`, `marrow/inserter.py:43`, `marrow/subpages.py:94` → candidate for shared util.

## Bypass risks (priority order)

- handover partial fallback (`handover_render.py:219`) — no baseline; user-rename to `handover.md` defeats observe-only contract.
- legacy full-render subpages (`subpages.py:161`) — silent fallback if `spec_builder` errors; add alert or guard.
- cheatsheet read-only path (`subpages.py:152`) — design-intentional; confirm no future user-edit expectation.

## Audit method

```sh
grep -rn '_atomic_write\|\.write_text(\|open(..., .w.\|os.fdopen' marrow/ \
  --include='*.py' | grep -v __pycache__
```

Cross-check each call site for routing through `MdIndex.record_block`, `TombstoneStore.record_block`, or watcher-observed md root.
