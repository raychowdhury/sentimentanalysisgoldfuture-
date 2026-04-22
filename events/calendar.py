"""
Economic calendar — high-impact US events relevant to gold.

Sourced from:
  - federalreserve.gov/monetarypolicy/fomccalendars.htm  (FOMC)
  - bls.gov/schedule/news_release/cpi.htm                (CPI)
  - bea.gov/news/schedule                                (PCE)
  - Derived — first Friday of month                     (NFP)

Dates are 1-day granularity. When the engine moves to intraday bars, swap in
8:30 AM / 2:00 PM ET timestamps. Refresh FOMC / CPI / PCE lists yearly from
the official schedules above; NFP is rule-derived and needs no refresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Event:
    date: date
    kind: str      # "FOMC" | "CPI" | "NFP" | "PCE"
    label: str


_FOMC_DATES: list[tuple[int, int, int]] = [
    (2020, 1, 29), (2020, 3, 15), (2020, 4, 29), (2020, 6, 10),
    (2020, 7, 29), (2020, 9, 16), (2020, 11, 5), (2020, 12, 16),
    (2021, 1, 27), (2021, 3, 17), (2021, 4, 28), (2021, 6, 16),
    (2021, 7, 28), (2021, 9, 22), (2021, 11, 3), (2021, 12, 15),
    (2022, 1, 26), (2022, 3, 16), (2022, 5, 4),  (2022, 6, 15),
    (2022, 7, 27), (2022, 9, 21), (2022, 11, 2), (2022, 12, 14),
    (2023, 2, 1),  (2023, 3, 22), (2023, 5, 3),  (2023, 6, 14),
    (2023, 7, 26), (2023, 9, 20), (2023, 11, 1), (2023, 12, 13),
    (2024, 1, 31), (2024, 3, 20), (2024, 5, 1),  (2024, 6, 12),
    (2024, 7, 31), (2024, 9, 18), (2024, 11, 7), (2024, 12, 18),
    (2025, 1, 29), (2025, 3, 19), (2025, 5, 7),  (2025, 6, 18),
    (2025, 7, 30), (2025, 9, 17), (2025, 10, 29),(2025, 12, 10),
    (2026, 1, 28), (2026, 3, 18), (2026, 4, 29), (2026, 6, 17),
    (2026, 7, 29), (2026, 9, 16), (2026, 10, 28),(2026, 12, 16),
]

_CPI_DATES: list[tuple[int, int, int]] = [
    (2020, 1, 14), (2020, 2, 13), (2020, 3, 11), (2020, 4, 10),
    (2020, 5, 12), (2020, 6, 10), (2020, 7, 14), (2020, 8, 12),
    (2020, 9, 11), (2020, 10, 13),(2020, 11, 12),(2020, 12, 10),
    (2021, 1, 13), (2021, 2, 10), (2021, 3, 10), (2021, 4, 13),
    (2021, 5, 12), (2021, 6, 10), (2021, 7, 13), (2021, 8, 11),
    (2021, 9, 14), (2021, 10, 13),(2021, 11, 10),(2021, 12, 10),
    (2022, 1, 12), (2022, 2, 10), (2022, 3, 10), (2022, 4, 12),
    (2022, 5, 11), (2022, 6, 10), (2022, 7, 13), (2022, 8, 10),
    (2022, 9, 13), (2022, 10, 13),(2022, 11, 10),(2022, 12, 13),
    (2023, 1, 12), (2023, 2, 14), (2023, 3, 14), (2023, 4, 12),
    (2023, 5, 10), (2023, 6, 13), (2023, 7, 12), (2023, 8, 10),
    (2023, 9, 13), (2023, 10, 12),(2023, 11, 14),(2023, 12, 12),
    (2024, 1, 11), (2024, 2, 13), (2024, 3, 12), (2024, 4, 10),
    (2024, 5, 15), (2024, 6, 12), (2024, 7, 11), (2024, 8, 14),
    (2024, 9, 11), (2024, 10, 10),(2024, 11, 13),(2024, 12, 11),
    (2025, 1, 15), (2025, 2, 12), (2025, 3, 12), (2025, 4, 10),
    (2025, 5, 13), (2025, 6, 11), (2025, 7, 15), (2025, 8, 12),
    (2025, 9, 11), (2025, 10, 15),(2025, 11, 13),(2025, 12, 10),
    (2026, 1, 14), (2026, 2, 11), (2026, 3, 11), (2026, 4, 14),
    (2026, 5, 12), (2026, 6, 10), (2026, 7, 15), (2026, 8, 12),
    (2026, 9, 10), (2026, 10, 15),(2026, 11, 13),(2026, 12, 10),
]

_PCE_DATES: list[tuple[int, int, int]] = [
    (2020, 1, 31), (2020, 2, 28), (2020, 3, 27), (2020, 4, 30),
    (2020, 5, 29), (2020, 6, 26), (2020, 7, 31), (2020, 8, 28),
    (2020, 9, 25), (2020, 10, 30),(2020, 11, 25),(2020, 12, 23),
    (2021, 1, 29), (2021, 2, 26), (2021, 3, 26), (2021, 4, 30),
    (2021, 5, 28), (2021, 6, 25), (2021, 7, 30), (2021, 8, 27),
    (2021, 9, 24), (2021, 10, 29),(2021, 11, 24),(2021, 12, 23),
    (2022, 1, 28), (2022, 2, 25), (2022, 3, 31), (2022, 4, 29),
    (2022, 5, 27), (2022, 6, 30), (2022, 7, 29), (2022, 8, 26),
    (2022, 9, 30), (2022, 10, 28),(2022, 11, 23),(2022, 12, 23),
    (2023, 1, 27), (2023, 2, 24), (2023, 3, 31), (2023, 4, 28),
    (2023, 5, 26), (2023, 6, 30), (2023, 7, 28), (2023, 8, 31),
    (2023, 9, 29), (2023, 10, 27),(2023, 11, 30),(2023, 12, 22),
    (2024, 1, 26), (2024, 2, 29), (2024, 3, 29), (2024, 4, 26),
    (2024, 5, 31), (2024, 6, 28), (2024, 7, 26), (2024, 8, 30),
    (2024, 9, 27), (2024, 10, 31),(2024, 11, 27),(2024, 12, 20),
    (2025, 1, 31), (2025, 2, 28), (2025, 3, 28), (2025, 4, 30),
    (2025, 5, 30), (2025, 6, 27), (2025, 7, 31), (2025, 8, 29),
    (2025, 9, 26), (2025, 10, 31),(2025, 11, 26),(2025, 12, 19),
    (2026, 1, 30), (2026, 2, 27), (2026, 3, 27), (2026, 4, 30),
    (2026, 5, 29), (2026, 6, 26), (2026, 7, 31), (2026, 8, 28),
    (2026, 9, 25), (2026, 10, 30),(2026, 11, 25),(2026, 12, 18),
]


def _first_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)  # Monday=0, Friday=4


def _nfp_dates(start: date, end: date) -> list[date]:
    out: list[date] = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        nfp = _first_friday(y, m)
        if start <= nfp <= end:
            out.append(nfp)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def get_events(start: date, end: date) -> list[Event]:
    """All high-impact US events in [start, end] inclusive."""
    out: list[Event] = []
    for y, mo, d in _FOMC_DATES:
        ev = date(y, mo, d)
        if start <= ev <= end:
            out.append(Event(ev, "FOMC", "FOMC Statement"))
    for y, mo, d in _CPI_DATES:
        ev = date(y, mo, d)
        if start <= ev <= end:
            out.append(Event(ev, "CPI", "US CPI"))
    for y, mo, d in _PCE_DATES:
        ev = date(y, mo, d)
        if start <= ev <= end:
            out.append(Event(ev, "PCE", "US Core PCE"))
    for ev in _nfp_dates(start, end):
        out.append(Event(ev, "NFP", "US Non-Farm Payrolls"))

    # Live FF calendar (covers multi-currency + non-US events, auto-refreshed).
    # ff_fetcher classifies USD events into FOMC/CPI/NFP/PCE; drop any FF row
    # that collides with an already-hardcoded (date, kind) so the UI + gate
    # don't show the same event twice.
    try:
        from events import ff_fetcher
        seen = {(e.date, e.kind) for e in out}
        for ev in ff_fetcher.get_events(start, end):
            if (ev.date, ev.kind) in seen:
                continue
            out.append(ev)
            seen.add((ev.date, ev.kind))
    except Exception as e:  # fetcher must never break the signal pipeline
        import logging
        logging.getLogger(__name__).warning(f"FF calendar merge skipped: {e}")

    out.sort(key=lambda e: (e.date, e.kind))
    return out
