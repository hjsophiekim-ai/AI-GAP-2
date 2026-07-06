"""
Tests for VolumeSpikeSelector — change_rate 3~18% 필터 검증.

12개 테스트:
  1. change_rate 2.9% 종목 제외
  2. change_rate 3.0% 종목 통과
  3. change_rate 10.0% 종목이 가장 높은 change_rate_score(8)를 받는지
  4. change_rate 18.0% 종목 통과
  5. change_rate 18.1% 종목 제외
  6. Top10 부족 시에도 3% 미만 종목은 fallback 복구 금지
  7. Top10 부족 시에도 18% 초과 종목은 fallback 복구 금지
  8. Top10 dict에 change_rate_score 컬럼 포함
  9. 3~5% 구간 점수(+2)가 5~8%(+4), 8~12%(+8)보다 낮아야 한다
 10. 가격 1만원 이상 2차 fallback — 10,000~19,999원 종목이 부족 시 포함
 11. 1만원 미만 종목은 2차 fallback에서도 절대 포함 안 됨
 12. 1만원 완화 fallback이어도 상승률 3~18% 조건은 유지된다
"""
import pytest

from app.strategy.volume_spike_selector import VolumeSpikeSelector, _change_rate_score


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_stock(
    symbol: str,
    name: str,
    change_rate: float,
    trade_value: float = 5_000_000_000,
    current_price: float = 50_000,
) -> dict:
    return {
        "symbol": symbol,
        "name": name,
        "current_price": current_price,
        "change_rate": change_rate,
        "trade_value": trade_value,
        "volume": 1_000_000,
        "is_etf": False,
        "is_etn": False,
        "is_preferred": False,
        "is_spac": False,
        "is_reit": False,
    }


def _selector_with_cfg(
    min_cr=3.0,
    max_cr=18.0,
    target_n=10,
    min_tv=3_000_000_000,
    fallback_tv=1_000_000_000,
):
    """config 없는 stub selector (vs_cfg를 직접 주입)."""
    sel = VolumeSpikeSelector.__new__(VolumeSpikeSelector)
    sel.cfg = None
    sel._vs_cfg = {
        "target_top_n": target_n,
        "min_price": 20_000,
        "min_change_rate": min_cr,
        "max_change_rate": max_cr,
        "min_trading_value": min_tv,
        "fallback_min_trading_value": fallback_tv,
        "max_candidates_to_score": 80,
    }
    return sel


# ---------------------------------------------------------------------------
# 1. 2.9% 종목은 제외
# ---------------------------------------------------------------------------

def test_change_rate_29_excluded():
    sel = _selector_with_cfg()
    stocks = [_make_stock("000001", "테스트A", change_rate=2.9)]
    top10, diag = sel.select(stocks)
    assert diag["excluded_below_5pct"] == 1
    assert not any(s["symbol"] == "000001" for s in top10)


# ---------------------------------------------------------------------------
# 2. 3.0% 종목은 통과
# ---------------------------------------------------------------------------

def test_change_rate_30_passes():
    sel = _selector_with_cfg()
    stocks = [_make_stock("000002", "테스트B", change_rate=3.0)]
    top10, diag = sel.select(stocks)
    assert diag["excluded_below_5pct"] == 0
    assert any(s["symbol"] == "000002" for s in top10)


# ---------------------------------------------------------------------------
# 3. 10.0% 종목이 가장 높은 change_rate_score(8) 받는지
# ---------------------------------------------------------------------------

def test_change_rate_100_highest_score():
    score = _change_rate_score(10.0)
    assert score == 8.0, f"expected 8.0, got {score}"


def test_change_rate_score_ordering():
    """8~12% 구간이 3~5%, 5~8%, 12~18% 구간보다 점수가 높아야 한다."""
    assert _change_rate_score(10.0) > _change_rate_score(4.0)   # 8 > 2
    assert _change_rate_score(10.0) > _change_rate_score(6.0)   # 8 > 4
    assert _change_rate_score(10.0) > _change_rate_score(15.0)  # 8 > 3


# ---------------------------------------------------------------------------
# 4. 18.0% 종목 통과
# ---------------------------------------------------------------------------

def test_change_rate_180_passes():
    sel = _selector_with_cfg()
    stocks = [_make_stock("000004", "테스트D", change_rate=18.0)]
    top10, diag = sel.select(stocks)
    assert diag["excluded_above_15pct"] == 0
    assert any(s["symbol"] == "000004" for s in top10)


# ---------------------------------------------------------------------------
# 5. 18.1% 종목 제외
# ---------------------------------------------------------------------------

def test_change_rate_181_excluded():
    sel = _selector_with_cfg()
    stocks = [_make_stock("000005", "테스트E", change_rate=18.1)]
    top10, diag = sel.select(stocks)
    assert diag["excluded_above_15pct"] == 1
    assert not any(s["symbol"] == "000005" for s in top10)


# ---------------------------------------------------------------------------
# 6. Top10 부족 시 3% 미만 종목은 fallback 복구 금지
# ---------------------------------------------------------------------------

