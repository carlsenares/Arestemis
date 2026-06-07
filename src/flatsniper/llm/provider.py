"""Groq LLM provider (OpenAI-compatible).

The single place the LLM backend is touched, so swapping providers is a one-file
change. All prompt strings live here. Two capabilities:

  - classify_empty_2p_wg(listing, secrets) -> bool
      Reads a WG listing's text and decides whether it's an *empty* 2-person WG
      the user would fully control (i.e. take the whole flat and pick the
      flatmate) vs. an existing shared flat with current occupants to join.
  - draft_application(listing, about_me, secrets) -> str
      Writes a warm, concise application in GERMAN, tailored to the listing.

Functions are synchronous (the Groq client is sync); async callers should wrap
them in asyncio.to_thread. Model + key come from Secrets (env).
"""

from __future__ import annotations

import json
import logging

from groq import Groq

from ..config import ApplicationConfig, Secrets
from ..models import Listing

log = logging.getLogger(__name__)


def _client(secrets: Secrets) -> Groq:
    return Groq(api_key=secrets.groq_api_key)


def _location_text(listing: Listing) -> str:
    raw = listing.raw or {}
    fields = [raw.get("city_name"), raw.get("district_custom"), raw.get("postcode"), listing.title]
    return " ".join(str(f) for f in fields if f).lower()


def is_garching(listing: Listing) -> bool:
    """True if the listing is in Garching (b. München) — by name or 85748 PLZ."""
    loc = _location_text(listing)
    return "garching" in loc or "85748" in loc


def _listing_blurb(listing: Listing) -> str:
    """Compact, human-readable summary of a listing for prompting."""
    bits = [f"Titel: {listing.title}"]
    if listing.rent_warm is not None:
        bits.append(f"Warmmiete: {listing.rent_warm} €")
    if listing.sqm is not None:
        bits.append(f"Größe: {listing.sqm:g} m²")
    if listing.rooms:
        bits.append(f"Zimmer: {listing.rooms:g}")
    if listing.is_wg:
        bits.append(f"WG-Größe (laut Anzeige): {listing.wg_size or '?'} Personen")
        # Room vs. flat size is the strongest "per-room rental" tell: a tiny
        # advertised room inside a much larger flat means a single room is being
        # sublet, not the whole empty flat.
        raw = listing.raw or {}
        room = raw.get("property_size")
        flat = raw.get("flatshare_property_size")
        if room:
            bits.append(f"Beworbene Zimmergröße: {room} m²")
        if flat:
            bits.append(f"Gesamte Wohnungsgröße: {flat} m²")
    raw = listing.raw or {}
    loc = raw.get("city_name") or raw.get("district_custom")
    if loc:
        bits.append(f"Ort: {loc}")
    desc = (listing.description or "").strip()
    if desc:
        bits.append(f"\nBeschreibung:\n{desc[:2500]}")
    return "\n".join(bits)


# --- Classification -------------------------------------------------------

_CLASSIFY_SYSTEM = (
    "Du bist ein sehr strenger Klassifikator für WG-Anzeigen auf WG-Gesucht. "
    "Der Nutzer sucht AUSSCHLIESSLICH eine LEERE 2er-WG: die GESAMTE Wohnung "
    "ist frei, sie ist für genau zwei Personen ausgelegt, es wohnt aktuell "
    "NIEMAND darin, und der Nutzer übernimmt die ganze Wohnung und sucht sich "
    "die zweite Person selbst aus. "
    "Antworte mit false, wenn AUCH NUR EINES zutrifft: "
    "(a) nur ein einzelnes Zimmer in einer Wohnung wird vermietet (die "
    "beworbene Zimmergröße ist viel kleiner als die Wohnungsgröße); "
    "(b) es wohnen bereits Personen in der Wohnung oder es wird eine bestehende "
    "WG fortgeführt; "
    "(c) die WG ist für mehr als zwei Personen; "
    "(d) es handelt sich um einen gewerblichen Anbieter, Co-Living, möblierte "
    "Einzelzimmer-Vermietung oder Anbieter, die mehrere Zimmer/Wohnungen "
    "verwalten (z. B. Formulierungen wie 'our flats', 'our rooms', 'each room', "
    "'co-living', 'shared apartment', 'Zimmer X von Y'); "
    "(e) der Mitbewohner kann nicht frei gewählt werden. "
    "Im Zweifel antworte mit false. "
    'Antworte ausschließlich mit JSON: {"empty_2p_wg": true|false, "reason": "<kurz>"}.'
)


def classify_empty_2p_wg(listing: Listing, secrets: Secrets, *, client: Groq | None = None) -> bool:
    """True if the listing is an empty 2-person WG the user would fully control."""
    client = client or _client(secrets)
    try:
        resp = client.chat.completions.create(
            model=secrets.llm_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _CLASSIFY_SYSTEM},
                {"role": "user", "content": _listing_blurb(listing)},
            ],
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        return bool(data.get("empty_2p_wg", False))
    except Exception:
        log.exception("classify_empty_2p_wg failed for %s; defaulting to False", listing.external_id)
        return False


# --- Application drafting -------------------------------------------------
#
# Deterministic on purpose: the user supplies the exact German copy in
# config.application (so it reads like them, not like an LLM, and no personal
# data is hardcoded). The only variation is the Garching sentence, included only
# for Garching listings. No model call — instant and predictable.


def draft_application(listing: Listing, application: ApplicationConfig) -> str:
    """Assemble the German application from the configured template.

    intro (+ Garching sentence for Garching listings) as the first paragraph,
    then the outro paragraph.
    """
    first = application.intro.strip()
    if is_garching(listing) and application.garching_line.strip():
        first = f"{first} {application.garching_line.strip()}".strip()
    paragraphs = [p for p in (first, application.outro.strip()) if p]
    return "\n\n".join(paragraphs)
