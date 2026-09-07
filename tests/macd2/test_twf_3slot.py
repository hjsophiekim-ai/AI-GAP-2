"""TWF 3-SLOT (2026-09-07) — 진입은 TW2 3-SLOT 과 동일, 청산 3개만 다름.

이 파일이 지키는 계약
  1. TW2 3-SLOT / TW2 / TEGv2 / MU_MACD 의 청산 동작이 **조금도** 바뀌지 않는다
     (override 를 안 주면 기존 모듈 상수 그대로).
  2. TWF override 3개가 정확히 config.TWF_* 에서 온다.
  3. 두 3-SLOT 모드가 같은 진입 경로/슬롯 카운터를 공유한다.
  4. 4-way 토글 상호배제.
  5. 조기익절이 두 전략 각각에서 독립적으로 ON/OFF 된다.
"""
from __future__ import annotations

import pytest

from app.trading.macd2 import config
from app.trading.macd2 import early_take_profit
from app.trading.macd2 import time_window_3slot as t3
from app.trading.macd2 import time_window_position_manager as twpm
from app.trading.macd2.models import RuntimeState


# ── 1. 기존 청산 동작 불변 (override 미지정) ────────────────────────────────
class TestExistingLadderUnchanged:
    def test_morning_stop_loss_still_minus_1_7(self):
        assert twpm.MORNING_STOP_LOSS_PCT == pytest.approx(-1.7)
        assert twpm.evaluate_position(
            session="MORNING", net_return_pct=-1.65, tp1_done=False,
        ).exit_reason is None
        assert twpm.evaluate_position(
            session="MORNING", net_return_pct=-1.75, tp1_done=False,
        ).exit_reason == config.EXIT_TW_STOP_LOSS

    def test_after_tp1_stop_still_plus_0_3(self):
        assert twpm.MORNING_AFTER_TP1_STOP_PCT == pytest.approx(0.3)
        d = twpm.evaluate_position(
            session="MORNING", net_return_pct=0.25, tp1_done=True, peak_net_return=3.1)
        assert d.exit_reason == config.EXIT_TW_AFTER_TP1_STOP
        assert d.label == "AFTER_TP1_STOP"
        assert twpm.evaluate_position(
            session="MORNING", net_return_pct=1.9, tp1_done=True, peak_net_return=3.1,
        ).exit_reason is None

    def test_trailing_stop_label_unchanged(self):
        d = twpm.evaluate_position(
            session="MORNING", net_return_pct=1.9, tp1_done=True, peak_net_return=3.6)
        assert d.exit_reason == config.EXIT_TW_TRAILING_STOP
        assert d.label == "TRAILING_STOP"

    def test_afternoon_tp_still_2_5(self):
        assert twpm.AFTERNOON_TP_PCT == pytest.approx(2.5)
        assert twpm.evaluate_position(
            session="AFTERNOON", net_return_pct=2.6, tp1_done=False,
        ).exit_reason == config.EXIT_TW_AFTERNOON_TP
        assert twpm.evaluate_take_profit_immediate(
            session="AFTERNOON", net_return_pct=2.6, tp1_done=False,
        ).exit_reason == config.EXIT_TW_AFTERNOON_TP

    def test_tp1_tp2_unchanged(self):
        assert twpm.evaluate_position(
            session="MORNING", net_return_pct=3.1, tp1_done=False,
        ).exit_reason == config.EXIT_TW_TP1_PARTIAL
        assert twpm.evaluate_position(
            session="MORNING", net_return_pct=5.1, tp1_done=False,
        ).exit_reason == config.EXIT_TW_TP2_FULL


