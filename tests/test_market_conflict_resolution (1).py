"""test_market_conflict_resolution.py — 국내시장예측 결과의 논리적 충돌(§1~§10) 수정 검증(13개 시나리오).

배경: current_regime=E인데 1h/3h가 UP으로 나오거나, up36/down31/side33처럼
확률차가 거의 없는데 UP으로 단정하거나, semiconductor_collapse_score=78인데
반도체가 UP으로 나오는 등 "논리적으로 충돌하는" 예측이 실제 운영에서
관측되었다. 이 파일은 그 각각의 재발을 막는 회귀 테스트다.
"""

from __future__ import annotations

import json

from app.execution.auto_trader import AutoTrader
from app.market import market_prediction as mp
from app.market import regime_features as rf


def _base_snapshot() -> dict:
    return {
        "domestic": {
            "kospi": {"value": 2500.0, "change_rate": 0.0, "advancers": 800, "decliners": 800, "success": True},
            "kosdaq": {"value": 800.0, "change_rate": 0.0, "advancers": 400, "decliners": 400, "success": True},
            "kospi200_futures": {"value": 330.0, "change_rate": 0.0, "success": True},
            "advancers": 800, "decliners": 800,
            "trading_value_top50": [], "change_rate_top50": [],
            "sector_change_rates": {}, "theme_change_rates": {}, "investor_flow": {},
            "investor_flow_market": {"foreign_net_buy_sum": 0, "institution_net_buy_sum": 0,
                                      "success": True, "is_proxy": False, "program_net_buy": 0},
            "news_shock": {"score": 5.0, "success": True},
            "hynix": {
                "symbol": "000660", "current_price": 200000.0, "open": 200000.0, "high": 200000.0,
                "low": 200000.0, "prev_close": 200000.0, "change_rate": 0.0, "trade_value": 1e9,
                "vwap": 200000.0, "success": True,
            },
            "samsung": {
                "symbol": "005930", "current_price": 70000.0, "open": 70000.0, "high": 70000.0,
                "low": 70000.0, "prev_close": 70000.0, "change_rate": 0.0, "trade_value": 1e9,
                "vwap": 70000.0, "success": True,
            },
            "hanmi": {
                "symbol": "042700", "current_price": 100000.0, "open": 100000.0, "high": 100000.0,
                "low": 100000.0, "prev_close": 100000.0, "change_rate": 0.0, "trade_value": 1e8,
                "vwap": 100000.0, "success": True,
            },
        },
        "overseas": {
            "nasdaq": {"change_rate": 0.0, "success": True}, "sp500": {"change_rate": 0.0, "success": True},
            "sox": {"change_rate": 0.0, "success": True}, "micron": {"change_rate": 0.0, "success": True},
            "nvidia": {"change_rate": 0.0, "success": True}, "amd": {"change_rate": 0.0, "success": True},
            "broadcom": {"change_rate": 0.0, "success": True}, "usdkrw": {"change_rate": 0.0, "value": 1350.0, "success": True},
            "us_futures": {"change_rate": 0.0, "success": True},
            "us_market_status": {
                "is_us_market_open": False, "is_us_holiday": False, "is_us_weekend": False,
                "is_us_early_close": False, "last_us_trading_day": "2026-07-06",
                "session": "closed", "source": "test", "timestamp": "", "confidence": 0.9,
            },
            "us_realtime_bars": {}, "us_last_session": {}, "holiday_mode_inputs": {},
        },
        "deltas": {
            "5m": {"kospi200_futures_change_rate": 0.0, "usdkrw_value": 0.0},
            "15m": {"kospi200_futures_change_rate": 0.0, "usdkrw_value": 0.0},
        },
        "meta": {"data_quality_ratio": 1.0, "log": []},
    }


# ---------------------------------------------------------------------------
# 1. up36/down31/side33 -> UNCERTAIN/SIDEWAYS, 절대 UP 아님
# ---------------------------------------------------------------------------

def test_close_probabilities_never_resolve_to_up():
    for horizon in ("30m", "1h", "3h"):
        direction = mp._direction_from_probs(31.0, 33.0, 36.0, horizon)
        assert direction in ("SIDEWAYS", "UNCERTAIN")
        assert direction != "UP"


# ---------------------------------------------------------------------------
# 2. current_regime=E, recovery_score=39(낮음) -> 1h/3h expected_regime != C
# ---------------------------------------------------------------------------

def test_low_recovery_score_in_e_regime_never_relaxes_to_c():
    snap = _base_snapshot()
    # 반도체/지수 모두 약세로 설정해 실제 하락압력 자체도 높게 만든다.
    snap["domestic"]["hynix"]["current_price"] = 190000.0
    snap["domestic"]["samsung"]["current_price"] = 66000.0
    regime_result = {"regime": "E", "scores": {"market_collapse_score": 82.0, "semiconductor_collapse_score": 78.0}}
    recovery_info = {"recovery_score": 39.0}

    for horizon in ("1h", "3h"):
        pred = mp.predict_market_direction(horizon, snap, regime_result, recovery_info=recovery_info)
        assert pred["expected_regime"] != "C"


