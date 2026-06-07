"""Portal reachability / ban detection.

Answers one question on demand or on a schedule: *can we still reach each
portal, or are we being blocked?* Used by the Telegram `/health` command (manual
check from the phone) and by the scheduler's watchdog (proactive alert the
moment a portal starts blocking us).

Each portal has a "canary": its public list URL plus a marker substring that
only appears when real listings render. Classification:
  - 200 + marker present                 -> OK
  - 403                                   -> BLOCKED (hard)
  - 429                                   -> rate-limited (soft; back off)
  - 200 + captcha/datadome, no listings  -> challenge/captcha page (soft block)
  - 200 but no listings                   -> reachable but no results (layout?)
  - other / exception                     -> error

NB: some healthy pages (WG-Gesucht) embed a captcha *widget*, so we key "OK" on
the positive listing marker being present, not on the absence of "captcha".
"""

from __future__ import annotations

from dataclasses import dataclass

from .sources import http_session

# (source_key, display_name, canary list URL, marker that means real listings)
CANARIES: list[tuple[str, str, str, str]] = [
    (
        "wg_gesucht",
        "WG-Gesucht",
        "https://www.wg-gesucht.de/wohnungen-in-Muenchen.90.2.1.0.html",
        "offer_list_item",
    ),
    (
        "kleinanzeigen",
        "Kleinanzeigen",
        "https://www.kleinanzeigen.de/s-wohnung-mieten/anzeige:angebote/"
        "sortierung:neueste/muenchen/c203l6411",
        "aditem",
    ),
    (
        "immowelt",
        "Immowelt",
        "https://www.immowelt.de/liste/muenchen/wohnungen/mieten",
        "serp-core-classified-card-testid",
    ),
]


@dataclass
class PortalHealth:
    name: str
    ok: bool
    detail: str  # short human label, e.g. "OK", "BLOCKED (403)"


def classify(name: str, status: int | None, body: str, marker: str) -> PortalHealth:
    """Pure classification of one portal response (easy to unit-test)."""
    body = body or ""
    if status == 200 and marker in body:
        return PortalHealth(name, True, "OK")
    if status == 403:
        return PortalHealth(name, False, "BLOCKED (403)")
    if status == 429:
        return PortalHealth(name, False, "rate-limited (429)")
    low = body.lower()
    if status == 200 and ("captcha" in low or "datadome" in low):
        return PortalHealth(name, False, "challenge/captcha page")
    if status == 200:
        return PortalHealth(name, False, "reachable, no listings (layout change?)")
    return PortalHealth(name, False, f"HTTP {status}")


async def _check_one(client, name: str, url: str, marker: str) -> PortalHealth:
    try:
        resp = await client.get(
            url, headers=http_session.navigation_headers(referer="https://www.google.com/")
        )
        return classify(name, getattr(resp, "status_code", None), resp.text, marker)
    except Exception as exc:  # network error, timeout, TLS, ...
        return PortalHealth(name, False, f"error: {type(exc).__name__}")


async def check_all(get_client=None) -> list[PortalHealth]:
    """Probe every portal. `get_client(source_key)` supplies a client to reuse
    (the scheduler passes its SessionPool so probes share the live identity);
    when omitted, one fresh impersonating client is used for all probes."""
    own = get_client is None
    shared = http_session.new_client() if own else None
    out: list[PortalHealth] = []
    try:
        for source, display, url, marker in CANARIES:
            client = shared if own else get_client(source)
            out.append(await _check_one(client, display, url, marker))
    finally:
        if own and shared is not None:
            await http_session.aclose(shared)
    return out


def format_report(results: list[PortalHealth]) -> str:
    """Human-readable summary for the /health reply."""
    all_ok = all(r.ok for r in results)
    header = "🟢 Alle Portale erreichbar" if all_ok else "🔴 Achtung: Problem erkannt"
    lines = [f"{'✅' if r.ok else '⛔'} {r.name}: {r.detail}" for r in results]
    return header + "\n" + "\n".join(lines)


def transitions(prev: dict[str, bool], results: list[PortalHealth]) -> list[str]:
    """Alert messages for state changes vs `prev` (for the proactive watchdog).

    Alerts when a portal newly breaks (or is already broken on the first check)
    and when it recovers — never on a steady state, to avoid spam.
    """
    msgs: list[str] = []
    for r in results:
        was = prev.get(r.name)
        if r.ok and was is False:
            msgs.append(f"✅ {r.name} ist wieder erreichbar.")
        elif not r.ok and was in (True, None):
            msgs.append(f"⚠️ {r.name}: {r.detail} — möglicherweise blockiert.")
    return msgs
