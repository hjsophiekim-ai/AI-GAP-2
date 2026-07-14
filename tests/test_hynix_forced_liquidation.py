"""
test_hynix_forced_liquidation.py — 15:15 강제청산 상태 정합성 + 관련 UI/모델 회귀 테스트.

요구된 5개 케이스:
  1) 15:15 이후 보유종목 없음이면 liquidation_done=True
  2) 15:15 이후 보유종목 있으면 강제청산 시도(매도 실행)
  3) 보유종목 없음이면 최근 매수 가격이 UI에서 '—' 처리되는지(소스 패턴 검증)
  4) Debug 버튼이 화면에 표시되는지(소스 패턴 검증)
  5) 마이크론 1분/3분 데이터 실패 시 fallback 점수가 표시되는지(None 노출 금지)

추가로 15:15 강제청산도 손절모드(ALERT_ONLY)를 따르는지(자동매도 차단) 검증한다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import app.trading.hynix_stop_loss_control as slc
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL, HYNIX_NAME
from app.models import OrderResult, Position
from app.trading.hynix_switch_position_manager import run_liquidation_if_needed
from app.trading.hynix_position_common import HynixPositionManager

_UI_PAGE_PATH = (
    Path(__file__).resolve().parent.parent
    / "app" / "ui" / "pages" / "9_SK하이닉스_자동매매.py"
)


class _FakeBroker:
    def __init__(self, positions=None, cash=10_000_000.0):
        self._positions = positions or []
        self._cash = cash
        self.sell_calls = []

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        self.sell_calls.append((symbol, quantity, price))
        remaining = []
        for p in self._positions:
            if p.symbol == symbol:
                if p.quantity > quantity:
                    p.quantity -= quantity
                    remaining.append(p)
            else:
                remaining.append(p)
        self._positions = remaining
        return OrderResult(success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                            side="sell", quantity=quantity, price=price, order_type=order_type, order_id="L1", message="ok")

    def get_positions(self):
        return self._positions

    def get_buyable_cash(self):
        return self._cash


def _empty_state(mode="mock"):
    return {
        "mode": mode, "position": {"symbol": None, "quantity": 0}, "stop_loss_mode": slc.STOP_LOSS_MODE_AUTO,
        "auto_trade_on": True,
    }


def test_liquidation_done_true_when_no_position_after_1515(tmp_path, monkeypatch):
    monkeypatch.setattr(slc, "_FORCED_LIQUIDATION_LOG_PATH", tmp_path / "forced_liquidation_log.csv")
    state = _empty_state()
    now = datetime.now().replace(hour=15, minute=16, second=0, microsecond=0)

    result = run_liquidation_if_needed(now, state, broker=_FakeBroker(), hynix_price=100_000.0, inverse_price=5_000.0)

    assert state["liquidation_done"] is True
    assert result["liquidated"] is False
    assert result.get("already_empty") is True


def test_liquidation_attempts_sell_when_position_held_after_1515(tmp_path, monkeypatch):
    monkeypatch.setattr(slc, "_FORCED_LIQUIDATION_LOG_PATH", tmp_path / "forced_liquidation_log.csv")
    state = _empty_state()
    state["position"] = {"symbol": HYNIX_SYMBOL, "name": HYNIX_NAME, "quantity": 10, "avg_price": 100_000.0, "entry_price": 100_000.0}
    now = datetime.now().replace(hour=15, minute=16, second=0, microsecond=0)

    hynix_position = Position(symbol=HYNIX_SYMBOL, name=HYNIX_NAME, quantity=10, avg_price=100_000.0, current_price=99_000.0)
    broker = _FakeBroker(positions=[hynix_position])
    pm = HynixPositionManager(broker, mode="mock")

    result = run_liquidation_if_needed(now, state, broker=broker, hynix_price=99_000.0, inverse_price=5_000.0, position_manager=pm)

    assert len(broker.sell_calls) == 1
    assert result["liquidated"] is True
    assert state["liquidation_done"] is True
    assert state["position"]["symbol"] is None

    log_path = tmp_path / "forced_liquidation_log.csv"
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8-sig")
    assert "SUCCESS" in content


def test_forced_liquidation_blocked_in_alert_only_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(slc, "_FORCED_LIQUIDATION_LOG_PATH", tmp_path / "forced_liquidation_log.csv")
    state = _empty_state()
    state["stop_loss_mode"] = slc.STOP_LOSS_MODE_ALERT_ONLY
    state["position"] = {"symbol": HYNIX_SYMBOL, "name": HYNIX_NAME, "quantity": 10, "avg_price": 100_000.0, "entry_price": 100_000.0}
    now = datetime.now().replace(hour=15, minute=16, second=0, microsecond=0)

    hynix_position = Position(symbol=HYNIX_SYMBOL, name=HYNIX_NAME, quantity=10, avg_price=100_000.0, current_price=99_000.0)
    broker = _FakeBroker(positions=[hynix_position])

    result = run_liquidation_if_needed(now, state, broker=broker, hynix_price=99_000.0, inverse_price=5_000.0)

    assert len(broker.sell_calls) == 0  # 자동매도 차단됨
    assert result["liquidated"] is False
    assert result.get("blocked_by_mode") is True
    assert state["liquidation_done"] is False  # 보유 포지션이 여전히 남아있으므로 완료로 볼 수 없음
    assert state["pending_manual_stop_loss_alert"]["symbol"] == HYNIX_SYMBOL


def test_ui_shows_dash_for_position_dependent_metrics_when_empty():
    """보유종목 없음이면 미실현손익/자동손절·익절 기준가가 '—'로 표시되는지,
    보유 중이면 원장 기반 평균매수가/최초진입시각 등이 표시되는지(소스 패턴 검증)."""
    source = _UI_PAGE_PATH.read_text(encoding="utf-8")
    assert "_has_position = bool(position.get(\"symbol\"))" in source
    assert '"평균 매수가"' in source and "compute_current_position_detail" in source
    assert '"현재 미실현손익(순손익)"' in source and 'if _has_position else "—"' in source
    assert '"자동손절 기준가"' in source and '"자동익절 기준가"' in source


def test_ui_has_broker_debug_panel_button():
    source = _UI_PAGE_PATH.read_text(encoding="utf-8")
    assert "🔍 Broker Debug Panel" in source
    assert 'st.button("🔍 Broker Debug Panel"' in source


def test_ui_trade_history_table_uses_execution_ledger_not_legacy_csv():
    """오늘 거래내역 표와 BUY/SELL 정합성 진단이 execution ledger를 기준으로 하는지
    검증한다 — legacy hynix_auto_trade_log_{date}.csv는 Dynamic Exit AI(1초 감시)의
    매도를 기록하지 않아 "매수만 보이고 매도가 안 보인다"는 사고(2026-07-13)의
    원인이었다(소스 패턴 검증)."""
    source = _UI_PAGE_PATH.read_text(encoding="utf-8")
    assert "from app.services.hynix_execution_ledger import load_ledger" in source
    assert "오늘 거래내역 (원장 기준" in source
    # 표시/진단 모두 legacy per-day CSV(pd.read_csv(...hynix_auto_trade_log_...))를
    # 더 이상 직접 읽지 않아야 한다(주석에서의 언급은 허용).
    assert "pd.read_csv(trade_log_path)" not in source
    assert 'pd.read_csv(_today_trade_log_path)' not in source


def test_micron_fallback_used_when_1min_3min_missing(tmp_path, monkeypatch):
    """1분/3분봉 데이터가 없어도 None을 그대로 노출하지 않고 fallback 점수/상태를 표시한다."""
    import app.models.hynix_micron_realtime_score as mscore

    monkeypatch.setattr(mscore, "_MU_1MIN_CSV", tmp_path / "no_1min.csv")
    monkeypatch.setattr(mscore, "_MU_3MIN_CSV", tmp_path / "no_3min.csv")

    def _fake_collect_mu_extended_hours(mode=None):
        return {"session_type": "CLOSED", "mu_extended_hours_score": 42.0, "timestamp": "2026-07-09T16:00:00"}

    monkeypatch.setattr(
        "app.data_sources.mu_extended_hours_collector.collect_mu_extended_hours",
        _fake_collect_mu_extended_hours,
    )

    def _fake_compute_micron_features(raw_1min):
        return {"micron_session_strength_score": None}

    monkeypatch.setattr(
        "app.features.micron_premarket_features.compute_micron_features",
        _fake_compute_micron_features,
    )

    result = mscore.calculate_existing_micron_score(mode="mock")

    assert result["micron_1min_score"] is None
    assert result["micron_3min_score"] is None
    assert result["micron_fallback_used"] is True
    assert result["micron_data_status"] == mscore.STATUS_STALE_DATA
    assert result["existing_micron_score"] == 50.0
    assert result["source"] == "stale_micron_display_only"
