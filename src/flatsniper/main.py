"""Scheduler loop / entrypoint.

Every `poll_interval_seconds`: run enabled adapters concurrently, dedup via the
store, prefilter, classify WG edge cases via the LLM, apply the post-classify
rent rule, notify new matches exactly once, and record seen/notified. One source
failing never kills the loop.

The Telegram bot polls in the same event loop so the ✍️ Generate button stays
live while the scheduler runs.

CLI:
  --check-config         load + print non-secret config, exit
  --dry-run              run the pipeline but log matches instead of sending;
                         also the recommended first run — it *seeds* the store
                         so a later real run won't notify every current listing
  --interval SECONDS     override poll interval (default: config value)
  --max-cycles N         stop after N cycles (default: run forever)
  --max-notify N         cap notifications per process (safety; default: none)
  --db PATH              override the SQLite path (default: data/flatsniper.db)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from telegram import Update

from . import filter as filt
from .bot import build_application, remember_listing, send_listing
from .config import Config, ConfigError, load_config
from .llm.provider import classify_empty_2p_wg
from .sources import wg_gesucht
from .store import Store

log = logging.getLogger(__name__)

# Only adapters that implement fetch(). Kleinanzeigen is added in Milestone 7.
ADAPTERS: dict = {wg_gesucht.name: wg_gesucht}

# Max NEW offers each adapter hydrates per cycle. Bounds request rate (politeness
# + avoids rate-limit bursts on cold start) while leaving ample headroom for the
# handful of genuinely-new listings a 60s cycle normally finds. Unhydrated offers
# aren't marked seen, so they're picked up on subsequent cycles, newest first.
MAX_HYDRATE_PER_CYCLE = 20


async def _gather_listings(config: Config, is_new) -> list:
    """Run every enabled adapter concurrently; a failing adapter yields []."""
    adapters = [a for n, a in ADAPTERS.items() if config.sources.get(n)]

    async def _safe(adapter):
        try:
            return await adapter.fetch(config, is_new, limit=MAX_HYDRATE_PER_CYCLE)
        except Exception:
            log.exception("adapter %s failed", adapter.name)
            return []

    results = await asyncio.gather(*(_safe(a) for a in adapters))
    return [listing for sub in results for listing in sub]


async def _evaluate(listing, config: Config) -> bool:
    """Full match decision for a single listing (prefilter -> WG classify -> rent)."""
    if not filt.passes_prefilter(listing, config):
        return False
    if listing.is_wg:
        is_empty = await asyncio.to_thread(classify_empty_2p_wg, listing, config.secrets)
        if not is_empty:
            return False
        return filt.passes_postclassify(listing, config.search)
    return True


async def run_cycle(config: Config, store: Store, notify, *, max_notify=None) -> tuple[int, int]:
    """One poll cycle. Returns (new_listings_seen, notified)."""
    listings = await _gather_listings(config, store.is_new)
    new_count = 0
    notified = 0
    for listing in listings:
        # Defensive dedup even though adapters already skip seen offers.
        if not store.is_new(listing.source, listing.external_id):
            continue
        new_count += 1
        matched = await _evaluate(listing, config)
        if matched and (max_notify is None or notified < max_notify):
            try:
                await notify(listing)
                store.mark_notified(listing)
                notified += 1
                continue
            except Exception:
                log.exception("notify failed for %s:%s", listing.source, listing.external_id)
        # Non-match, over the cap, or a failed send: record as seen so we neither
        # reprocess nor re-notify it next cycle.
        store.mark_seen(listing)
    return new_count, notified


async def scheduler_loop(config, store, notify, *, interval, max_cycles, max_notify) -> None:
    cycle = 0
    while max_cycles is None or cycle < max_cycles:
        cycle += 1
        try:
            new_count, notified = await run_cycle(config, store, notify, max_notify=max_notify)
            log.info("cycle %d: %d new listing(s), %d notified", cycle, new_count, notified)
        except Exception:
            log.exception("cycle %d crashed", cycle)
        if max_cycles is not None and cycle >= max_cycles:
            break
        await asyncio.sleep(interval)


async def _run(config: Config, *, dry_run, interval, max_cycles, max_notify, db_path) -> None:
    store = Store(db_path) if db_path else Store()

    if dry_run:
        async def notify(listing):
            kind = f"WG/{listing.wg_size}p" if listing.is_wg else "Flat"
            log.info(
                "[DRY-RUN MATCH] [%s] %s€ · %sm² · %s",
                kind, listing.rent_warm, listing.sqm, listing.url,
            )

        try:
            await scheduler_loop(
                config, store, notify,
                interval=interval, max_cycles=max_cycles, max_notify=max_notify,
            )
        finally:
            store.close()
        return

    # Live run: bot polls (for the Generate button) in the same loop.
    app = build_application(config)
    chat_id = config.secrets.telegram_chat_id

    async def notify(listing):
        remember_listing(app.bot_data, listing)
        await send_listing(app.bot, chat_id, listing)

    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("FlatSniper running — polling every %ss. Ctrl+C to stop.", interval)
        try:
            await scheduler_loop(
                config, store, notify,
                interval=interval, max_cycles=max_cycles, max_notify=max_notify,
            )
        finally:
            await app.updater.stop()
            await app.stop()
            store.close()


def _check_config() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Config error:\n{exc}", file=sys.stderr)
        return 1
    print("FlatSniper config loaded OK. (secrets redacted)\n")
    print(json.dumps(config.public_dict(), indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flatsniper", description="Munich flat-hunting bot")
    parser.add_argument("--check-config", action="store_true",
                        help="Load and print the (non-secret) config, then exit.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the pipeline but log matches instead of sending (also seeds the store).")
    parser.add_argument("--interval", type=int, default=None,
                        help="Override poll interval in seconds.")
    parser.add_argument("--max-cycles", type=int, default=None, help="Stop after N cycles.")
    parser.add_argument("--max-notify", type=int, default=None, help="Cap notifications per run.")
    parser.add_argument("--db", default=None, help="Override SQLite DB path.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.check_config:
        return _check_config()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Config error:\n{exc}", file=sys.stderr)
        return 1

    interval = args.interval if args.interval is not None else config.poll_interval_seconds
    try:
        asyncio.run(_run(
            config,
            dry_run=args.dry_run,
            interval=interval,
            max_cycles=args.max_cycles,
            max_notify=args.max_notify,
            db_path=args.db,
        ))
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