def test_fallback_does_not_recover_below_3pct():
    """primary pass가 0개여도 2.9% 종목은 fallback에 포함 안 됨."""
    sel = _selector_with_cfg(target_n=10)
    stocks = [
        _make_stock(f"A{i:03d}", f"종목{i}", change_rate=2.9, trade_value=2_000_000_000)
        for i in range(5)
    ]
    top10, diag = sel.select(stocks)
    assert diag["excluded_below_5pct"] == 5
    assert diag["final_top10"] == 0


# ---------------------------------------------------------------------------
# 7. Top10 부족 시 18% 초과 종목은 fallback 복구 금지
# ---------------------------------------------------------------------------

def test_fallback_does_not_recover_above_18pct():
    """primary pass가 0개여도 18.5% 종목은 fallback에 포함 안 됨."""
    sel = _selector_with_cfg(target_n=10)
    stocks = [
        _make_stock(f"B{i:03d}", f"종목{i}", change_rate=18.5, trade_value=2_000_000_000)
        for i in range(5)
    ]
    top10, diag = sel.select(stocks)
    assert diag["excluded_above_15pct"] == 5
    assert diag["final_top10"] == 0


# ---------------------------------------------------------------------------
# 8. Top10 dict에 change_rate_score 컬럼 포함
# ---------------------------------------------------------------------------

def test_top10_dict_has_change_rate_score():
    sel = _selector_with_cfg()
    stocks = [_make_stock("005930", "삼성전자", change_rate=9.0)]
    top10, _ = sel.select(stocks)
    assert top10, "선정 결과 없음"
    assert "change_rate_score" in top10[0]
    assert top10[0]["change_rate_score"] == 8.0  # 8~12% 구간


# ---------------------------------------------------------------------------
# 9. 3~5% 구간 점수(+2)가 다른 구간보다 낮아야 한다
# ---------------------------------------------------------------------------

def test_change_rate_score_low_band():
    """3~5% 구간은 +2로 5~8%(+4), 8~12%(+8)보다 낮다."""
    assert _change_rate_score(4.0) == 2.0
    assert _change_rate_score(4.0) < _change_rate_score(6.0)
    assert _change_rate_score(4.0) < _change_rate_score(10.0)


# ---------------------------------------------------------------------------
# 10. 가격 1만원 이상 2차 fallback — 10,000~19,999원 종목이 부족 시 포함
# ---------------------------------------------------------------------------

def _selector_with_price_relaxed(target_n=10, min_price=20_000, fallback_min_price=10_000,
                                  min_tv=3_000_000_000, fallback_tv=1_000_000_000):
    sel = VolumeSpikeSelector.__new__(VolumeSpikeSelector)
    sel.cfg = None
    sel._vs_cfg = {
        "target_top_n": target_n,
        "min_price": min_price,
        "fallback_min_price": fallback_min_price,
        "min_change_rate": 3.0,
        "max_change_rate": 18.0,
        "min_trading_value": min_tv,
        "fallback_min_trading_value": fallback_tv,
        "max_candidates_to_score": 80,
    }
    return sel


def test_price_relaxed_fallback_includes_10k_stocks():
    """primary+fallback1이 부족하면 1만원 이상 종목이 2차 fallback으로 포함된다."""
    sel = _selector_with_price_relaxed(target_n=3)
    stocks = [
        # 2만원+ → primary (30억 이상)
        _make_stock("A001", "정상종목", change_rate=9.0, trade_value=5_000_000_000, current_price=50_000),
        # 1만원~2만원 구간 → 가격 완화 fallback 후보
        _make_stock("B001", "저가종목1", change_rate=8.0, trade_value=2_000_000_000, current_price=15_000),
        _make_stock("B002", "저가종목2", change_rate=7.0, trade_value=1_500_000_000, current_price=12_000),
    ]
    top10, diag = sel.select(stocks)
    assert diag["price_relaxed_added"] > 0, "2차 fallback이 적용되어야 함"
    symbols = {s["symbol"] for s in top10}
    assert "B001" in symbols or "B002" in symbols, "1만원~2만원 종목이 포함되어야 함"


def test_price_below_10k_always_excluded():
    """1만원 미만 종목은 2차 fallback에서도 절대 포함 안 됨."""
    sel = _selector_with_price_relaxed(target_n=5)
    stocks = [
        _make_stock("C001", "초저가", change_rate=9.0, trade_value=5_000_000_000, current_price=8_000),
    ]
    top10, diag = sel.select(stocks)
    assert diag["excluded_price"] == 1
    assert not any(s["symbol"] == "C001" for s in top10)


def test_price_relaxed_fallback_still_requires_rate_filter():
    """1만원 완화 fallback이어도 상승률 3~18% 조건은 유지된다."""
    sel = _selector_with_price_relaxed(target_n=5)
    stocks = [
        # 1만원~2만원 but 상승률 2% → 제외 (3% 미만)
        _make_stock("D001", "저가저율", change_rate=2.0, trade_value=1_500_000_000, current_price=15_000),
        # 1만원~2만원 but 상승률 20% → 제외 (18% 초과)
        _make_stock("D002", "저가고율", change_rate=20.0, trade_value=1_500_000_000, current_price=15_000),
    ]
    top10, diag = sel.select(stocks)
    assert diag["final_top10"] == 0
    assert diag["price_relaxed_added"] == 0
