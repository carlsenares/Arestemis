# Roadmap

## v1 (current build) — "notify me first, draft on demand"
- WG-Gesucht + Kleinanzeigen monitoring, ~60s.
- Filter: single flats + empty 2-person WGs (LLM-classified).
- Instant Telegram alert with inline buttons.
- On-demand tailored **German** application (copy-paste to send).
- Runs 24/7 as a systemd service on Hetzner.

## v2 — candidate features (pick later, not committed)
- **Auto-send** (`📤 Send it` button). Submits the application through the portal's
  own messaging using a logged-in session.
  ⚠️ This is the ToS-risky part: it means automating WG-Gesucht's account/messaging,
  storing login, and handling captchas. Realistic downside = account suspension.
  Also: an *instant templated* message can read as a bot and lose. Keep this opt-in,
  behind a high-confidence filter, and only after v1 is trusted. Human-sent in <3 min
  often beats bot-sent.
- **ImmoScout24 adapter.** Biggest single-flat portal, but heavy anti-bot and no free
  listings API — expect to fight detection. Add as an isolated adapter.
- **Multi-user (friends).** Replace single chat id with a users table
  (`chat_id -> {criteria, about_me}`); fan out matches per user. Friends just Start the
  bot. No domain needed — still long-polling.
- **Smarter ranking.** Score matches (price vs area vs size) and highlight the best.
- **Dedup across portals.** Detect the same flat cross-listed on multiple sites.

## Non-goals
- No public website / domain (Telegram covers the UX).
- No mass-blasting low-quality applications.
- No automating hostile platforms in ways that risk your real, identity-bearing accounts beyond the single dedicated portal login.
