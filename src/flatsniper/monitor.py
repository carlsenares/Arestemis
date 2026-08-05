"""Per-source scrape watchdog: notice when a source stops returning cards,
classify *why*, self-heal the fixable case, and notify on break + recovery.

The faithful "are we still getting listings?" signal is the scraper's own raw
enumeration count each cycle — not a side probe that may hit a different endpoint
and disagree (that mismatch is exactly what let a 4-hour Kleinanzeigen outage go
unnoticed while /health stayed green). So the scheduler feeds this watch the
per-source `enumerated` count every cycle. When a source returns zero cards for
`streak_threshold` cycles in a row, we diagnose the cause once with a fresh probe
(see `health.probe`) and react:

  * **session block** — the portal serves *our* long-lived session empty pages
    while a fresh session still gets cards. Self-healable: rotate the session and
    retry. Recovery (cards again) sends an all-clear.
  * **portal block** — a fresh session is blocked too (403/429/captcha/stripped).
    Can't auto-fix; notify so the user knows. Recovery sends an all-clear.
  * **error** — network/TLS/timeout. Likely transient; notify and keep trying;
    recovery sends an all-clear.

To avoid alert spam we notify only on the *transition* into trouble and back out,
and two extra guards suppress the flapping that used to fire "blocked → reachable"
pairs minutes apart:

  * **Confirmation grace.** Crossing `streak_threshold` does not alert. It starts
    a `confirm_grace_seconds` hold (default 7 min) during which we self-heal
    silently. The "blocked" message is sent only if the source is *still* down
    when the grace elapses; if cards come back inside the window the break is
    cancelled silently — no blocked message, and therefore no recovery message.
    So a short outage that heals within a few minutes stays entirely quiet.
  * **Durable recovery.** Once a break has been announced, the all-clear requires
    `recovery_cycles` good cycles in a row — a portal that flaps (one good cycle
    right after each rotation, then dark again) stays in the already-announced
    broken state, silently re-rotating, instead of firing a break/recovery pair
    every few minutes.

A persistent session block keeps silently re-rotating on a short cooldown in case
the first rotation drew another flagged identity (rotation adds no extra requests
— it only swaps identity on the next scheduled poll, so retrying often is cheap
and polite).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

SESSION = "session"
PORTAL = "portal"
ERROR = "error"

# Default hold between detecting a source is down and actually alerting the user,
# so a block that self-heals within a few minutes never produces a notification.
DEFAULT_CONFIRM_GRACE_SECONDS = 7 * 60


def break_message(display: str, cause: str, detail: str) -> str:
    if cause == SESSION:
        return (
            f"⚠️ {display}: liefert keine Anzeigen mehr — vermutlich ist *unsere "
            f"Session* blockiert. Ich rotiere die Session und versuche es weiter."
        )
    if cause == PORTAL:
        return (
            f"⛔ {display}: liefert keine Anzeigen mehr — {detail}. "
            f"Das Portal blockiert uns; das kann ich nicht automatisch beheben."
        )
    return (
        f"⚠️ {display}: Abruf fehlgeschlagen ({detail}). "
        f"Vermutlich vorübergehend; ich versuche es weiter."
    )


def recovery_message(display: str, cause: str) -> str:
    if cause == SESSION:
        return f"✅ {display}: wieder erreichbar — Session rotiert, Anzeigen kommen wieder."
    return f"✅ {display}: wieder erreichbar."


@dataclass
class _SourceState:
    zero_streak: int = 0  # consecutive cycles with no cards
    good_streak: int = 0  # consecutive cycles WITH cards while broken (for recovery)
    pending: bool = False  # down & confirmed, holding the alert during the grace window
    announced: bool = False  # break alert sent; awaiting durable recovery
    pending_since: float = 0.0  # clock() reading when the grace hold started
    cause: str | None = None  # SESSION / PORTAL / ERROR while down
    detail: str = ""  # human detail for the break message
    heal_cooldown: int = 0  # cycles until the next silent re-rotation attempt

    def reset(self) -> None:
        """Back to fully healthy."""
        self.zero_streak = 0
        self.good_streak = 0
        self.pending = False
        self.announced = False
        self.pending_since = 0.0
        self.cause = None
        self.detail = ""
        self.heal_cooldown = 0


async def _safe_alert(alert, text: str) -> None:
    try:
        await alert(text)
    except Exception:
        log.exception("scrape-watch: alert failed")


class ScrapeWatch:
    """Tracks per-source card flow and drives detect → classify → heal → notify.

    `streak_threshold` cycles of zero cards before we start watching a source as
    down (rides out transient blips). `confirm_grace_seconds` is then held before
    the "blocked" alert actually fires, so an outage that heals within that window
    never notifies. `reheal_every` cycles between silent re-rotations while a
    session block persists. `clock` is injectable for deterministic tests.
    """

    def __init__(
        self,
        *,
        streak_threshold: int = 3,
        recovery_cycles: int = 2,
        reheal_every: int = 5,
        confirm_grace_seconds: float = DEFAULT_CONFIRM_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._states: dict[str, _SourceState] = {}
        self.streak_threshold = streak_threshold
        self.recovery_cycles = recovery_cycles
        self.reheal_every = reheal_every
        self.confirm_grace_seconds = confirm_grace_seconds
        self.clock = clock

    def state_of(self, source: str) -> _SourceState:
        return self._states.setdefault(source, _SourceState())

    async def _maybe_reheal(self, st: _SourceState, source: str, sessions) -> None:
        """Silent self-heal for a persisting session block: re-rotate once the
        per-source cooldown elapses (cheap — only swaps identity on the next poll)."""
        if st.cause != SESSION:
            return
        st.heal_cooldown -= 1
        if st.heal_cooldown <= 0:
            await sessions.rotate(source)
            st.heal_cooldown = self.reheal_every
            log.info("scrape-watch: re-rotated %s session (still no cards)", source)

    async def observe(
        self, source: str, display: str, enumerated: int | None, *, sessions, alert, diagnose
    ) -> None:
        """Record one cycle's card count for `source` and react.

        `enumerated` is the raw card count this cycle (None if the adapter never
        reported one, e.g. it raised). `diagnose(source)` returns (cause, detail).
        """
        st = self.state_of(source)
        got_cards = enumerated is not None and enumerated > 0

        # --- Healthy: watch for a source going dark, then enter the grace hold. ---
        if not st.pending and not st.announced:
            if got_cards:
                st.zero_streak = 0
                return
            st.zero_streak += 1
            if st.zero_streak < self.streak_threshold:
                return  # not yet sure — ride out blips
            cause, detail = await diagnose(source)
            st.pending = True
            st.cause = cause
            st.detail = detail
            st.pending_since = self.clock()
            st.good_streak = 0
            log.warning(
                "scrape-watch: %s down — cause=%s (%s); holding alert up to %ds",
                source, cause, detail, int(self.confirm_grace_seconds),
            )
            if cause == SESSION:
                await sessions.rotate(source)
                st.heal_cooldown = self.reheal_every
            # Fall through: a zero-length grace should still alert this cycle.

        # --- Pending: down & confirmed, holding the alert during the grace window. ---
        if st.pending:
            if got_cards:
                st.reset()  # healed within the grace window -> stay silent entirely
                return
            await self._maybe_reheal(st, source, sessions)
            if self.clock() - st.pending_since >= self.confirm_grace_seconds:
                st.pending = False
                st.announced = True
                st.good_streak = 0
                await _safe_alert(alert, break_message(display, st.cause, st.detail))
            return

        # --- Announced-broken: wait for durable recovery, else keep healing. ---
        if got_cards:
            st.good_streak += 1
            st.zero_streak = 0
            if st.good_streak >= self.recovery_cycles:  # durably back -> all-clear
                cause = st.cause
                st.reset()
                await _safe_alert(alert, recovery_message(display, cause))
            return

        # Still no cards: a one-off good cycle didn't stick — keep healing silently.
        st.good_streak = 0
        await self._maybe_reheal(st, source, sessions)
