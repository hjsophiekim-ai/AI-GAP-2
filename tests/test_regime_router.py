"""
Market Regime Router 테스트.

검증 항목:
  1. A~F 각 유형 분류 (합성 스냅샷)
  2. confidence_score < 60 이면 F(NO_TRADE)로 강등
"""

import copy

from app.market.regime_router import MarketRegimeRouter
from app.market import regime_router as regime_router_mod


def _base_snapshot() -> dict:
    return {
        "domestic": {
            "kospi": {"value": 2500.0, "change_rate": 0.0, "advancers": 800, "decliners": 800, "success": True},
            "kosdaq": {"value": 800.0, "change_rate": 0.0, "advancers": 400, "decliners": 400, "success": True},
            "kospi200_futures": {"value": 330.0, "change_rate": 0.0, "success": True},
            "advancers": 800, "decliners": 800,
            "trading_value_top50": [],
            "change_rate_top50": [],
            "sector_change_rates": {},
            "theme_change_rates": {},
            "investor_flow": {},
            "hynix": {
                "symbol": "000660", "current_price": 200000.0, "open": 200000.0, "high": 200000.0,
                "low": 200000.0, "prev_close": 200000.0, "change_rate": 0.0, "trade_value": 1e9, "success": True,
            },
            "samsung": {
                "symbol": "005930", "current_price": 70000.0, "open": 70000.0, "high": 70000.0,
                "low": 70000.0, "prev_close": 70000.0, "change_rate": 0.0, "trade_value": 1e9, "success": True,
            },
            "hanmi": {
                "symbol": "042700", "current_price": 100000.0, "open": 100000.0, "high": 100000.0,
                "low": 100000.0, "prev_close": 100000.0, "change_rate": 0.0, "trade_value": 1e8, "success": True,
            },
        },
        "overseas": {
            "nasdaq": {"change_rate": 0.0, "success": True},
            "sp500": {"change_rate": 0.0, "success": True},
            "sox": {"change_rate": 0.0, "success": True},
            "micron": {"change_rate": 0.0, "success": True},
            "nvidia": {"change_rate": 0.0, "success": True},
            "amd": {"change_rate": 0.0, "success": True},
            "broadcom": {"change_rate": 0.0, "success": True},
            "usdkrw": {"change_rate": 0.0, "success": True},
            "us_futures": {"change_rate": 0.0, "success": True},
        },
        "meta": {"data_quality_ratio": 1.0, "log": []},
    }


