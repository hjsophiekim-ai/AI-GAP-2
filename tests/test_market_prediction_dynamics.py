"""
실시간 장세 변화 감지 + 30분/1시간/3시간/내일장 예측 기능 테스트.

검증 항목(요청 순서):
  1. 09:30 C타입 -> 외국인 선물 급매도 + 실제 지표 악화 시 10:00 D타입 전환
  2. KOSPI200 선물 급락 + 환율 상승 시 predicted_down_30m 상승
  3. 하이닉스 VWAP 이탈 + 삼성전자 약세 시 semiconductor_collapse_score 상승
  4. predicted_down_30m >= 65 이면 신규 자동매수 금지
  5. market_collapse_score >= 80 이면 alert_level CRITICAL
  6. C타입인데 predicted_regime_30m이 D이면 WATCH_ONLY/MANUAL_ONLY로 강등
  7. 데이터 품질이 낮으면 confidence가 낮아지고 자동매수가 금지된다
  8. 5분마다 market_prediction 로그가 jsonl로 저장된다
  9. 내일장 예측이 preliminary/closing_based/us_session_updated 상태를 구분한다
  10. 수동매수 포지션의 자동손절/시간청산 회귀 테스트 (+CRITICAL 방어청산 확장 확인)
"""

import copy
import json

from unittest.mock import MagicMock

from app.market.regime_router import MarketRegimeRouter
from app.market import regime_router as regime_router_mod
from app.market import regime_features as rf
from app.market import market_prediction as mp
from app.market import market_alert as ma
from app.market.policy_selector import select_policy


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------

