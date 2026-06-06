# Architecture

## Goal
Be the **first** to apply to a matching Munich listing. Detect new listings within ~60s, push to phone instantly, draft a tailored German application on demand.

## High-level flow
```
                ┌─────────────┐
   every ~60s   │  Scheduler  │
   ─────────────▶  (loop)     │
                └──────┬──────┘
                       │  fetch
        ┌──────────────┼───────────────┐
        ▼              ▼                ▼
 ┌────────────┐ ┌────────────┐   (immoscout: later)
 │ WG-Gesucht │ │Kleinanzeig.│   each = a "source adapter"
 │  adapter   │ │  adapter   │
 └─────┬──────┘ └─────┬──────┘
       └──────┬───────┘
              ▼  normalized Listing objects
       ┌─────────────┐   seen-id check (SQLite)
       │  Dedup +    │──────────────────────────┐
       │  Filter     │   drop already-seen       │
       └─────┬───────┘                           ▼
             │  candidates                 ┌───────────┐
             ▼                             │  SQLite   │
       ┌─────────────┐  WG edge case       │ listings  │
       │ LLM classify│◀────────────────────│  store    │
       │ (is empty   │                     └───────────┘
       │  2p WG?)    │
       └─────┬───────┘
             ▼  matches
       ┌─────────────┐
       │  Telegram   │  message + inline buttons
       │  notifier   │  [🔗 Open] [✍️ Generate]
       └─────┬───────┘
             │ user taps "Generate"
             ▼
       ┌─────────────┐
       │ LLM provider│  -> tailored German application text
       │  (Groq)     │  replied back into the chat to copy-paste
       └─────────────┘
```

## Components

### Source adapters (`sources/`)
Each adapter implements a common interface and returns a list of normalized `Listing` objects:
```python
class Listing:
    source: str          # "wg_gesucht" | "kleinanzeigen"
    external_id: str      # unique per source
    url: str
    title: str
    rent_warm: int | None
    rooms: float | None
    sqm: float | None
    is_wg: bool
    wg_size: int | None
    description: str
    posted_at: datetime | None
    raw: dict             # original payload, for debugging
```
- **`wg_gesucht.py`** — use the WG-Gesucht **mobile JSON API** (reverse-engineered) rather than HTML; more stable. Build a search query from `config.yaml`.
- **`kleinanzeigen.py`** — HTML + BeautifulSoup over the Munich apartment category, filtered by criteria.
- **`immoscout.py`** — stub only in v1 (documented later adapter).

Adding a portal = adding one file implementing the interface. Keep it that clean.

### Storage (`store.py`)
SQLite via stdlib `sqlite3`. One table `seen_listings(source, external_id, first_seen, notified)` keyed by `(source, external_id)`. Used purely to know what's new and avoid duplicate pings. No ORM.

### Filter (`filter.py`)
Pure functions over a `Listing` + the `search` config:
- Single apartment (`is_wg == False`) → must clear the **size-tiered rent rule**: qualifies if any tier matches (`sqm >= tier.min_sqm` and `rent_warm <= tier.max_rent_warm`), plus area + move-in date. E.g. 28–39 m² ≤ €1000, 40 m²+ ≤ €1200. Anything under `min_sqm` is rejected outright.
- WG (`is_wg == True`) → only continue if `wg_size == 2` and `allow_empty_2person_wg`, then defer the "is it actually empty / new" judgment to the LLM classifier. (Same tiered rent rule applies once classified.)

### LLM provider (`llm/provider.py`)
Single thin module wrapping the Groq client (OpenAI-compatible). Two functions:
- `classify_empty_2p_wg(listing) -> bool` — reads the description, returns whether it's an empty 2-person WG the user would fully control.
- `draft_application(listing, about_me) -> str` — writes a warm, concise **German** application tailored to the listing.

Model + key come from env (`GROQ_API_KEY`, `LLM_MODEL`). Keep all prompt strings here so swapping providers is a one-file change.

### Telegram bot (`bot.py`)
`python-telegram-bot` in **long-polling** mode (no webhook, no domain, no inbound ports).
- Sends a formatted message per match with inline buttons: `🔗 Open listing` (URL button) and `✍️ Generate application` (callback).
- On `Generate` callback → calls `draft_application` → replies with the text for copy-paste.
- `(later)` a `📤 Send it` button for auto-submission.

### Scheduler (`main.py`)
Async loop: every `poll_interval_seconds`, run all enabled adapters concurrently (`asyncio.gather`), normalize → dedup → filter → classify → notify. Robust to a single source failing (catch per-adapter, log, continue). Runs forever; designed to be supervised by systemd.

## Deployment
- Python 3.11+ in an isolated venv. Runs as a **systemd service** on the existing Hetzner box (own user/dir/venv; negligible footprint, won't disturb the other project). See `docs/BUILD_PLAN.md` Milestone 6.
- Secrets in `.env`, user criteria in `config.yaml`, both git-ignored.

## Multi-user (v2)
Replace the single `TELEGRAM_CHAT_ID` with a small users table mapping `chat_id -> {criteria, about_me}`, and fan out matches per user. Friends just Start the bot. Out of scope for v1.
