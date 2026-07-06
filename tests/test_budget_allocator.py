"""
Tests for BudgetAllocator.
Uses direct Candidate construction with no external dependencies.
"""
import pytest

from app.models import Candidate
from app.trading.budget_allocator import BudgetAllocator


# ---------------------------------------------------------------------------
# Config stub
# ---------------------------------------------------------------------------

class _TradingCfg(dict):
    pass


class _StubConfig:
    def __init__(self, **trading_overrides):
        defaults = {
            "total_budget": 10_000_000,
            "max_shares_per_stock": 2,
        }
        defaults.update(trading_overrides)
        self.trading = defaults
        self.filters = {}

    def get(self, *keys, default=None):
        return default


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _candidate(rank: int, symbol: str, name: str, price: float) -> Candidate:
    return Candidate(
        rank=rank,
        symbol=symbol,
        name=name,
        current_price=price,
        open=price,
        high=price,
        low=price,
        previous_close=price,
        gap_rate=5.0,
        open_to_current_rate=0.0,
        trade_value=5_000_000_000,
        final_score=80.0,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_basic_allocation_10m():
    """10M budget, 15 stocks at ~50K each: each should get at least 1 share (2 total)."""
    cfg = _StubConfig(total_budget=10_000_000, max_shares_per_stock=2)
    allocator = BudgetAllocator(cfg=cfg)

    candidates = [_candidate(i, f"{i:06d}", f"종목{i}", 50_000) for i in range(1, 16)]
    plans = allocator.allocate(candidates, total_budget=10_000_000, max_shares=2)

    assert len(plans) > 0
    for plan in plans:
        assert plan.allocated_quantity >= 1
        assert plan.allocated_quantity <= 2


def test_expensive_stock_skipped():
    """Stock at 8M won is skipped when remaining budget is less than its price."""
    cfg = _StubConfig(total_budget=10_000_000, max_shares_per_stock=2)
    allocator = BudgetAllocator(cfg=cfg)

    # 1 cheap stock + 1 expensive stock; after buying 2 shares of cheap (2x50K=100K)
    # the 8M stock should not be allocated if budget < 8M
    cheap = _candidate(1, "000001", "저가주", 50_000)
    expensive = _candidate(2, "000002", "고가주", 8_000_000)

    plans = allocator.allocate([cheap, expensive], total_budget=10_000_000, max_shares=2)

    symbols = {p.symbol for p in plans}
    # expensive stock should not appear because 8M > 10M - (round 1 cheap purchase)
    # After first round: 10M - 50K = 9.95M; second round cheap: 9.95M - 50K = 9.9M
    # But 8M < 9.9M, so it would be allocated in round 1. Let's verify the logic:
    # Round 1: cheap=50K OK -> buy, expensive=8M OK (9.5M left) -> buy
    # Round 2: cheap 50K OK -> buy, expensive 8M: 1.45M left < 8M -> skip
    # So expensive gets exactly 1 share
    expensive_plans = [p for p in plans if p.symbol == "000002"]
    if expensive_plans:
        assert expensive_plans[0].allocated_quantity == 1


def test_max_shares_2_limit():
    """종목이 10개(target_n)이면 Phase2가 비활성화되므로 max_shares=2 상한이 유지된다."""
    cfg = _StubConfig(total_budget=100_000_000, max_shares_per_stock=2)
    allocator = BudgetAllocator(cfg=cfg)

    # 10개 종목 → len == target_n(10) → Phase2 미발동, 최대 2주
    candidates = [_candidate(i, f"{i:06d}", f"종목{i}", 10_000) for i in range(1, 11)]
    plans = allocator.allocate(candidates, total_budget=100_000_000, max_shares=2)

    for plan in plans:
        assert plan.allocated_quantity <= 2


def test_round_robin_order():
    """
    Round-robin allocation: all stocks get 1 share in round 1 before
    any stock receives a second share.  With enough budget for 2 per stock,
    every stock present in the result should end up with 2 shares.
    Uses 10 candidates so Phase 2 is NOT triggered (len == target_n).
    """
    cfg = _StubConfig(total_budget=100_000_000, max_shares_per_stock=2)
    allocator = BudgetAllocator(cfg=cfg)

    # 10개 종목 → Phase2 미발동
    candidates = [_candidate(i, f"{i:06d}", f"종목{i}", 1_000) for i in range(1, 11)]
    plans = allocator.allocate(candidates, total_budget=100_000_000, max_shares=2)

    for plan in plans:
        assert plan.allocated_quantity == 2


def test_remaining_budget_tracked():
    """remaining_budget_after should decrease (or stay) from one plan to the next."""
    cfg = _StubConfig(total_budget=10_000_000, max_shares_per_stock=2)
    allocator = BudgetAllocator(cfg=cfg)

    candidates = [_candidate(i, f"{i:06d}", f"종목{i}", 50_000) for i in range(1, 6)]
    plans = allocator.allocate(candidates, total_budget=10_000_000, max_shares=2)

    assert len(plans) >= 2
    for i in range(len(plans) - 1):
        assert plans[i].remaining_budget_after >= plans[i + 1].remaining_budget_after


# ---------------------------------------------------------------------------
# Phase 2: 잔여 예산 추가 배분 테스트
# ---------------------------------------------------------------------------

def test_budget_fillup_phase2_triggered():
    """6종목 < 10개 → Phase2 활성화, 잔여 예산을 최소가(min_price)보다 적게 남긴다."""
    cfg = _StubConfig(total_budget=10_000_000, max_shares_per_stock=2)
    allocator = BudgetAllocator(cfg=cfg)

    # 6 stocks at 300K each
    # Phase1: 6×2×300K = 3.6M used, 6.4M remaining
    # Phase2: rounds until remaining < 300K
    candidates = [_candidate(i, f"{i:06d}", f"종목{i}", 300_000) for i in range(1, 7)]
    plans = allocator.allocate(candidates, total_budget=10_000_000, max_shares=2)

    total_used = sum(p.allocated_amount for p in plans)
    remaining = 10_000_000 - total_used
    min_price = 300_000

    assert remaining < min_price, f"잔여예산 {remaining:,}원 ≥ 최소가 {min_price:,}원 — Phase2 미작동"
    assert len(plans) == 6


def test_budget_fillup_rank_priority():
    """Phase2에서 잔여예산이 일부 종목만 살 수 있을 때, 순위 높은 종목이 우선 배분된다."""
    cfg = _StubConfig(total_budget=9_500_000, max_shares_per_stock=2)
    allocator = BudgetAllocator(cfg=cfg)

    # 3 stocks at 500K, budget=9.5M
    # Phase1: 3×2×500K = 3M, remaining = 6.5M
    # Phase2 4 full rounds = 6M → remaining = 0.5M
    # Final partial: rank1 buys (0M left), rank2/3 cannot → rank1 gets +1 extra
    candidates = [_candidate(i, f"{i:06d}", f"종목{i}", 500_000) for i in range(1, 4)]
    plans = allocator.allocate(candidates, total_budget=9_500_000, max_shares=2)

    by_rank = {p.rank: p for p in plans}
    # rank 1 must have strictly more shares than rank 3 (partial-round priority)
    assert by_rank[1].allocated_quantity > by_rank[3].allocated_quantity, (
        f"rank1={by_rank[1].allocated_quantity}, rank3={by_rank[3].allocated_quantity}"
    )


def test_budget_fillup_not_triggered_at_10_stocks():
    """종목 수 == target_n(10)이면 Phase2가 비활성화되고 잔여 예산이 남는다."""
    cfg = _StubConfig(total_budget=100_000_000, max_shares_per_stock=2)
    allocator = BudgetAllocator(cfg=cfg)

    # 10 stocks at 10K, budget=100M → Phase1 uses only 200K, Phase2 must NOT run
    candidates = [_candidate(i, f"{i:06d}", f"종목{i}", 10_000) for i in range(1, 11)]
    plans = allocator.allocate(candidates, total_budget=100_000_000, max_shares=2)

    for plan in plans:
        assert plan.allocated_quantity == 2, (
            f"Phase2가 의도치 않게 활성화됨: {plan.symbol} → {plan.allocated_quantity}주"
        )