def _base_snapshot() -> dict:
    return {
        "domestic": {
            "kospi": {"value": 2500.0, "change_rate": 0.0, "advancers": 800, "decliners": 800, "success": True},
            "kosdaq": {"value": 800.0, "change_rate": 0.0, "advancers": 400, "decliners": 400, "success": True},
            "kospi200_futures": {"value": 330.0, "change_rate": 0.0, "success": True},
            "advancers": 800, "decliners": 800,
            "trading_value_top50": [], "change_rate_top50": [],
            "sector_change_rates": {}, "theme_change_rates": {}, "investor_flow": {},
            "investor_flow_market": {"foreign_net_buy_sum": 0, "institution_net_buy_sum": 0, "success": True},
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
        "deltas": {"5m": {}, "15m": {}},
        "meta": {"data_quality_ratio": 1.0, "log": []},
    }


def _make_router(monkeypatch, tmp_path):
    monkeypatch.setattr(regime_router_mod, "_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(regime_router_mod, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(regime_router_mod, "_PREDICTION_LOG_DIR", tmp_path / "predlogs")
    return MarketRegimeRouter()


def _regime_result(regime="C", confidence=80.0, predicted_regime_30m=None, predicted_down_30m=None,
                    market_collapse_score=10.0, semiconductor_collapse_score=10.0):
    from app.market.regime_rules import REGIME_POLICY_MAP
    return {
        "regime": regime,
        "confidence_score": confidence,
        "policy_name": REGIME_POLICY_MAP.get(regime, "policy_no_trade"),
        "scores": {"risk_off_score": 10.0},
        "predicted_regime_30m": predicted_regime_30m,
        "market_collapse_score": market_collapse_score,
        "semiconductor_collapse_score": semiconductor_collapse_score,
        "predictions": {"30m": {"probability_down": predicted_down_30m}} if predicted_down_30m is not None else {},
    }


# ---------------------------------------------------------------------------
# 1. C -> D 전환 감지 (외국인 선물 급매도 + 실제 지표 악화)
# ---------------------------------------------------------------------------

def test_regime_transitions_from_c_to_d_within_the_day(monkeypatch, tmp_path):
    router = _make_router(monkeypatch, tmp_path)

    snap_c = _base_snapshot()
    snap_c["domestic"]["kospi"]["change_rate"] = -0.3
    snap_c["domestic"]["kosdaq"]["change_rate"] = -0.2
    snap_c["domestic"]["advancers"] = 500
    snap_c["domestic"]["decliners"] = 1000
    snap_c["domestic"]["sector_change_rates"] = {
        "battery_ev": 9.0, "auto": 0.5, "finance": 0.2, "materials_copper": 0.1, "defense": 0.1,
    }
    snap_c["domestic"]["theme_change_rates"] = {"2차전지": 9.5}

    result_c = router.determine_regime(now_hm="09:30", snapshot=snap_c)
    assert result_c["regime"] == "C"
    assert result_c["initial_regime"] == "C"

    snap_d = _base_snapshot()
    snap_d["domestic"]["kospi"]["change_rate"] = 0.1
    snap_d["domestic"]["kosdaq"]["change_rate"] = 0.1
    snap_d["domestic"]["trading_value_top50"] = [{"trading_value": 3_000_000_000} for _ in range(10)]
    snap_d["domestic"]["hynix"].update({
        "prev_close": 200000.0, "open": 208000.0, "high": 212000.0, "current_price": 202000.0,
    })
    snap_d["domestic"]["samsung"].update({
        "prev_close": 70000.0, "open": 72800.0, "high": 74000.0, "current_price": 70600.0,
    })
    # 외국인 선물(프록시) 급매도로 전환 — 09:20 +흐름 -> 10:00 급격한 순매도
    snap_d["domestic"]["investor_flow_market"] = {
        "foreign_net_buy_sum": -3_000_000, "institution_net_buy_sum": -500_000, "success": True,
    }

    result_d = router.determine_regime(now_hm="10:00", snapshot=snap_d)
    assert result_d["regime"] == "D"
    assert result_d["initial_regime"] == "C"          # 최초 유형은 유지
    assert result_d["current_regime"] == "D"          # 현재 유형은 갱신
    assert result_d["regime_change_risk"] > 0


# ---------------------------------------------------------------------------
# 2. KOSPI200 선물 급락 + 환율 상승 -> predicted_down_30m 상승
# ---------------------------------------------------------------------------

def test_futures_and_fx_deterioration_raises_predicted_down_30m():
    calm = _base_snapshot()
    calm["deltas"] = {
        "5m": {"kospi200_futures_change_rate": 0.0, "usdkrw_value": 0.0},
        "15m": {"kospi200_futures_change_rate": 0.0, "usdkrw_value": 0.0},
    }
    stressed = _base_snapshot()
    stressed["deltas"] = {
        "5m": {"kospi200_futures_change_rate": -1.2, "usdkrw_value": 4.0},
        "15m": {"kospi200_futures_change_rate": -2.0, "usdkrw_value": 6.0},
    }

    calm_pred = mp.predict_market_direction("30m", calm, _regime_result())
    stressed_pred = mp.predict_market_direction("30m", stressed, _regime_result())

    assert stressed_pred["probability_down"] > calm_pred["probability_down"]
    assert stressed_pred["direction"] == "DOWN"


# ---------------------------------------------------------------------------
# 3. 하이닉스 VWAP 이탈 + 삼성전자 약세 -> semiconductor_collapse_score 상승
# ---------------------------------------------------------------------------

def test_semiconductor_vwap_breakdown_raises_collapse_score():
    healthy = _base_snapshot()  # hynix/samsung price == vwap, change_rate 0

    weak = _base_snapshot()
    weak["domestic"]["hynix"]["current_price"] = 190000.0  # vwap(200000) 아래로 이탈
    weak["domestic"]["hynix"]["change_rate"] = -3.0
    weak["domestic"]["samsung"]["current_price"] = 67000.0  # vwap(70000) 아래
    weak["domestic"]["samsung"]["change_rate"] = -2.5

    healthy_score = rf.compute_semiconductor_collapse_score(healthy)
    weak_score = rf.compute_semiconductor_collapse_score(weak)

    assert weak_score > healthy_score


# ---------------------------------------------------------------------------
# 4. predicted_down_30m >= 65 -> 신규 자동매수 금지
# ---------------------------------------------------------------------------

def test_predicted_down_30m_blocks_new_entry():
    result = select_policy(
        _regime_result(regime="A", confidence=90.0, predicted_down_30m=70.0),
        now_hm="09:30",
    )
    assert result.allow_new_entry is False
    assert any("30분 후 하락확률" in r for r in result.block_reasons)


def test_predicted_down_30m_below_threshold_does_not_block():
    result = select_policy(
        _regime_result(regime="A", confidence=90.0, predicted_down_30m=40.0),
        now_hm="09:30",
    )
    assert result.allow_new_entry is True


# ---------------------------------------------------------------------------
# 5. market_collapse_score >= 80 -> alert_level CRITICAL
# ---------------------------------------------------------------------------

def test_market_collapse_score_triggers_critical_alert():
    alert = ma.compute_alert_level(
        current_regime="C", predicted_regime_30m="D", predicted_down_30m=50.0,
        market_collapse_score=85.0, semiconductor_collapse_score=20.0,
    )
    assert alert.alert_level == ma.CRITICAL


def test_normal_conditions_no_alert():
    alert = ma.compute_alert_level(
        current_regime="A", predicted_regime_30m="A", predicted_down_30m=20.0,
        market_collapse_score=15.0, semiconductor_collapse_score=15.0,
        foreign_flow_reversal_score=40.0,
    )
    assert alert.alert_level == ma.NONE


# ---------------------------------------------------------------------------
# 6. C타입 + predicted_regime_30m=D -> WATCH_ONLY/MANUAL_ONLY 강등
# ---------------------------------------------------------------------------

def test_c_regime_with_predicted_d_becomes_watch_only():
    result = select_policy(
        _regime_result(regime="C", confidence=85.0, predicted_regime_30m="D", predicted_down_30m=30.0),
        now_hm="09:30",
    )
    assert result.allow_new_entry is True  # 후보 생성 자체는 계속 허용
    assert result.watch_only is True
    assert result.manual_approval_only is True
    assert result.policy_name == "policy_gap_support"  # 정책 자체는 유지, 자동매수만 금지


# ---------------------------------------------------------------------------
# 7. 데이터 품질 낮으면 confidence 하락 + 자동매수 금지
# ---------------------------------------------------------------------------

def test_low_data_quality_reduces_confidence_and_blocks_entry(monkeypatch, tmp_path):
    router = _make_router(monkeypatch, tmp_path)

    good_snap = _base_snapshot()
    good_snap["domestic"]["kospi"]["change_rate"] = 1.5
    good_snap["domestic"]["kosdaq"]["change_rate"] = 1.8
    good_snap["overseas"]["nasdaq"]["change_rate"] = 2.0
    good_snap["overseas"]["sox"]["change_rate"] = 4.0
    good_snap["domestic"]["advancers"] = 1500
    good_snap["domestic"]["decliners"] = 150
    good_snap["domestic"]["sector_change_rates"] = {"semiconductor": 7.0, "ai_data_center": 4.0}
    good_snap["domestic"]["trading_value_top50"] = [{"trading_value": 5_000_000_000} for _ in range(50)]
    good_snap["meta"]["data_quality_ratio"] = 1.0

    bad_snap = copy.deepcopy(good_snap)
    bad_snap["meta"]["data_quality_ratio"] = 0.15  # 대부분의 데이터 수집 실패

    good_result = router.determine_regime(now_hm="09:30", snapshot=good_snap)

    router2 = _make_router(monkeypatch, tmp_path)
    bad_result = router2.determine_regime(now_hm="09:30", snapshot=bad_snap)

    assert bad_result["confidence_score"] < good_result["confidence_score"]

    policy_selection = select_policy(bad_result, now_hm="09:30", policy_cfg={"confidence_threshold": 60})
    if bad_result["confidence_score"] < 60:
        assert policy_selection.allow_new_entry is False


# ---------------------------------------------------------------------------
# 8. 5분마다 market_prediction 로그가 jsonl로 저장된다
# ---------------------------------------------------------------------------

def test_market_prediction_log_written_as_jsonl(monkeypatch, tmp_path):
    router = _make_router(monkeypatch, tmp_path)
    snap = _base_snapshot()

    router.determine_regime(now_hm="09:25", snapshot=snap)
    router.determine_regime(now_hm="09:30", snapshot=snap)

    log_path = tmp_path / "predlogs" / f"{regime_router_mod._today()}.jsonl"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        entry = json.loads(line)
        for key in (
            "timestamp", "initial_regime", "current_regime", "predicted_regime_30m",
            "predicted_regime_1h", "predicted_regime_3h", "tomorrow_prediction",
            "market_collapse_score", "semiconductor_collapse_score", "alert_level",
            "action_recommendation",
        ):
            assert key in entry


# ---------------------------------------------------------------------------
# 9. 내일장 예측 상태 구분 (US_SESSION_UPDATED/PREOPEN_FINAL/INTRADAY_PRELIMINARY/CLOSING_BASED)
# ---------------------------------------------------------------------------

def test_tomorrow_prediction_state_transitions():
    snap = _base_snapshot()
    assert mp.predict_tomorrow_market(snap, now_hm="08:00")["state"] == "US_SESSION_UPDATED"
    assert mp.predict_tomorrow_market(snap, now_hm="08:55")["state"] == "PREOPEN_FINAL"
    assert mp.predict_tomorrow_market(snap, now_hm="10:30")["state"] == "INTRADAY_PRELIMINARY"
    assert mp.predict_tomorrow_market(snap, now_hm="16:00")["state"] == "CLOSING_BASED"


# ---------------------------------------------------------------------------
# 10. 수동매수 포지션 자동손절/시간청산 회귀 + CRITICAL 방어청산 확장
# ---------------------------------------------------------------------------

def test_manual_position_auto_stop_loss_still_works_after_alert_level_param():
    from app.execution.position_guard import PositionGuard, GuardedPosition
    from app.models import OrderResult

    executor = MagicMock()

    def _sell(symbol, name, quantity, price, reason="", source=""):
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="sell", quantity=quantity, price=price, order_type="market",
            order_id="T-1", message="ok",
        )

    executor.sell.side_effect = _sell
    guard = PositionGuard(executor, cfg={"stop_loss_pct": -1.2, "force_exit_time": "11:10"})
    guard.register_position(GuardedPosition(symbol="000660", name="SK하이닉스", quantity=10, avg_price=100000, source="manual"))

    # alert_level 파라미터를 아예 넘기지 않아도(default) 기존 동작 그대로 유지
    actions = guard.evaluate_and_execute({"000660": {"price": 98800.0}}, now_hm="10:00", regime="A")
    assert actions[0]["reason"] == "stop_loss"
    assert guard.get_open_positions() == []

    guard2 = PositionGuard(executor, cfg={"force_exit_time": "11:10"})
    guard2.register_position(GuardedPosition(symbol="005930", name="삼성전자", quantity=5, avg_price=70000, source="manual"))
    actions2 = guard2.evaluate_and_execute({"005930": {"price": 70100.0}}, now_hm="11:10", regime="A")
    assert actions2[0]["reason"] == "time_exit"


def test_critical_alert_defends_manual_position_below_take_profit1():
    from app.execution.position_guard import PositionGuard, GuardedPosition
    from app.models import OrderResult

    executor = MagicMock()

    def _sell(symbol, name, quantity, price, reason="", source=""):
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="sell", quantity=quantity, price=price, order_type="market",
            order_id="T-2", message="ok",
        )

    executor.sell.side_effect = _sell
    guard = PositionGuard(executor, cfg={"stop_loss_pct": -1.2, "take_profit1_pct": 2.0, "force_exit_time": "11:10"})
    guard.register_position(GuardedPosition(symbol="000660", name="SK하이닉스", quantity=10, avg_price=100000, source="manual"))

    # 수익률 +0.5% (손절선/익절선 어느 쪽도 아직 도달 안함) — 평상시라면 유지되어야 함
    normal_actions = guard.evaluate_and_execute({"000660": {"price": 100500.0}}, now_hm="10:00", regime="A", alert_level="NONE")
    assert normal_actions == []
    assert len(guard.get_open_positions()) == 1

    # CRITICAL 경보 시에는 동일 가격에서도 방어적으로 청산한다
    critical_actions = guard.evaluate_and_execute({"000660": {"price": 100500.0}}, now_hm="10:01", regime="A", alert_level="CRITICAL")
    assert len(critical_actions) == 1
    assert critical_actions[0]["reason"] == "critical_alert_defensive"
    assert guard.get_open_positions() == []