# ── 2. TWF override ────────────────────────────────────────────────────────
class TestTwfOverrides:
    def test_exit_overrides_come_from_config(self):
        ov = t3.exit_overrides(t3.MODE_TWF_3SLOT)
        assert ov["stop_loss_pct_override"] == pytest.approx(config.TWF_MORNING_STOP_LOSS * 100.0)
        assert ov["after_tp1_stop_pct_override"] == pytest.approx(config.TWF_MORNING_AFTER_TP1_STOP * 100.0)
        assert ov["afternoon_tp_pct_override"] == pytest.approx(config.TWF_AFTERNOON_TP * 100.0)

    def test_validated_values(self):
        """백테스트가 검증한 값 그대로 (SL -1.4 / afterTP1 +2.0 / 오후TP +3.0)."""
        ov = t3.exit_overrides(t3.MODE_TWF_3SLOT)
        assert ov["stop_loss_pct_override"] == pytest.approx(-1.4)
        assert ov["after_tp1_stop_pct_override"] == pytest.approx(2.0)
        assert ov["afternoon_tp_pct_override"] == pytest.approx(3.0)

    @pytest.mark.parametrize("mode", [t3.MODE_TW2_3SLOT, "TW2", "TEGv2", None, ""])
    def test_non_twf_modes_get_no_override(self, mode):
        assert t3.exit_overrides(mode) == {
            "stop_loss_pct_override": None,
            "after_tp1_stop_pct_override": None,
            "afternoon_tp_pct_override": None,
        }

    def test_twf_stop_loss_fires_earlier(self):
        ov = t3.exit_overrides(t3.MODE_TWF_3SLOT)
        assert twpm.evaluate_position(
            session="MORNING", net_return_pct=-1.45, tp1_done=False, **ov,
        ).exit_reason == config.EXIT_TW_STOP_LOSS
        # 같은 값에서 TW2 3-SLOT 은 아직 안 끊는다
        assert twpm.evaluate_position(
            session="MORNING", net_return_pct=-1.45, tp1_done=False,
            **t3.exit_overrides(t3.MODE_TW2_3SLOT),
        ).exit_reason is None

    def test_twf_after_tp1_stop_is_2_0_and_labelled_correctly(self):
        ov = t3.exit_overrides(t3.MODE_TWF_3SLOT)
        d = twpm.evaluate_position(
            session="MORNING", net_return_pct=1.9, tp1_done=True, peak_net_return=3.1, **ov)
        assert d.exit_reason == config.EXIT_TW_AFTER_TP1_STOP
        # peak < trailing trigger 이므로 after-TP1 가지다 — 값이 trailing 과
        # 같아도 라벨이 TRAILING_STOP 으로 새지 않아야 한다.
        assert d.label == "AFTER_TP1_STOP"

    def test_twf_afternoon_tp_is_3_0(self):
        ov = t3.exit_overrides(t3.MODE_TWF_3SLOT)
        assert twpm.evaluate_position(
            session="AFTERNOON", net_return_pct=2.6, tp1_done=False, **ov,
        ).exit_reason is None
        assert twpm.evaluate_position(
            session="AFTERNOON", net_return_pct=3.05, tp1_done=False, **ov,
        ).exit_reason == config.EXIT_TW_AFTERNOON_TP
        assert twpm.evaluate_take_profit_immediate(
            session="AFTERNOON", net_return_pct=3.05, tp1_done=False,
            afternoon_tp_pct_override=ov["afternoon_tp_pct_override"],
        ).exit_reason == config.EXIT_TW_AFTERNOON_TP

    def test_twf_leaves_tp1_tp2_alone(self):
        ov = t3.exit_overrides(t3.MODE_TWF_3SLOT)
        assert twpm.evaluate_position(
            session="MORNING", net_return_pct=3.1, tp1_done=False, **ov,
        ).exit_reason == config.EXIT_TW_TP1_PARTIAL
        assert twpm.evaluate_position(
            session="MORNING", net_return_pct=6.1, tp1_done=False,
            tp2_pct_override=config.TW2_MORNING_TP2 * 100.0, **ov,
        ).exit_reason == config.EXIT_TW_TP2_FULL


# ── 3. 모드 헬퍼 ───────────────────────────────────────────────────────────
class TestModeHelpers:
    def test_modes_tuple(self):
        assert t3.MODES_3SLOT == ("TW2_3SLOT", "TWF_3SLOT")

    def test_active_mode(self):
        s = RuntimeState()
        assert t3.active_3slot_mode(s) is None
        assert not t3.is_3slot_enabled(s)
        s.time_window_3slot_filter_enabled = True
        assert t3.active_3slot_mode(s) == t3.MODE_TW2_3SLOT
        s.time_window_3slot_filter_enabled = False
        s.time_window_twf_filter_enabled = True
        assert t3.active_3slot_mode(s) == t3.MODE_TWF_3SLOT
        assert t3.is_3slot_enabled(s)

    def test_both_on_is_deterministic(self):
        """방어적: 손상된 상태로 둘 다 켜져도 TW2 3-SLOT 이 이긴다(기존 동작 보존)."""
        s = RuntimeState()
        s.time_window_3slot_filter_enabled = True
        s.time_window_twf_filter_enabled = True
        assert t3.active_3slot_mode(s) == t3.MODE_TW2_3SLOT

    def test_resolve_slot_is_shared_and_capped(self):
        """TWF 는 resolve_slot 을 그대로 쓴다 — 하루 3회 cap 은 여기 하나뿐이다."""
        from datetime import datetime
        now = datetime(2026, 9, 7, 13, 30, tzinfo=config.KST)
        d = t3.resolve_slot(now=now, slots_used_today=config.TW2_3SLOT_DAILY_CAP,
                            morning_count=3, afternoon_count=0, direction="UP_RED",
                            is_flat=True, last_afternoon_direction=None)
        assert not d.slot_allowed
        assert d.reject_reason == t3.REJECT_SLOT_CAP

    def test_afternoon_slot_available_when_morning_left_room(self):
        from datetime import datetime
        now = datetime(2026, 9, 7, 13, 30, tzinfo=config.KST)
        d = t3.resolve_slot(now=now, slots_used_today=1, morning_count=1,
                            afternoon_count=0, direction="UP_RED", is_flat=True,
                            last_afternoon_direction=None)
        assert d.slot_allowed and d.slot_number == 2
        assert d.session == t3.SESSION_AFTERNOON
        assert d.requires_teg_gate


