# 🎯 FlatSniper

A 24/7 bot that watches Munich apartment portals, catches new matching listings within ~60 seconds, pings your phone via Telegram, and drafts a tailored German application on demand — so you're *first* in a market where the first 30 minutes decide everything.

> **Status:** Spec + scaffold. Implementation is built on the server by following [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md).

## What it does (v1)
- **Monitors** WG-Gesucht + Kleinanzeigen every ~60s for new listings.
- **Filters** to: single apartments, *or* empty 2-person WGs (the kind where you pick your flatmate — classified by the LLM from the description).
- **Notifies** instantly via a Telegram bot with inline buttons: `🔗 Open listing` · `✍️ Generate application`.
- **Generates** a warm, tailored **German** application from the listing + your profile, using a free LLM (Groq). You copy-paste it to send.

## What it is NOT (yet)
- No auto-submitting applications (ToS-risky — see [`docs/ROADMAP.md`](docs/ROADMAP.md)).
- No ImmoScout24 yet (heavy anti-bot — later adapter).
- No multi-user / friends support yet (v2).

## Quickstart (on the server)
```bash
git clone https://github.com/carlsenares/flatsniper.git
cd flatsniper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then fill in your secrets
cp config.example.yaml config.yaml   # then fill in your search criteria + about-me
# build/run per docs/BUILD_PLAN.md
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design and [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md) for the ordered build milestones.
