# FlatSniper — build instructions for Claude Code

You are building **FlatSniper**, a Munich apartment-hunting Telegram bot. This repo currently contains the **spec and scaffold**; your job is to implement it on the server.

## How to work
1. Read `docs/ARCHITECTURE.md` (design) and `docs/ROADMAP.md` (scope) first.
2. **Before starting any non-trivial milestone, weigh whether to enter plan mode first.** If the work touches multiple files, has design choices, or its approach isn't obvious, draft and confirm a plan before writing code. Skip planning only for genuinely small, mechanical changes.
3. Build **milestone by milestone** following `docs/BUILD_PLAN.md`. After each milestone, verify its acceptance check before moving on.
4. **Always generate tests that verify the milestone works**, even when the acceptance check is a manual command. Prefer runnable `pytest` tests under `tests/`; run them and show they pass before considering a milestone done.
5. Keep changes small and runnable. Commit after each working milestone.

## Hard rules
- **Never commit secrets.** All tokens/keys live in `.env` (git-ignored). Read them via `python-dotenv`. `.env.example` lists the required vars.
- **User criteria live in `config.yaml`** (git-ignored), shaped like `config.example.yaml`. Never hardcode personal data.
- **Be polite to portals:** respect the configured poll interval (default 60s), use a real User-Agent, no aggressive parallelism. Scraping WG-Gesucht/Kleinanzeigen is against their ToS — keep request rates low; this is single-user personal use.
- **Telegram runs in long-polling mode** — no public web server, no domain, no inbound ports.
- **LLM access goes through one provider module** (`llm/provider.py`) so the backend (Groq by default) is swappable. Output language for applications is **German**.
- Target runtime: **Python 3.11+**, deployed as a **systemd service** in an isolated venv on a Hetzner box that also runs an unrelated project — stay self-contained.

## Stack
Python · httpx · python-telegram-bot · SQLite (stdlib `sqlite3`) · PyYAML · python-dotenv · Groq (OpenAI-compatible API) · BeautifulSoup (for HTML sources).
