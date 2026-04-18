"""Tests for sentiment/cache.py — JSONL append/load/lookup."""

from datetime import date

from sentiment import cache


def test_append_creates_file_and_entry(tmp_output_dir):
    cache.append(avg_score=0.12, n_articles=10, run_date=date(2026, 4, 16))

    path = tmp_output_dir / cache.CACHE_FILENAME
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert '"date": "2026-04-16"' in lines[0]
    assert '"avg_score": 0.12' in lines[0]


def test_append_none_is_skipped(tmp_output_dir):
    cache.append(avg_score=None, n_articles=0, run_date=date(2026, 4, 16))
    path = tmp_output_dir / cache.CACHE_FILENAME
    assert not path.exists()


def test_load_empty_returns_empty_dict(tmp_output_dir):
    assert cache.load() == {}


def test_load_latest_entry_wins(tmp_output_dir):
    cache.append(0.10, 5, date(2026, 4, 16))
    cache.append(0.25, 8, date(2026, 4, 16))  # same date, newer value
    cache.append(-0.05, 2, date(2026, 4, 17))

    loaded = cache.load()
    assert loaded["2026-04-16"] == 0.25
    assert loaded["2026-04-17"] == -0.05


def test_load_skips_bad_lines(tmp_output_dir):
    path = tmp_output_dir / cache.CACHE_FILENAME
    path.write_text(
        '{"date": "2026-04-16", "avg_score": 0.1}\n'
        'not-json\n'
        '{"date": "2026-04-17", "avg_score": -0.2}\n'
    )
    loaded = cache.load()
    assert loaded == {"2026-04-16": 0.1, "2026-04-17": -0.2}


def test_lookup_by_date_object(tmp_output_dir):
    cache.append(0.3, 4, date(2026, 4, 16))
    assert cache.lookup(date(2026, 4, 16)) == 0.3
    assert cache.lookup(date(2026, 4, 17)) is None


def test_lookup_by_iso_string(tmp_output_dir):
    cache.append(0.3, 4, date(2026, 4, 16))
    assert cache.lookup("2026-04-16") == 0.3
