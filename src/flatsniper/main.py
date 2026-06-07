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
from . import health, monitor
from .bot import (
    PAUSED_META_KEY,
    SOURCE_LABELS,
    build_application,
    remember_listing,
    send_listing,
)
from .config import Config, ConfigError, load_config
from .llm.provider import classify_empty_2p_wg
from .sources import http_session, immowelt, kleinanzeigen, wg_gesucht
from .store import Store

log = logging.getLogger(__name__)

# Source adapters implementing fetch(). The SessionPool hands each its own
# long-lived, cookie-keeping client; the store dedups per (source, external_id).
ADAPTERS: dict = {
    wg_gesucht.name: wg_gesucht,
    kleinanzeigen.name: kleinanzeigen,
    immowelt.name: immowelt,
}

# Max NEW offers each adapter hydrates per cycle. Bounds request rate (politeness
# + avoids rate-limit bursts on cold start) while leaving ample headroom for the
# handful of genuinely-new listings a 60s cycle normally finds. Unhydrated offers
# aren't marked seen, so they're picked up on subsequent cycles, newest first.
MAX_HYDRATE_PER_CYCLE = 20

# Cold-start ramp: a fresh DB would otherwise try to hydrate a full cap of offers
# in one burst (this historically tripped HTTP 429s). Instead grow the cap over
# the first cycles — `COLD_START_STEP * cycle`, capped — so the initial backfill
# is spread out. Harmless in steady state (a populated DB rarely has many new
# offers per cycle anyway) and reaches the full cap within a few cycles.
COLD_START_STEP = 5

# Jitter the poll interval by ±this fraction so cycles aren't a fixed metronome
# (perfectly periodic requests are an easy automated-client signature). Modest,
# to stay responsive on a ~60s target.
POLL_INTERVAL_JITTER = 0.15

def _hydrate_limit(cycle: int) -> int:
    """Per-cycle hydrate cap, ramped up over the first cycles (1-indexed)."""
    return min(MAX_HYDRATE_PER_CYCLE, COLD_START_STEP * max(cycle, 1))


async def _gather_listings(config: Config, is_new, *, limit, sessions=None, stats=None) -> list:
    """Run every enabled adapter concurrently; a failing adapter yields [].

    When `stats` is given, each adapter's raw card count lands in
    `stats[name]["enumerated"]` so the scrape watchdog can see whether we got any
    cards at all this cycle (a missing key means the adapter never reported one —
    treated as trouble)."""
    adapters = [a for n, a in ADAPTERS.items() if config.sources.get(n)]

    async def _safe(adapter):
        try:
            kwargs = {"limit": limit}
            # Hand the adapter its long-lived, cookie-keeping client when we have
            # a pool, so polling looks like one returning session.
            if sessions is not None:
                kwargs["client"] = sessions.client_for(adapter.name)
            if stats is not None:
                src_stats: dict = {}
                stats[adapter.name] = src_stats
                kwargs["stats"] = src_stats
            return await adapter.fetch(config, is_new, **kwargs)
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


async def run_cycle(
    config: Config, store: Store, notify, *,
    max_notify=None, limit=MAX_HYDRATE_PER_CYCLE, sessions=None, stats=None,
) -> tuple[int, int]:
    """One poll cycle. Returns (new_listings_seen, notified). When `stats` is
    given, per-source card counts are recorded into it for the scrape watchdog."""
    listings = await _gather_listings(
        config, store.is_new, limit=limit, sessions=sessions, stats=stats
    )
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


async def _diagnose_source(source: str) -> tuple[str, str]:
    """Classify why a source stopped returning cards, with a *fresh* probe.

    Live session already failed (that's why we're here); if a fresh session gets
    cards, the block is on our session (self-healable). Otherwise it's the portal
    (hard) or a network/TLS error."""
    res = await health.probe(source)
    if res.ok:
        return monitor.SESSION, res.detail
    if res.state == health.ERROR:
        return monitor.ERROR, res.detail
    return monitor.PORTAL, res.detail


