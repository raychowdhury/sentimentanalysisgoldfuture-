"""
Event blackout gate.

Given an anchor date + list of events, decide whether the anchor sits inside
a blackout window around any high-impact US macro event.

Config:
  EVENT_GATE_ENABLED          master switch
  EVENT_BLACKOUT_DAYS_BEFORE  block signals N days before event
  EVENT_BLACKOUT_DAYS_AFTER   block signals N days after event
  EVENT_BLACKOUT_TYPES        event kinds that trigger a block

Daily granularity. Swap to hour-based when engine goes intraday.
"""

from __future__ import annotations

from datetime import date, timedelta

import config
from events.calendar import Event, get_events

# Higher rank wins when multiple events overlap the same day. FOMC rate
# decisions move gold hardest; inflation prints (CPI/PCE) next; jobs (NFP)
# next; unclassified FF rows last. Tie-breaker is date-distance to `at`.
_KIND_RANK: dict[str, int] = {
    "FOMC": 5,
    "CPI":  4,
    "PCE":  3,
    "NFP":  2,
    "FF":   1,
}


def is_blackout(
    at: date,
    events: list[Event] | None = None,
) -> tuple[bool, str | None]:
    """
    Returns (blocked, reason).
    reason example: "pre-FOMC (2026-04-29)" or None if clear.
    When multiple events overlap, reports the highest-ranked one.
    """
    if not getattr(config, "EVENT_GATE_ENABLED", False):
        return False, None

    before = int(getattr(config, "EVENT_BLACKOUT_DAYS_BEFORE", 1))
    after  = int(getattr(config, "EVENT_BLACKOUT_DAYS_AFTER", 1))
    allowed = set(getattr(
        config, "EVENT_BLACKOUT_TYPES",
        ["FOMC", "CPI", "NFP", "PCE"],
    ))

    if events is None:
        events = get_events(
            at - timedelta(days=before + 1),
            at + timedelta(days=after + 1),
        )

    matches: list[Event] = []
    for ev in events:
        if ev.kind not in allowed:
            continue
        win_start = ev.date - timedelta(days=before)
        win_end   = ev.date + timedelta(days=after)
        if win_start <= at <= win_end:
            matches.append(ev)

    if not matches:
        return False, None

    # Highest-ranked kind first; ties broken by closeness to `at`.
    matches.sort(key=lambda e: (-_KIND_RANK.get(e.kind, 0), abs((e.date - at).days)))
    ev = matches[0]
    if at < ev.date:
        side = "pre"
    elif at > ev.date:
        side = "post"
    else:
        side = "on"
    return True, f"{side}-{ev.kind} ({ev.date.isoformat()})"