# ---------------------------------------------------------------------------
# 3. E->C 완화는 가드 조건을 모두 충족했을 때만 허용된다
# ---------------------------------------------------------------------------

def test_e_to_c_relaxation_requires_all_guard_conditions():
    # 조건 미충족(데이터품질 낮음) -> 차단
    ctx_incomplete = {
        "market_collapse_delta_15m": -12.0, "semiconductor_collapse_score": 60.0,
        "hynix_vwap_recovered": True, "breadth_recovering": True, "data_quality_score": 50.0,
    }
    regime, applied = mp._expected_regime(
        direction="SIDEWAYS", down_pressure=45.0, market_collapse_score=65.0, current_regime="E",
        recovery_score=75.0, guard_context=ctx_incomplete,
    )
    assert regime != "C"
    assert "E_RELAXATION_BLOCKED" in applied or "E_TO_D_PASS" in applied

    # 모든 조건 충족 -> 허용
    ctx_full = {
        "market_collapse_delta_15m": -12.0, "semiconductor_collapse_score": 60.0,
        "hynix_vwap_recovered": True, "breadth_recovering": True, "data_quality_score": 75.0,
    }
    regime_ok, applied_ok = mp._expected_regime(
        direction="SIDEWAYS", down_pressure=45.0, market_collapse_score=65.0, current_regime="E",
        recovery_score=75.0, guard_context=ctx_full,
    )
    assert regime_ok == "C"
    assert "E_TO_C_PASS" in applied_ok


# ---------------------------------------------------------------------------
# 4. semiconductor_collapse_score>=75 -> 반도체 UP 판정 금지
# ---------------------------------------------------------------------------

def test_semiconductor_collapse_score_above_75_forbids_up():
    snap = _base_snapshot()
    # 반도체 컴포넌트를 강세로 만들어(대장주 VWAP 위, 미국반도체 강세) 가드 없으면 UP이 나올 상황을 만든다.
    for key in ("hynix", "samsung", "hanmi"):
        snap["domestic"][key]["current_price"] = snap["domestic"][key]["vwap"] * 1.03
    for key in ("micron", "nvidia", "sox"):
        snap["overseas"][key]["change_rate"] = 3.0
    snap["deltas"]["5m"]["kospi200_futures_change_rate"] = 1.0
    snap["deltas"]["15m"]["kospi200_futures_change_rate"] = 0.8

    regime_result = {"scores": {"semiconductor_collapse_score": 80.0}}
    pred = mp.predict_semiconductor_direction("1h", snap, regime_result)
    assert pred["direction"] != "UP"
    assert pred["direction_before_guard"] == "UP"
    assert pred["semiconductor_collapse_score"] == 80.0


# ---------------------------------------------------------------------------
# 5. 하이닉스/삼성전자/한미반도체 모두 VWAP 이탈 -> 반도체 UP 판정 금지
# ---------------------------------------------------------------------------

def test_all_semi_stocks_below_vwap_forbids_up():
    snap = _base_snapshot()
    # collapse_score는 낮게 유지하되(가드4는 통과), 3개 종목 모두 VWAP 아래로 만든다.
    for key in ("hynix", "samsung", "hanmi"):
        snap["domestic"][key]["current_price"] = snap["domestic"][key]["vwap"] * 0.97
    for key in ("micron", "nvidia", "sox"):
        snap["overseas"][key]["change_rate"] = 3.0
    snap["deltas"]["5m"]["kospi200_futures_change_rate"] = 1.0
    snap["deltas"]["15m"]["kospi200_futures_change_rate"] = 0.8

    regime_result = {"scores": {"semiconductor_collapse_score": 20.0}}
    pred = mp.predict_semiconductor_direction("1h", snap, regime_result)
    assert pred["all_semi_stocks_below_vwap"] is True
    assert pred["direction"] != "UP"


# ---------------------------------------------------------------------------
# 6. 상승/하락종목수 0/0 -> data_quality_score 하드캡(70) 적용
# ---------------------------------------------------------------------------

def test_zero_advancers_decliners_caps_data_quality_score():
    snap = _base_snapshot()
    snap["domestic"]["advancers"] = 0
    snap["domestic"]["decliners"] = 0
    quality = rf.compute_data_quality_score(snap)
    assert quality <= 70.0


# ---------------------------------------------------------------------------
# 7. 외국인 수급이 proxy면 data_quality_score/confidence 하드캡(75) 적용
# ---------------------------------------------------------------------------