def _make_router(monkeypatch, tmp_path):
    monkeypatch.setattr(regime_router_mod, "_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(regime_router_mod, "_LOG_DIR", tmp_path / "logs")
    return MarketRegimeRouter()


def test_regime_a_strong_bull_market(monkeypatch, tmp_path):
    """나스닥/SOX/반도체 급등 + 국내 시초강세 + 주도섹터 뚜렷 → A."""
    router = _make_router(monkeypatch, tmp_path)
    snap = _base_snapshot()
    snap["overseas"]["nasdaq"]["change_rate"] = 2.0
    snap["overseas"]["sox"]["change_rate"] = 4.0
    snap["overseas"]["micron"]["change_rate"] = 5.0
    snap["overseas"]["nvidia"]["change_rate"] = 5.0
    snap["domestic"]["kospi"]["change_rate"] = 1.5
    snap["domestic"]["kosdaq"]["change_rate"] = 1.8
    snap["domestic"]["advancers"] = 1500
    snap["domestic"]["decliners"] = 150
    snap["domestic"]["sector_change_rates"] = {
        "semiconductor": 7.0, "ai_data_center": 4.0, "defense": 0.5, "auto": 0.3, "finance": 0.1,
    }
    snap["domestic"]["theme_change_rates"] = {"HBM": 7.5, "AI서버": 5.0}
    snap["domestic"]["trading_value_top50"] = [{"trading_value": 5_000_000_000} for _ in range(50)]

    result = router.determine_regime(now_hm="09:30", snapshot=snap)
    assert result["regime"] == "A"
    assert result["confidence_score"] >= 60
    assert result["policy_name"] == "policy_leader_top3"


def test_regime_b_semiconductor_rebound(monkeypatch, tmp_path):
    """전일 급락 + 09:20 저점 회복 + 미국 반도체지표 반등 → B."""
    router = _make_router(monkeypatch, tmp_path)

    snap1 = _base_snapshot()
    snap1["domestic"]["hynix"]["low"] = 190000.0
    snap1["domestic"]["hynix"]["current_price"] = 190000.0
    snap1["domestic"]["samsung"]["low"] = 65000.0
    snap1["domestic"]["samsung"]["current_price"] = 65000.0
    router.determine_regime(now_hm="09:20", snapshot=snap1)  # 09:20 기준가 캐시

    snap2 = _base_snapshot()
    snap2["domestic"]["hynix"]["current_price"] = 197000.0
    snap2["domestic"]["hynix"]["day1_return"] = -3.5
    snap2["domestic"]["hynix"]["day2_cum_return"] = -6.0
    snap2["domestic"]["samsung"]["current_price"] = 67500.0
    snap2["domestic"]["samsung"]["day1_return"] = -2.5
    snap2["overseas"]["micron"]["change_rate"] = 3.0
    snap2["overseas"]["sox"]["change_rate"] = 2.0
    snap2["overseas"]["nvidia"]["change_rate"] = 1.5

    result = router.determine_regime(now_hm="09:35", snapshot=snap2)
    assert result["regime"] == "B"
    assert result["confidence_score"] >= 60
    assert result["policy_name"] == "policy_semiconductor_rebound"


def test_regime_c_theme_strong_index_weak(monkeypatch, tmp_path):
    """지수 약세/보합 + 특정 테마 거래대금 집중 → C."""
    router = _make_router(monkeypatch, tmp_path)
    snap = _base_snapshot()
    snap["domestic"]["kospi"]["change_rate"] = -0.3
    snap["domestic"]["kosdaq"]["change_rate"] = -0.2
    snap["domestic"]["advancers"] = 500
    snap["domestic"]["decliners"] = 1000
    snap["domestic"]["sector_change_rates"] = {
        "battery_ev": 9.0, "auto": 0.5, "finance": 0.2, "materials_copper": 0.1, "defense": 0.1,
    }
    snap["domestic"]["theme_change_rates"] = {"2차전지": 9.5}

    result = router.determine_regime(now_hm="09:40", snapshot=snap)
    assert result["regime"] == "C"
    assert result["confidence_score"] >= 60
    assert result["policy_name"] == "policy_gap_support"


def test_regime_d_gap_up_failure(monkeypatch, tmp_path):
    """시초 갭상승 후 시가 이탈 + 윗꼬리 → D."""
    router = _make_router(monkeypatch, tmp_path)
    snap = _base_snapshot()
    snap["domestic"]["kospi"]["change_rate"] = 0.1
    snap["domestic"]["kosdaq"]["change_rate"] = 0.1
    snap["domestic"]["trading_value_top50"] = [{"trading_value": 3_000_000_000} for _ in range(10)]

    snap["domestic"]["hynix"].update({
        "prev_close": 200000.0, "open": 208000.0, "high": 212000.0, "current_price": 202000.0,
    })
    snap["domestic"]["samsung"].update({
        "prev_close": 70000.0, "open": 72800.0, "high": 74000.0, "current_price": 70600.0,
    })

    result = router.determine_regime(now_hm="09:30", snapshot=snap)
    assert result["regime"] == "D"
    assert result["confidence_score"] >= 60
    assert result["policy_name"] == "policy_no_trade"


def test_regime_e_persistent_selloff(monkeypatch, tmp_path):
    """지수 -1.5% 이하 + 환율 상승 + 반도체 대형주 동반 약세 → E."""
    router = _make_router(monkeypatch, tmp_path)
    snap = _base_snapshot()
    snap["domestic"]["kospi"]["change_rate"] = -2.2
    snap["domestic"]["kosdaq"]["change_rate"] = -2.8
    snap["domestic"]["hynix"]["change_rate"] = -2.5
    snap["domestic"]["samsung"]["change_rate"] = -2.0
    snap["domestic"]["investor_flow"] = {"foreign_net_buy": -500000}
    snap["overseas"]["usdkrw"]["change_rate"] = 0.9

    result = router.determine_regime(now_hm="09:30", snapshot=snap)
    assert result["regime"] == "E"
    assert result["confidence_score"] >= 60
    assert result["policy_name"] == "policy_inverse"


def test_regime_f_no_clear_direction(monkeypatch, tmp_path):
    """방향성 없는 혼조 스냅샷 → F(보합/혼조장), 신규매수 금지 정책."""
    router = _make_router(monkeypatch, tmp_path)
    snap = _base_snapshot()  # 완전 중립 스냅샷

    result = router.determine_regime(now_hm="09:30", snapshot=snap)
    assert result["regime"] == "F"
    assert result["policy_name"] == "policy_no_trade"


def test_confidence_below_threshold_forces_no_trade(monkeypatch, tmp_path):
    """가장 높은 후보 점수가 60 미만이면 해당 유형이 아니라 F(NO_TRADE)로 강등된다."""
    router = _make_router(monkeypatch, tmp_path)
    snap = _base_snapshot()
    # 약한 갭실패 신호만 존재 — D 후보 점수는 나오지만 60 미만이어야 한다.
    snap["domestic"]["hynix"].update({
        "prev_close": 200000.0, "open": 202200.0, "high": 203000.0, "current_price": 201800.0,
    })

    result = router.determine_regime(now_hm="09:30", snapshot=snap)
    assert result["confidence_score"] < 60
    assert result["regime"] == "F"
    assert any("신뢰도 부족" in r for r in result["reasons"])
