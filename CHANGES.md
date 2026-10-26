# PLMS code changes (items 1–17)

## Batch 1 — functional gaps
1. **Compare any two versions** — `GET /api/project/<id>/compare?a=&b=`; "Compare Versions" card with pickers (`project.html`, `app.js:initCompare`); `ChangeEngine.compare`.
2. **Arrangement + complexity** — `flp_parser.py` decodes the playlist event (60-byte items; verified on the three sample projects). New `Version` columns: `playlist_items, tracks_used, arrangement_bars, complexity, arrangement_json`. `structural_complexity()` documented in `engines.py`.
3. **Annotations** — `POST /api/version/<id>/notes`, note button on each version card.
4. **Search / filter** — filter bar on the project page (`_filter_versions`).
5. **Export** — `/project/<id>/export.csv` and `.pdf` (CSV cells are protected against spreadsheet formula injection).

## Batch 2 — behaviour fixes
6. **Notifications** — `Notification` table + `notify()`; capture, score, prediction, rollback and failures; `/api/notifications` polled by `app.js`; the `notifications_enabled` setting is now honoured (errors always show).
7. **Pattern identification** — `PatternAnalyzer` ("Development Patterns" card).
8. **Rollback** — modal confirmation; safety copy of the current file; atomic, checksum-verified restore with automatic undo on failure; recorded as a new version (`origin='rollback'`, `restored_from`); `RollbackEvent` audit trail; watcher echo is ignored.
9. **Rollback button** — verified correct (hidden only on the latest version); now also hidden on demo projects.
10. **Prediction** — trend thresholds use relative slope (scale-independent); confidence uses R² + sample size; predictions clamped to valid ranges.
11. **Scoring transparency** — real per-dimension breakdown stored with every version and shown on the page and in compare; Settings weights are now actually applied (previously the form saved nothing).

## Batch 3 — security, integrity, quality
12. **Backup integrity** — SHA-256 per capture/backup, verified before rollback; `POST /api/project/<id>/verify` + "Verify backups" button.
13. **Secret key** — from `PLMS_SECRET_KEY` or a random per-install key (no hard-coded secret).
14. **CSRF** — Flask-WTF on all forms and `fetch` calls.
15. **Benchmark** — `benchmarks/bench.py`, `tests/test_performance.py`.
16. **Tests** — 66 pytest tests (`tests/`).
17. **Demo mode** — `Project.is_demo`; DEMO badges; no uploads/rollback; no duplicate loads; "Remove demo projects"; old demo data is auto-flagged on upgrade. The random "simulated save" path was removed from the real capture code.

## Bugs found and fixed along the way
- The old start-up migration used a string `conn.execute(...)` that fails on SQLAlchemy 2 and was silently swallowed, so `sample_count` was **never stored** (the model had no such column either).
- Settings → Scoring saved nothing and the engine ignored the weight sliders.
- Project upload used the raw client filename in a path (path traversal) — now `secure_filename`.
- Login `?next=` allowed open redirects — now local paths only.
- Toasts rendered messages as HTML (project names are user input → XSS) — now plain text.
- Backups were missing for the first version and for manual uploads, so those versions could not be rolled back to.
- Same project name for two users shared one backup folder — backups are now per project id.
- `pattern_count` came from an unrelated event; it is now the number of distinct patterns actually placed in the playlist (falls back to the old method if there is no playlist). **Scores of newly captured versions may differ slightly from older ones for this reason.**
- `watcher.start_watcher()` accepted only a single folder while the app passes a list (Settings → start watcher crashed with `TypeError: expected str, bytes or os.PathLike object, not list`). It now takes one path or a list, skips folders nested inside another watched folder, ignores FL Studio's own `Backup/` folder, and old guardian threads exit when a new watcher replaces them. Covered by tests that start the real watcher.
