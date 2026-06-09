"""Portal reachability / ban detection.

Answers one question on demand or on a schedule: *can we still reach each
portal and get real listings, or are we being blocked?* Used by the Telegram
`/health` command (manual check from the phone) and as the **classifier** for the
scheduler's per-cycle scrape watchdog (see `monitor.py`), which decides whether a
source that stopped returning cards is blocked at the portal, blocked only on our
session, or just erroring.

The signal that matters is **did real listing cards render**, not whether some
marker substring is present: a healthy Kleinanzeigen page contains the class name
"aditem" hundreds of times in its CSS/JS, so presence proves nothing. Each portal
therefore has a `count_cards` callable (its own adapter's parser) and we key "OK"
on a positive card count. Classification of one fresh probe:

  - 200 + cards > 0                       -> OK
  - 403                                    -> BLOCKED (hard, portal-level)
  - 429                                    -> rate-limited (portal-level)
  - 200 + captcha/datadome, no cards       -> challenge/captcha page (portal-level)
  - 200 but no cards                       -> reachable but stripped (block or layout)
  - other / exception                      -> error (network/TLS/timeout)

A *session*-level block (the portal serves our long-lived session empty pages
while a fresh session still gets cards) is not visible from one probe — the
scheduler concludes it by combining a live-session failure with a fresh probe
that comes back OK. See `monitor.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .sources import http_session, immoscout, immowelt, kleinanzeigen, wg_gesucht

# Health states. SESSION_BLOCKED is never produced by `classify` (one probe can't
# see it); the scrape watchdog / session-aware report derive it.
OK = "ok"
PORTAL_BLOCKED = "portal_blocked"
SESSION_BLOCKED = "session_blocked"
ERROR = "error"


@dataclass(frozen=True)
class Canary:
    source: str  # source key (matches adapter.name / SessionPool / breaker)
    name: str  # display name
    url: str  # public list page (or app-API endpoint) to probe
    count_cards: Callable[[str], int]  # real-card counter for this portal
    referer: str = "https://www.google.com/"
    method: str = "GET"  # "POST" for the ImmoScout mobile API (reads query, empty body)
    # Non-browser client identity for the standalone probe (e.g. the IS24 app);
    # None means the default browser disguise. In scheduler mode the live
    # SessionPool already supplies the right per-source identity.
    identity: http_session.Identity | None = None


CANARIES: list[Canary] = [
    Canary(
        "wg_gesucht",
        "WG-Gesucht",
        "https://www.wg-gesucht.de/wohnungen-in-Muenchen.90.2.1.0.html",
        wg_gesucht.count_list_cards,
    ),
    Canary(
        "kleinanzeigen",
        "Kleinanzeigen",
        "https://www.kleinanzeigen.de/s-wohnung-mieten/anzeige:angebote/"
        "sortierung:neueste/muenchen/c203l6411",
        kleinanzeigen.count_list_cards,
    ),
    Canary(
        "immowelt",
        "Immowelt",
        "https://www.immowelt.de/liste/muenchen/wohnungen/mieten",
        immowelt.count_list_cards,
    ),
    Canary(
        "immoscout",
        "ImmoScout24",
        immoscout._list_url("/de/bayern/muenchen"),
        immoscout.count_cards,
        method="POST",
        identity=http_session.IDENTITIES.get("immoscout"),
    ),
]

_BY_SOURCE: dict[str, Canary] = {c.source: c for c in CANARIES}


@dataclass
class PortalHealth:
    name: str  # display name
    source: str  # source key
    state: str  # one of OK / PORTAL_BLOCKED / SESSION_BLOCKED / ERROR
    detail: str  # short human label, e.g. "OK", "BLOCKED (403)"

    @property
    def ok(self) -> bool:
        return self.state == OK


def classify(name: str, source: str, status: int | None, body: str, count: int) -> PortalHealth:
    """Pure classification of one portal response (easy to unit-test)."""
    body = body or ""
    if status == 403:
        return PortalHealth(name, source, PORTAL_BLOCKED, "BLOCKED (403)")
    if status == 429:
        return PortalHealth(name, source, PORTAL_BLOCKED, "rate-limited (429)")
    if status == 200 and count > 0:
        return PortalHealth(name, source, OK, "OK")
    low = body.lower()
    if status == 200 and ("captcha" in low or "datadome" in low):
        return PortalHealth(name, source, PORTAL_BLOCKED, "challenge/captcha page")
    if status == 200:
        return PortalHealth(
            name, source, PORTAL_BLOCKED, "reachable, no listings (blocked or layout change?)"
        )
    return PortalHealth(name, source, ERROR, f"HTTP {status}")


async def _check_one(client, canary: Canary) -> PortalHealth:
    try:
        if canary.method == "POST":
            # App-API canary (IS24 mobile): empty body, the client already carries
            # the app's identity headers — no browser navigation headers.
            resp = await client.post(canary.url, data="")
        else:
            resp = await client.get(
                canary.url, headers=http_session.navigation_headers(referer=canary.referer)
            )
        status = getattr(resp, "status_code", None)
        body = resp.text
        try:
            count = canary.count_cards(body)
        except Exception:  # a parser blowing up shouldn't read as "reachable"
            count = 0
        return classify(canary.name, canary.source, status, body, count)
    except Exception as exc:  # network error, timeout, TLS, ...
        return PortalHealth(canary.name, canary.source, ERROR, f"error: {type(exc).__name__}")


def _downgrade_for_breaker(result: PortalHealth) -> PortalHealth:
    """A healthy list page can still hide a rate-limited detail API; surface that
    via the circuit breaker so /health doesn't read green while hydration is in
    cooldown."""
    if not result.ok:
        return result
    breaker = http_session.get_breaker(result.source)
    if breaker.is_open():
        mins = int(breaker.seconds_left() // 60) + 1
        return PortalHealth(
            result.name,
            result.source,
            PORTAL_BLOCKED,
            f"Liste OK, Detail-API gedrosselt (~{mins} min Cooldown)",
        )
    return result


async def probe(source: str, client=None) -> PortalHealth:
    """Probe a single portal (fresh client unless one is supplied) and classify.

    A fresh standalone client honors the canary's identity + per-source proxy, so a
    source on the app API (IS24) is probed as the app, not a browser."""
    canary = _BY_SOURCE[source]
    own = client is None
    c = (
        http_session.new_client(
            identity=canary.identity, proxy=http_session.proxy_for(source)
        )
        if own
        else client
    )
    try:
        return _downgrade_for_breaker(await _check_one(c, canary))
    finally:
        if own:
            await http_session.aclose(c)


async def check_all(get_client=None) -> list[PortalHealth]:
    """Probe every portal. `get_client(source)` supplies a client to reuse (the
    scheduler passes its SessionPool so probes share the live identity); when
    omitted, one fresh impersonating client is used for all probes."""
    own = get_client is None
    # Standalone: reuse one browser client across the browser canaries, but build a
    # dedicated identity client per app-API canary (e.g. IS24 mobile).
    shared = http_session.new_client() if own else None
    out: list[PortalHealth] = []
    try:
        for canary in CANARIES:
            if not own:
                client = get_client(canary.source)
            elif canary.identity is not None:
                client = http_session.new_client(
                    identity=canary.identity, proxy=http_session.proxy_for(canary.source)
                )
                try:
                    out.append(_downgrade_for_breaker(await _check_one(client, canary)))
                finally:
                    await http_session.aclose(client)
                continue
            else:
                client = shared
            out.append(_downgrade_for_breaker(await _check_one(client, canary)))
    finally:
        if own and shared is not None:
            await http_session.aclose(shared)
    return out


async def session_aware_report(get_client) -> list[PortalHealth]:
    """Probe via the live sessions, then for any portal that looks blocked re-probe
    with a *fresh* session: if the fresh one gets cards, the block is on our
    session (self-healing via rotation), not the portal. Used by `/health` so the
    manual check tells the same truth the watchdog acts on."""
    live = await check_all(get_client=get_client)
    out: list[PortalHealth] = []
    for r in live:
        if r.ok or r.state == ERROR:
            out.append(r)
            continue
        fresh = await probe(r.source)
        if fresh.ok:
            out.append(
                PortalHealth(
                    r.name,
                    r.source,
                    SESSION_BLOCKED,
                    "nur unsere Session blockiert (wird automatisch rotiert)",
                )
            )
        else:
            out.append(r)
    return out


def format_report(results: list[PortalHealth]) -> str:
    """Human-readable summary for the /health reply."""
    all_ok = all(r.ok for r in results)
    header = "🟢 Alle Portale erreichbar" if all_ok else "🔴 Achtung: Problem erkannt"
    lines = [f"{'✅' if r.ok else '⛔'} {r.name}: {r.detail}" for r in results]
    return header + "\n" + "\n".join(lines)
