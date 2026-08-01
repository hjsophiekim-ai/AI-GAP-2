"""
test_hynix_pullback_gate_engine.py — evaluate_pullback_gate()의 대기/데드라인 강제진입 검증.
"""

from __future__ import annotations

from datetime import datetime

import app.services.hynix_switch_engine as engine
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL


def test_waits_when_no_pullback_and_deadline_not_reached(monkeypatch):
    monkeypatch.setattr(engine, "detect_pullback", lambda df: {"is_pullback": False, "reason": "대기"})
    state = {}
    now = datetime(2026, 7, 9, 9, 20)  # 09:10~10:00 구간, 아직 여유 있음

    result = engine.evaluate_pullback_gate(state, HYNIX_SYMBOL, "HYNIX_BUY", now, {}, None, "mock")

    assert result["proceed"] is False
    assert state["pending_entry"]["symbol"] == HYNIX_SYMBOL


def test_forces_entry_at_morning_deadline(monkeypatch):
    monkeypatch.setattr(engine, "detect_pullback", lambda df: {"is_pullback": False, "reason": "대기"})
    # 09:15부터 눌림목을 기다려온 신호가 09:10~10:00 창 마감(10:00)까지도 눌림목이 안 나온 경우
    state = {"pending_entry": {"action": "HYNIX_BUY", "symbol": HYNIX_SYMBOL, "since": "2026-07-09T09:15:00"}}
    now = datetime(2026, 7, 9, 10, 0)

    result = engine.evaluate_pullback_gate(state, HYNIX_SYMBOL, "HYNIX_BUY", now, {}, None, "mock")

    assert result["proceed"] is True
    assert "데드라인" in result["message"]


def test_proceeds_immediately_when_pullback_detected(monkeypatch):
    monkeypatch.setattr(engine, "detect_pullback", lambda df: {"is_pullback": True, "reason": "눌림목 확인"})
    state = {}
    now = datetime(2026, 7, 9, 9, 15)

    result = engine.evaluate_pullback_gate(state, HYNIX_SYMBOL, "HYNIX_BUY", now, {}, None, "mock")

    assert result["proceed"] is True
    assert "눌림목" in result["message"]


def test_afternoon_signal_forces_after_patience_window(monkeypatch):
    monkeypatch.setattr(engine, "detect_pullback", lambda df: {"is_pullback": False, "reason": "대기"})
    state = {"pending_entry": {"action": "HYNIX_BUY", "symbol": HYNIX_SYMBOL, "since": "2026-07-09T13:30:00"}}
    now = datetime(2026, 7, 9, 13, 46)  # since로부터 16분 경과 (패턴스 15분 초과)

    result = engine.evaluate_pullback_gate(state, HYNIX_SYMBOL, "HYNIX_BUY", now, {}, None, "mock")

    assert result["proceed"] is True


def test_forced_window_end_caps_deadline(monkeypatch):
    monkeypatch.setattr(engine, "detect_pullback", lambda df: {"is_pullback": False, "reason": "대기"})
    state = {"pending_entry": {"action": "HYNIX_BUY", "symbol": HYNIX_SYMBOL, "since": "2026-07-09T13:30:00"}}
    now = datetime(2026, 7, 9, 13, 40)  # patience(15분)로는 아직 대기중이지만 강제거래창이 13:40에 끝남

    result = engine.evaluate_pullback_gate(state, HYNIX_SYMBOL, "HYNIX_BUY", now, {"window": "13:30-13:40"}, None, "mock")

    assert result["proceed"] is True
    assert "데드라인" in result["message"]
