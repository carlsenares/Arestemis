"""Tests for the per-source scrape watchdog (offline)."""

from __future__ import annotations

import asyncio

from flatsniper.monitor import ERROR, PORTAL, SESSION, ScrapeWatch


class _FakeSessions:
    def __init__(self):
        self.rotated: list[str] = []

    async def rotate(self, source):
        self.rotated.append(source)


class _Clock:
    """Controllable monotonic clock for the confirmation-grace tests."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _harness(diag_result):
    sessions = _FakeSessions()
    alerts: list[str] = []

    async def diagnose(source):
        return diag_result

    return sessions, alerts, diagnose


def _observe(watch, source, count, sessions, alert_list, diagnose, display="X"):
    async def alert(text):
        alert_list.append(text)

    asyncio.run(watch.observe(
        source, display, count, sessions=sessions, alert=alert, diagnose=diagnose
    ))


# --- Detection threshold ---------------------------------------------------

def test_rides_out_short_outages_below_threshold():
    sessions, alerts, diagnose = _harness((SESSION, "x"))
    watch = ScrapeWatch(streak_threshold=3)
    # Two zero cycles (below threshold) -> no action yet.
    _observe(watch, "ka", 0, sessions, alerts, diagnose)
    _observe(watch, "ka", 0, sessions, alerts, diagnose)
    assert alerts == [] and sessions.rotated == []
    assert watch.state_of("ka").pending is False


# --- Confirmation grace: a short outage that heals stays silent ------------

def test_outage_that_heals_within_grace_is_silent():
    # The whole point: crossing the threshold self-heals but does NOT alert; if
    # cards return before the grace elapses, no blocked *or* reachable message.
    clock = _Clock()
    sessions, alerts, diagnose = _harness((PORTAL, "BLOCKED (403)"))
    watch = ScrapeWatch(streak_threshold=2, confirm_grace_seconds=300, clock=clock)
    _observe(watch, "iw", 0, sessions, alerts, diagnose, display="Immowelt")
    _observe(watch, "iw", 0, sessions, alerts, diagnose, display="Immowelt")  # -> pending
    assert alerts == [] and watch.state_of("iw").pending is True
    clock.advance(120)  # still inside the grace window
    _observe(watch, "iw", 0, sessions, alerts, diagnose, display="Immowelt")
    assert alerts == []
    # Cards come back before the grace expires -> cancel silently.
    _observe(watch, "iw", 7, sessions, alerts, diagnose, display="Immowelt")
    st = watch.state_of("iw")
    assert alerts == [] and st.pending is False and st.announced is False


def test_session_block_heals_silently_during_grace_then_recovers_quietly():
    # A session block rotates itself during the grace window without ever pinging
    # the user, and if it comes back inside the window it stays completely silent.
    clock = _Clock()
    sessions, alerts, diagnose = _harness((SESSION, "stripped"))
    watch = ScrapeWatch(
        streak_threshold=1, reheal_every=2, confirm_grace_seconds=600, clock=clock
    )
    _observe(watch, "ka", 0, sessions, alerts, diagnose)  # -> pending + first rotation
    assert sessions.rotated == ["ka"] and alerts == []
    for _ in range(4):  # still dark, under grace -> silent re-rotations, no alert
        clock.advance(30)
        _observe(watch, "ka", 0, sessions, alerts, diagnose)
    assert alerts == [] and len(sessions.rotated) >= 2
    _observe(watch, "ka", 9, sessions, alerts, diagnose)  # recovered inside grace
    assert alerts == [] and watch.state_of("ka").pending is False


# --- Once the grace elapses, we do alert (and later recover) ---------------

def test_break_fires_after_grace_and_then_recovers():
    clock = _Clock()
    sessions, alerts, diagnose = _harness((PORTAL, "BLOCKED (403)"))
    watch = ScrapeWatch(
        streak_threshold=2, recovery_cycles=1, confirm_grace_seconds=300, clock=clock
    )
    _observe(watch, "iw", 0, sessions, alerts, diagnose, display="Immowelt")
    _observe(watch, "iw", 0, sessions, alerts, diagnose, display="Immowelt")  # -> pending
    assert alerts == []
    clock.advance(300)  # grace elapsed
    _observe(watch, "iw", 0, sessions, alerts, diagnose, display="Immowelt")  # still down
    assert len(alerts) == 1 and alerts[0].startswith("⛔") and "403" in alerts[0]
    assert watch.state_of("iw").announced is True
    # Cards return -> exactly one recovery alert.
    _observe(watch, "iw", 5, sessions, alerts, diagnose, display="Immowelt")
    assert len(alerts) == 2 and alerts[1].startswith("✅")
    assert watch.state_of("iw").announced is False


def test_session_break_alert_and_recovery_after_grace():
    # With a zero-length grace the alert fires on the threshold cycle (kept for the
    # message-content contract: session break rotates once and names the session).
    sessions, alerts, diagnose = _harness((SESSION, "stripped"))
    watch = ScrapeWatch(streak_threshold=3, recovery_cycles=1, confirm_grace_seconds=0)
    for _ in range(3):  # hit the threshold
        _observe(watch, "ka", 0, sessions, alerts, diagnose, display="Kleinanzeigen")
    assert sessions.rotated == ["ka"]
    assert len(alerts) == 1 and "Session" in alerts[0] and alerts[0].startswith("⚠️")

    _observe(watch, "ka", 17, sessions, alerts, diagnose, display="Kleinanzeigen")
    assert len(alerts) == 2 and alerts[1].startswith("✅")
    assert watch.state_of("ka").announced is False


def test_portal_block_notifies_but_does_not_rotate():
    sessions, alerts, diagnose = _harness((PORTAL, "BLOCKED (403)"))
    watch = ScrapeWatch(streak_threshold=2, confirm_grace_seconds=0)
    _observe(watch, "iw", 0, sessions, alerts, diagnose, display="Immowelt")
    _observe(watch, "iw", 0, sessions, alerts, diagnose, display="Immowelt")
    assert sessions.rotated == []  # not self-healable
    assert len(alerts) == 1 and alerts[0].startswith("⛔") and "403" in alerts[0]


def test_error_notifies_and_recovers():
    sessions, alerts, diagnose = _harness((ERROR, "error: TimeoutException"))
    watch = ScrapeWatch(streak_threshold=2, recovery_cycles=1, confirm_grace_seconds=0)
    _observe(watch, "wg", 0, sessions, alerts, diagnose, display="WG-Gesucht")
    _observe(watch, "wg", None, sessions, alerts, diagnose, display="WG-Gesucht")  # None == no count
    assert sessions.rotated == []
    assert len(alerts) == 1 and "fehlgeschlagen" in alerts[0]
    _observe(watch, "wg", 5, sessions, alerts, diagnose, display="WG-Gesucht")
    assert alerts[-1].startswith("✅")


def test_no_duplicate_break_alerts_while_still_down():
    sessions, alerts, diagnose = _harness((PORTAL, "BLOCKED (403)"))
    watch = ScrapeWatch(streak_threshold=1, confirm_grace_seconds=0)
    for _ in range(5):
        _observe(watch, "iw", 0, sessions, alerts, diagnose)
    assert len(alerts) == 1  # only the transition into trouble alerts


def test_flapping_does_not_spam_alerts():
    # A portal that recovers for a single cycle then dies again must NOT fire a
    # break/recovery pair each flap — only the first break, until it's durably back.
    sessions, alerts, diagnose = _harness((SESSION, "x"))
    watch = ScrapeWatch(
        streak_threshold=2, recovery_cycles=2, reheal_every=99, confirm_grace_seconds=0
    )
    _observe(watch, "ka", 0, sessions, alerts, diagnose)
    _observe(watch, "ka", 0, sessions, alerts, diagnose)  # trip -> 1 alert
    assert len(alerts) == 1
    # Flap: good, bad, good, bad — never two good in a row -> no recovery alert.
    for count in (9, 0, 9, 0, 9, 0):
        _observe(watch, "ka", count, sessions, alerts, diagnose)
    assert len(alerts) == 1 and watch.state_of("ka").announced is True
    # Durably back: two good cycles in a row -> exactly one recovery alert.
    _observe(watch, "ka", 9, sessions, alerts, diagnose)
    _observe(watch, "ka", 9, sessions, alerts, diagnose)
    assert len(alerts) == 2 and alerts[1].startswith("✅")
    assert watch.state_of("ka").announced is False


def test_persistent_session_block_re_rotates_silently_on_cooldown():
    sessions, alerts, diagnose = _harness((SESSION, "x"))
    watch = ScrapeWatch(streak_threshold=1, reheal_every=3, confirm_grace_seconds=0)
    # Cycle 1: trip + first rotation + alert.
    _observe(watch, "ka", 0, sessions, alerts, diagnose)
    assert sessions.rotated == ["ka"] and len(alerts) == 1
    # Still down for several cycles: re-rotate once per `reheal_every`, silently.
    for _ in range(3):
        _observe(watch, "ka", 0, sessions, alerts, diagnose)
    assert sessions.rotated == ["ka", "ka"]  # one extra rotation after cooldown
    assert len(alerts) == 1  # no new user-facing alerts
