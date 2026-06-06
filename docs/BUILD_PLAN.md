# Build plan (ordered milestones)

Build these in order. Each milestone is independently runnable and has an **acceptance check** — verify it before moving on. Commit after each green milestone.

## Milestone 0 — Project skeleton
- Create package layout:
  ```
  src/flatsniper/
    __init__.py
    main.py            # scheduler loop / entrypoint
    config.py          # load config.yaml + .env
    models.py          # Listing dataclass
    store.py           # sqlite seen-listings
    filter.py          # criteria matching
    bot.py             # telegram long-polling + buttons
    llm/
      __init__.py
      provider.py      # groq wrapper: classify + draft
    sources/
      __init__.py
      base.py          # adapter interface
      wg_gesucht.py
      kleinanzeigen.py
      immoscout.py     # stub
  ```
- `config.py` loads `.env` (python-dotenv) and `config.yaml` (PyYAML) into typed objects; fail loudly with a clear message if required env vars are missing.
- **Acceptance:** `python -m flatsniper.main --check-config` prints the loaded (non-secret) config and exits 0.

## Milestone 1 — Storage + models
- Implement `Listing` dataclass (see ARCHITECTURE) and `store.py` with `is_new(source, id)`, `mark_seen(listing)`, `mark_notified(...)`. Auto-create the SQLite schema on first run.
- **Acceptance:** a unit test inserts a listing, second `is_new` returns False.

## Milestone 2 — Telegram notifier (prove the pipe end-to-end early)
- `bot.py`: send a formatted test message to `TELEGRAM_CHAT_ID` with the two inline buttons. Implement the `✍️ Generate` callback to reply with a placeholder string for now.
- Add `python -m flatsniper.bot --test-message` to send one hardcoded sample listing.
- **Acceptance:** running it makes your phone buzz with a message + working buttons. (Requires you to have pressed Start on the bot.)

## Milestone 3 — WG-Gesucht adapter
- Implement `wg_gesucht.py` against the mobile JSON API: build a Munich search from `config.yaml`, parse results into `Listing` objects. Handle pagination minimally (first page is enough at 60s cadence).
- **Acceptance:** `python -m flatsniper.sources.wg_gesucht --once` prints N normalized listings from a live query.

## Milestone 4 — Filter + LLM classify
- `filter.py`: single-flat rules + the empty-2p-WG gate.
- `llm/provider.py`: implement `classify_empty_2p_wg` and `draft_application` against Groq. German output for drafts.
- Wire `Generate` button to the real `draft_application`.
- **Acceptance:** feed a few sample WG descriptions; classifier separates empty-2p WGs from shared/occupied ones. Generate button returns a real German application.

## Milestone 5 — Full loop
- `main.py`: async scheduler — every `poll_interval_seconds`, run enabled adapters concurrently, dedup via `store`, filter, classify WG edge cases, notify new matches, `mark_seen`/`mark_notified`. Per-adapter try/except so one failure doesn't kill the loop. Structured logging.
- **Acceptance:** run it live for a few cycles; new matching listings ping you exactly once; restarts don't re-notify old ones.

## Milestone 6 — Deploy as systemd service (server)
- Provide `deploy/flatsniper.service` (systemd unit: own user, WorkingDirectory, venv ExecStart, `Restart=always`, EnvironmentFile or dotenv).
- Document enable/start/logs (`journalctl -u flatsniper -f`) in `deploy/README.md`.
- **Acceptance:** service survives a reboot and keeps notifying; isolated from the other project on the box.

## Milestone 7 — Kleinanzeigen adapter
- Implement `kleinanzeigen.py` (HTML + BeautifulSoup). Same interface; feeds the same pipeline.
- **Acceptance:** Kleinanzeigen matches also ping, deduped independently from WG-Gesucht.

---
See `docs/ROADMAP.md` for v2 (auto-send, ImmoScout, multi-user friends).
