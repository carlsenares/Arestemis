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

from ..config import Secrets
from ..models import Listing

log = logging.getLogger(__name__)


def _client(secrets: Secrets) -> Groq:
    return Groq(api_key=secrets.groq_api_key)


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
        bits.append(f"WG-Größe: {listing.wg_size or '?'} Personen")
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
    "Du bist ein präziser Klassifikator für WG-Anzeigen auf WG-Gesucht. "
    "Ein Nutzer sucht ausschließlich eine LEERE 2er-WG: eine Wohnung, die als "
    "WG für genau zwei Personen ausgeschrieben ist und in der noch NIEMAND wohnt, "
    "sodass der Nutzer die Wohnung faktisch allein übernimmt und sich den/die "
    "zweite/n Mitbewohner/in selbst aussucht. "
    "Das trifft NICHT zu, wenn bereits jemand in der Wohnung wohnt und nur ein "
    "Zimmer in einer bestehenden WG frei wird, oder wenn die WG mehr als zwei "
    "Personen hat. "
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

_DRAFT_SYSTEM = (
    "Du schreibst im Auftrag eines Wohnungssuchenden eine kurze, herzliche und "
    "überzeugende Bewerbung auf DEUTSCH für eine konkrete Wohnungs- bzw. "
    "WG-Anzeige. Sprich die anbietende Person höflich mit 'Hallo' an. "
    "Gehe knapp auf die konkrete Anzeige ein (Lage, Größe oder Art der Wohnung) "
    "und hebe relevante Stärken des Bewerbers hervor (Zuverlässigkeit, "
    "gesicherte Mietzahlung, Sauberkeit, langfristiges Interesse). "
    "Halte dich an 90–160 Wörter, wirke natürlich und nicht wie eine Vorlage, "
    "vermeide Übertreibungen und Emojis. Gib NUR den Bewerbungstext aus, ohne "
    "Betreffzeile und ohne Erklärungen."
)


def draft_application(
    listing: Listing, about_me: str, secrets: Secrets, *, client: Groq | None = None
) -> str:
    """Return a tailored German application text for the listing."""
    client = client or _client(secrets)
    user = (
        "ANZEIGE:\n"
        f"{_listing_blurb(listing)}\n\n"
        "ÜBER MICH (Bewerber):\n"
        f"{about_me.strip()}\n\n"
        "Schreibe jetzt die Bewerbung."
    )
    resp = client.chat.completions.create(
        model=secrets.llm_model,
        temperature=0.5,
        messages=[
            {"role": "system", "content": _DRAFT_SYSTEM},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "").strip()