async def _run_scrape_watch(watch, config, cycle_stats, sessions, alert) -> None:
    """Feed each enabled source's card count to the watch (detect/heal/notify)."""
    for name in ADAPTERS:
        if not config.sources.get(name):
            continue
        enumerated = (cycle_stats.get(name) or {}).get("enumerated")
        await watch.observe(
            name, SOURCE_LABELS.get(name, name), enumerated,
            sessions=sessions, alert=alert, diagnose=_diagnose_source,
        )


async def scheduler_loop(
    config, store, notify, *, interval, max_cycles, max_notify,
    sessions=None, is_paused=None, alert=None,
) -> None:
    cycle = 0
    scans = 0  # only scanning cycles count toward the cold-start ramp
    watch = monitor.ScrapeWatch()
    # The scrape watchdog needs both a session pool (to rotate/heal) and an
    # alerter (to notify) — i.e. a live run, not dry-run/tests.
    monitoring = alert is not None and sessions is not None
    while max_cycles is None or cycle < max_cycles:
        cycle += 1
        if is_paused is not None and is_paused():
            # Paused from the phone: keep the loop (and Telegram polling) alive
            # but don't touch the portals.
            log.info("cycle %d: paused, skipping scan", cycle)
        else:
            scans += 1
            limit = _hydrate_limit(scans)
            cycle_stats: dict = {} if monitoring else None
            run_kwargs = dict(max_notify=max_notify, limit=limit, sessions=sessions)
            if monitoring:
                run_kwargs["stats"] = cycle_stats
            crashed = False
            try:
                new_count, notified = await run_cycle(config, store, notify, **run_kwargs)
                log.info(
                    "cycle %d: %d new listing(s), %d notified (hydrate cap %d)",
                    cycle, new_count, notified, limit,
                )
            except Exception:
                log.exception("cycle %d crashed", cycle)
                crashed = True
            # Per-cycle scrape watchdog: detect a source going dark, classify,
            # self-heal a session block, notify on break/recovery.
            if monitoring and not crashed:
                await _run_scrape_watch(watch, config, cycle_stats, sessions, alert)
        if max_cycles is not None and cycle >= max_cycles:
            break
        # Jittered sleep so requests aren't perfectly periodic.
        await asyncio.sleep(http_session.jitter(interval, POLL_INTERVAL_JITTER))


async def _run(config: Config, *, dry_run, interval, max_cycles, max_notify, db_path) -> None:
    store = Store(db_path) if db_path else Store()
    # One pool of long-lived, cookie-keeping clients shared across cycles — the
    # anti-ban "returning session" (see http_session.SessionPool).
    sessions = http_session.SessionPool()

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
                sessions=sessions,
            )
        finally:
            await sessions.aclose()
            store.close()
        return

    # Live run: bot polls (for the Generate button + control commands) in the
    # same loop.
    app = build_application(config)
    chat_id = config.secrets.telegram_chat_id
    # Share the store with the bot (for the persisted pause flag) and restore the
    # last paused state so a restart honors a phone "/stop".
    app.bot_data["store"] = store
    app.bot_data["sessions"] = sessions  # lets /health probe the live sessions
    app.bot_data["paused"] = store.get_meta(PAUSED_META_KEY, "0") == "1"

    async def notify(listing):
        remember_listing(app.bot_data, listing)
        await send_listing(app.bot, chat_id, listing)

    async def alert(text):
        # Plain push for watchdog ban/recovery notices.
        await app.bot.send_message(chat_id, text)

    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info(
            "FlatSniper running — polling every ~%ss (%s). Ctrl+C to stop.",
            interval, "paused" if app.bot_data["paused"] else "scanning",
        )
        try:
            await scheduler_loop(
                config, store, notify,
                interval=interval, max_cycles=max_cycles, max_notify=max_notify,
                sessions=sessions, is_paused=lambda: bool(app.bot_data.get("paused")),
                alert=alert,
            )
        finally:
            await app.updater.stop()
            await app.stop()
            await sessions.aclose()
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
