# Deploying FlatSniper as a systemd service

Runs FlatSniper 24/7 on the Hetzner box, restarting on crashes and surviving
reboots. It shares the machine with an unrelated project (InsureAI, in Docker)
but is fully isolated — see [Isolation](#isolation-from-insureai).

## Prerequisites (one-time)

From `/root/flatsniper`:

1. **Venv + deps** (already done during the build):
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -e .
   ```
2. **Secrets + criteria** present and filled in:
   - `.env` — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GROQ_API_KEY`, `LLM_MODEL`
   - `config.yaml` — your search criteria (copy from `config.ready.yaml`)
   Verify:
   ```bash
   .venv/bin/python -m flatsniper.main --check-config
   ```
3. **Press Start** on your bot in Telegram (so it may message you).

## Seed the store first (important)

On a cold database there are ~150 currently-matching listings. If you start the
live service straight away it will notify you about **all** of them. Seed once so
you only get *future* new listings:

```bash
# Marks current listings as seen WITHOUT notifying; also a dry preview of matches.
# Run until cycles report "0 new listing(s)" (a few minutes), then Ctrl+C.
.venv/bin/python -m flatsniper.main --dry-run --interval 30
```

(The scheduler hydrates at most 20 new offers per cycle, so seeding takes several
cycles — that's intentional, it keeps request rates polite.)

## Install & enable

```bash
sudo cp /root/flatsniper/deploy/flatsniper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flatsniper      # start now + on every boot
```

## Operate

```bash
systemctl status flatsniper          # is it running?
journalctl -u flatsniper -f          # live logs (cycle summaries, matches)
journalctl -u flatsniper -n 100      # last 100 lines
sudo systemctl restart flatsniper    # after a config/code change
sudo systemctl stop flatsniper       # pause notifications
sudo systemctl disable flatsniper    # stop starting on boot
```

## Verify reboot-survival (acceptance)

```bash
systemctl is-enabled flatsniper      # -> enabled
sudo reboot
# after it comes back:
systemctl is-active flatsniper       # -> active
journalctl -u flatsniper --since "5 min ago"
```

Because the SQLite store persists (`data/flatsniper.db`), a restart/reboot does
**not** re-notify listings you've already seen.

## Updating

```bash
cd /root/flatsniper
git pull
.venv/bin/pip install -e .           # if deps changed
sudo systemctl restart flatsniper
```

## Isolation from InsureAI

- **No ports:** FlatSniper uses Telegram long-polling — it opens no listening
  sockets, so it cannot collide with InsureAI's nginx (80/443) or redis.
- **Own venv & data:** everything lives under `/root/flatsniper` (its own
  `.venv` and `data/flatsniper.db`); nothing is shared with InsureAI's Docker
  stack.
- **Resource caps:** the unit sets `MemoryMax=256M`, `CPUQuota=40%`,
  `TasksMax=64`, so a runaway FlatSniper can't starve InsureAI.
- **Separate unit:** `flatsniper.service` is independent of InsureAI's
  `docker compose` lifecycle.

## Optional: more-hardened layout

The shipped unit runs as **root** because the code lives under `/root`. For
stronger isolation, relocate to a dedicated user:

```bash
sudo useradd --system --create-home --home-dir /opt/flatsniper flatsniper
sudo cp -r /root/flatsniper/. /opt/flatsniper/
sudo chown -R flatsniper:flatsniper /opt/flatsniper
```

Then in the unit set `User=flatsniper`, `Group=flatsniper`, update the
`/root/flatsniper` paths to `/opt/flatsniper`, and you can additionally enable
`ProtectHome=true` and `ProtectSystem=strict` with
`ReadWritePaths=/opt/flatsniper/data`.
