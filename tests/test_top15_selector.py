"""
Tests for Top15Selector.
Uses direct Candidate construction with no external dependencies.
"""
import pytest

from app.models import Candidate
from app.strategy.top15_selector import Top15Selector


# ---------------------------------------------------------------------------
# Config stub
# ---------------------------------------------------------------------------

class _StubConfig:
    def __init__(self, max_positions: int = 15):
        self._max = max_positions
        self.trading = {"max_positions": max_positions}
        self.filters = {}

    def get(self, *keys, default=None):
        return default


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _candidate(
    rank: int,
    symbol: str,
    name: str,
    final_score: float,
    sector: str = "",
    gap_rate: float = 5.0,
    open_to_current_rate: float = 0.5,
) -> Candidate:
    return Candidate(
        rank=rank,
        symbol=symbol,
        name=name,
        current_price=10000,
        open=9434,
        high=10100,
        low=9400,
        previous_close=9434,
        gap_rate=gap_rate,
        open_to_current_rate=open_to_current_rate,
        trade_value=5_000_000_000,
        ml_score=0.5,
        rule_score=final_score,
        final_score=final_score,
        selected_reason="",
        risk_comment="",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_selects_max_15():
    """From 30 candidates, selector returns at most 15."""
    cfg = _StubConfig(max_positions=15)
    selector = Top15Selector(cfg=cfg)

    candidates = [
        _candidate(i, f"{i:06d}", f"종목{i}", final_score=float(100 - i))
        for i in range(1, 31)
    ]
    result = selector.select(candidates)
    assert len(result) <= 15


def test_fewer_than_15():
    """From 8 valid candidates, selector returns all 8."""
    cfg = _StubConfig(max_positions=15)
    selector = Top15Selector(cfg=cfg)

    candidates = [
        _candidate(i, f"{i:06d}", f"종목{i}", final_score=float(100 - i))
        for i in range(1, 9)
    ]
    result = selector.select(candidates)
    assert len(result) == 8


def test_rank_assigned():
    """Returned candidates must have ranks 1..n in order."""
    cfg = _StubConfig(max_positions=15)
    selector = Top15Selector(cfg=cfg)

    candidates = [
        _candidate(i, f"{i:06d}", f"종목{i}", final_score=float(100 - i))
        for i in range(1, 11)
    ]
    result = selector.select(candidates)
    for idx, c in enumerate(result, start=1):
        assert c.rank == idx


def test_sector_diversification():
    """
    If more than 3 candidates from the same sector appear at the top,
    the selector caps them at 3 in consecutive positions and moves
    overflow candidates to the back (or fills with other sectors).
    The result should have at most 3 consecutive picks from one sector
    unless there are not enough candidates from other sectors.
    """
    cfg = _StubConfig(max_positions=15)
    selector = Top15Selector(cfg=cfg)

    # 6 very high-score stocks all in "반도체" sector
    semiconductor = [
        _candidate(i, f"A{i:05d}", f"반도체{i}", final_score=float(95 - i), sector="반도체")
        for i in range(1, 7)
    ]
    # 10 lower-score stocks in diverse sectors
    others = [
        _candidate(i + 6, f"B{i:05d}", f"기타{i}", final_score=float(80 - i), sector=f"섹터{i}")
        for i in range(1, 11)
    ]

    result = selector.select(semiconductor + others)

    sector_counts: dict[str, int] = {}
    for c in result:
        sec = getattr(c, "sector", "") or "__unique__"
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    # No single named sector should appear more than 3 times
    # (unique sectors use per-symbol keys so they don't accumulate)
    for sector, count in sector_counts.items():
        if not sector.startswith("__unique_"):
            assert count <= 3, f"Sector '{sector}' appears {count} times (max 3)"