# ── 4. 조기익절 — 두 전략 공통, 각각 독립 ON/OFF ───────────────────────────
class TestEarlyTakeProfitIsSharedFilter:
    def test_needs_a_3slot_mode(self):
        s = RuntimeState()
        s.early_tp_filter_enabled = True
        assert not early_take_profit.is_enabled(s)

    @pytest.mark.parametrize("flag", ["time_window_3slot_filter_enabled",
                                      "time_window_twf_filter_enabled"])
    def test_enabled_under_either_mode(self, flag):
        s = RuntimeState()
        s.early_tp_filter_enabled = True
        setattr(s, flag, True)
        assert early_take_profit.is_enabled(s)

    @pytest.mark.parametrize("flag", ["time_window_3slot_filter_enabled",
                                      "time_window_twf_filter_enabled"])
    def test_off_toggle_disables_under_either_mode(self, flag):
        s = RuntimeState()
        s.early_tp_filter_enabled = False
        setattr(s, flag, True)
        assert not early_take_profit.is_enabled(s)

    @pytest.mark.parametrize("mode", [t3.MODE_TW2_3SLOT, t3.MODE_TWF_3SLOT])
    def test_is_active_for_both_modes(self, mode):
        s = RuntimeState()
        s.early_tp_filter_enabled = True
        s.time_window_3slot_filter_enabled = (mode == t3.MODE_TW2_3SLOT)
        s.time_window_twf_filter_enabled = (mode == t3.MODE_TWF_3SLOT)
        s.time_window_position_active = True
        s.time_window_active_mode = mode
        assert early_take_profit.is_active(s)

    def test_not_active_for_plain_tw2_position(self):
        s = RuntimeState()
        s.early_tp_filter_enabled = True
        s.time_window_twf_filter_enabled = True
        s.time_window_position_active = True
        s.time_window_active_mode = "TW2"
        assert not early_take_profit.is_active(s)

    def test_evaluate_itself_is_untouched(self):
        """조기익절 판정식 자체는 전략과 무관하게 동일하다."""
        d = early_take_profit.evaluate(
            entry_chop=True, peak_net_return_pct=1.6, net_return_pct=0.7)
        assert d.exit_reason == config.EXIT_EARLY_TAKE_PROFIT
        assert early_take_profit.evaluate(
            entry_chop=False, peak_net_return_pct=1.6, net_return_pct=0.7,
        ).exit_reason is None


# ── 5. 토글 상호배제 / 영속화 ──────────────────────────────────────────────
class TestToggleWiring:
    def test_state_defaults(self):
        s = RuntimeState()
        assert s.time_window_twf_filter_enabled is False
        assert s.time_window_twf_filter_version == ""

    def test_config_defaults_off(self):
        assert config.TWF_3SLOT_FILTER_DEFAULT is False
        assert config.TWF_3SLOT_FILTER_VERSION == "TWF_3SLOT_V1_20260907"

    def test_state_store_roundtrip(self, tmp_path, monkeypatch):
        from app.trading.macd2 import state_store
        monkeypatch.setattr(state_store, "STATE_PATH", tmp_path / "state.json", raising=False)
        s = state_store.load_state()
        s.time_window_twf_filter_enabled = True
        s.time_window_twf_filter_version = config.TWF_3SLOT_FILTER_VERSION
        s.time_window_3slot_filter_enabled = False
        s.time_window_2_filter_enabled = False
        s.time_window_teg_filter_enabled = False
        state_store.save_state(s)
        back = state_store.load_state()
        assert back.time_window_twf_filter_enabled is True

    def test_state_store_defensively_drops_twf_when_another_mode_on(self, tmp_path, monkeypatch):
        from app.trading.macd2 import state_store
        monkeypatch.setattr(state_store, "STATE_PATH", tmp_path / "state.json", raising=False)
        s = state_store.load_state()
        s.time_window_twf_filter_enabled = True
        s.time_window_3slot_filter_enabled = True
        state_store.save_state(s)
        back = state_store.load_state()
        assert not (back.time_window_twf_filter_enabled
                    and back.time_window_3slot_filter_enabled)