def test_proxy_foreign_flow_caps_data_quality_score():
    snap = _base_snapshot()
    snap["domestic"]["investor_flow_market"]["is_proxy"] = True
    quality = rf.compute_data_quality_score(snap)
    assert quality <= 75.0

    regime_result = {"data_quality_score": quality}
    pred = mp.predict_market_direction("1h", snap, regime_result)
    assert pred["confidence_score"] <= 65.0


# ---------------------------------------------------------------------------
# 8. MU 데이터가 Yahoo-delayed면 반도체 예측 confidence 상한(60) 적용
# ---------------------------------------------------------------------------

def test_mu_yahoo_delayed_caps_semiconductor_confidence():
    snap = _base_snapshot()
    pred = mp.predict_semiconductor_direction(
        "1h", snap, regime_result=None, mu_data_status="DELAYED", mu_data_source="yahoo",
    )
    assert pred["confidence_score"] <= 60.0


# ---------------------------------------------------------------------------
# 9. 뉴스 수집 실패는 UNKNOWN이며 "긍정 신호(0점)"로 쓰이지 않는다
# ---------------------------------------------------------------------------

def test_news_collection_failure_is_unknown_not_positive_signal():
    snap = _base_snapshot()
    snap["domestic"]["news_shock"] = {"success": False}
    score = rf.compute_news_shock_score(snap)
    assert score is None  # 0.0(매우 긍정적)이 아니라 None(UNKNOWN)이어야 한다
    assert rf.classify_news_status(snap) == "COLLECTION_FAILED"


# ---------------------------------------------------------------------------
# 10. 전체시장(overall_market)과 주도테마(leading_theme) 예측은 분리되어 있다
# ---------------------------------------------------------------------------

def test_overall_market_and_leading_theme_are_separated():
    for horizon_weights in mp._HORIZON_WEIGHTS.values():
        assert "theme" not in horizon_weights
        assert "semi_vwap" not in horizon_weights

    snap = _base_snapshot()
    theme_status = mp.predict_leading_theme_status(snap)
    assert "direction" not in theme_status
    assert "probability_up" not in theme_status
    assert "status" in theme_status and "leading_theme_maintained" in theme_status


# ---------------------------------------------------------------------------
# 11. E/D + (가정상) 강한 주도테마여도 AUTO 매수는 차단된다
# ---------------------------------------------------------------------------

def test_e_regime_blocks_auto_buy_regardless_of_leading_theme_strength():
    trader = AutoTrader.__new__(AutoTrader)
    trader.trading_cfg = {}
    # leading_theme_prediction이 "강하게 유지"라고 나와도 _auto_buy_recovery_gate는
    # 이를 파라미터로 받지 않는다 — current_regime=E이면 무조건 AUTO를 막는다는
    # 사실 자체가 "주도테마 강세가 E/D 차단을 무력화하지 않는다"는 요건을 구조적으로 보장한다.
    regime_result = {
        "regime": "E", "recovery_score": 90.0, "market_collapse_score": 30.0,
        "semiconductor_collapse_score": 20.0, "data_quality_score": 95.0,
        "leading_theme_prediction": {"leading_theme_maintained": True, "status": "STABLE"},
        "predictions": {"30m": {"confidence_score": 90.0, "probability_up": 90.0}},
    }
    manual_only, reason = trader._auto_buy_recovery_gate(regime_result)
    assert manual_only is True
    assert "AUTO" in reason or "수동승인" in reason


# ---------------------------------------------------------------------------
# 12. 장중 내일장 예측은 INTRADAY_PRELIMINARY + confidence 상한(60)
# ---------------------------------------------------------------------------

def test_intraday_tomorrow_prediction_is_capped():
    snap = _base_snapshot()
    result = mp.predict_tomorrow_market(snap, now_hm="11:00")
    assert result["state"] == "INTRADAY_PRELIMINARY"
    assert result["confidence_score"] <= 60.0
    assert result["disclaimer"]


# ---------------------------------------------------------------------------
# 13. expected_regime_before_guard/after_guard가 디버그 로그에 기록된다
# ---------------------------------------------------------------------------

def test_debug_log_records_before_and_after_guard_regime(monkeypatch, tmp_path):
    monkeypatch.setattr(mp, "_DEBUG_LOG_DIR", tmp_path)
    snap = _base_snapshot()
    regime_result = {"regime": "D", "scores": {"market_collapse_score": 60.0}}
    result = mp.predict_market_direction("1h", snap, regime_result)

    assert "expected_regime_before_guard" in result
    assert "expected_regime" in result
    assert "guard_rules_applied" in result

    log_files = list(tmp_path.glob("*.jsonl"))
    assert len(log_files) == 1
    lines = log_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert "expected_regime_before_guard" in entry
    assert "expected_regime_after_guard" in entry
    assert "guard_rules_applied" in entry
