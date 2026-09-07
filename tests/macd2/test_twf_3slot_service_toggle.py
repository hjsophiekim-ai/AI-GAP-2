"""TWF 3-SLOT 토글 서비스 레이어 (2026-09-07).

tests/macd2/test_service.py 와 같은 하네스 — conftest.py 의 autouse tmp_path
상태격리를 그대로 쓰고 실제 브로커/KIS 는 건드리지 않는다(토글 커맨드는 상태만
갱신하고 주문을 내지 않는다).

검증 대상
  - 4-way 상호배제 (TW2 / TEGv2 / TW2 3-SLOT / TWF 3-SLOT)
  - 조기익절이 두 3-SLOT 전략 각각에서 독립적으로 ON/OFF 되고,
    "둘 다 꺼질 때만" 자동 비활성화된다
"""
from __future__ import annotations

from app.trading.macd2 import config, service as service_module, state_store


def _svc():
    return service_module.Macd2Service()


def _state():
    return state_store.load_state()


class TestMutualExclusion:
    def test_twf_on_forces_tw2_teg_and_tw2_3slot_off(self):
        svc = _svc()
        svc.set_time_window_2_filter_enabled(True, changed_by="t")
        svc.set_time_window_teg_filter_enabled(True, changed_by="t")
        res = svc.set_time_window_twf_filter_enabled(True, changed_by="t")
        assert res["ok"] is True
        s = _state()
        assert s.time_window_twf_filter_enabled is True
        assert s.time_window_2_filter_enabled is False
        assert s.time_window_teg_filter_enabled is False
        assert s.time_window_3slot_filter_enabled is False

    def test_tw2_3slot_on_forces_twf_off(self):
        svc = _svc()
        svc.set_time_window_twf_filter_enabled(True, changed_by="t")
        assert _state().time_window_twf_filter_enabled is True
        svc.set_time_window_3slot_filter_enabled(True, changed_by="t")
        s = _state()
        assert s.time_window_3slot_filter_enabled is True
        assert s.time_window_twf_filter_enabled is False

    def test_tw2_on_forces_twf_off(self, monkeypatch):
        """2026-09-07: TW2 는 UI 에서 숨겨졌지만 **코드 경로는 그대로**다.
        복구 플래그를 켜면 예전과 똑같이 상호배제가 동작해야 한다."""
        monkeypatch.setattr(config, "SHOW_LEGACY_TW2_TOGGLES", True)
        svc = _svc()
        svc.set_time_window_twf_filter_enabled(True, changed_by="t")
        svc.set_time_window_2_filter_enabled(True, changed_by="t")
        s = _state()
        assert s.time_window_2_filter_enabled is True
        assert s.time_window_twf_filter_enabled is False

    def test_teg_on_forces_twf_off(self, monkeypatch):
        monkeypatch.setattr(config, "SHOW_LEGACY_TW2_TOGGLES", True)
        svc = _svc()
        svc.set_time_window_twf_filter_enabled(True, changed_by="t")
        svc.set_time_window_teg_filter_enabled(True, changed_by="t")
        s = _state()
        assert s.time_window_teg_filter_enabled is True
        assert s.time_window_twf_filter_enabled is False

    def test_hidden_tw2_cannot_come_back_alive(self):
        """숨김 기본값에서는 TW2 를 켜려 해도 복원 시 꺼진다 -- 사용자에게
        보이지 않는 전략이 몰래 활성화돼 있는 상태를 만들지 않는다."""
        svc = _svc()
        svc.set_time_window_2_filter_enabled(True, changed_by="t")
        assert _state().time_window_2_filter_enabled is False
        svc.set_time_window_teg_filter_enabled(True, changed_by="t")
        assert _state().time_window_teg_filter_enabled is False

    def test_never_two_modes_live_at_once(self):
        """어떤 순서로 켜도 이 tier 에서 동시에 살아남는 것은 없다.
        (숨김 기본값에서는 TW2/TEG 가 복원 시 꺼지므로 0개일 수 있다 --
        '둘 이상'이 없다는 것이 지켜야 할 불변식이다.)"""
        svc = _svc()
        for setter in (svc.set_time_window_2_filter_enabled,
                       svc.set_time_window_3slot_filter_enabled,
                       svc.set_time_window_twf_filter_enabled,
                       svc.set_time_window_2_filter_enabled,
                       svc.set_time_window_twf_filter_enabled):
            setter(True, changed_by="t")
            s = _state()
            live = sum([
                bool(s.time_window_2_filter_enabled),
                bool(s.time_window_3slot_filter_enabled),
                bool(s.time_window_twf_filter_enabled),
            ])
            assert live <= 1, (
                f"동시 활성 {live}개: TW2={s.time_window_2_filter_enabled} "
                f"3SLOT={s.time_window_3slot_filter_enabled} "
                f"TWF={s.time_window_twf_filter_enabled}"
            )

    def test_two_visible_strategies_are_mutually_exclusive(self):
        """사용자에게 보이는 두 전략은 정확히 하나만 선택된다."""
        svc = _svc()
        for setter in (svc.set_time_window_3slot_filter_enabled,
                       svc.set_time_window_twf_filter_enabled,
                       svc.set_time_window_3slot_filter_enabled):
            setter(True, changed_by="t")
            s = _state()
            live = sum([bool(s.time_window_3slot_filter_enabled),
                        bool(s.time_window_twf_filter_enabled)])
            assert live == 1, (
                f"3SLOT={s.time_window_3slot_filter_enabled} "
                f"TWF={s.time_window_twf_filter_enabled}"
            )

    def test_version_stamped(self):
        svc = _svc()
        svc.set_time_window_twf_filter_enabled(True, changed_by="t")
        assert _state().time_window_twf_filter_version == config.TWF_3SLOT_FILTER_VERSION


class TestEarlyTakeProfitIndependence:
    def test_can_enable_under_twf(self):
        svc = _svc()
        svc.set_time_window_twf_filter_enabled(True, changed_by="t")
        res = svc.set_early_tp_filter_enabled(True, changed_by="t")
        assert res["ok"] is True
        assert _state().early_tp_filter_enabled is True

    def test_can_enable_under_tw2_3slot(self):
        svc = _svc()
        svc.set_time_window_3slot_filter_enabled(True, changed_by="t")
        res = svc.set_early_tp_filter_enabled(True, changed_by="t")
        assert res["ok"] is True
        assert _state().early_tp_filter_enabled is True

    def test_rejected_when_no_3slot_mode_live(self):
        svc = _svc()
        svc.set_time_window_3slot_filter_enabled(False, changed_by="t")
        svc.set_time_window_twf_filter_enabled(False, changed_by="t")
        res = svc.set_early_tp_filter_enabled(True, changed_by="t")
        assert res["ok"] is False
        assert res["reason"] == "TW2_3SLOT_REQUIRED"
        assert _state().early_tp_filter_enabled is False

    def test_can_toggle_off_and_on_within_twf(self):
        svc = _svc()
        svc.set_time_window_twf_filter_enabled(True, changed_by="t")
        svc.set_early_tp_filter_enabled(True, changed_by="t")
        svc.set_early_tp_filter_enabled(False, changed_by="t")
        assert _state().early_tp_filter_enabled is False
        assert _state().time_window_twf_filter_enabled is True
        svc.set_early_tp_filter_enabled(True, changed_by="t")
        assert _state().early_tp_filter_enabled is True

    def test_switching_twf_to_tw2_3slot_keeps_early_tp_on(self):
        """전략을 갈아타도 조기익절은 살아 있다 -- 공통 서브필터이기 때문."""
        svc = _svc()
        svc.set_time_window_twf_filter_enabled(True, changed_by="t")
        svc.set_early_tp_filter_enabled(True, changed_by="t")
        svc.set_time_window_3slot_filter_enabled(True, changed_by="t")
        s = _state()
        assert s.time_window_3slot_filter_enabled is True
        assert s.time_window_twf_filter_enabled is False
        assert s.early_tp_filter_enabled is True

    def test_switching_tw2_3slot_to_twf_keeps_early_tp_on(self):
        svc = _svc()
        svc.set_time_window_3slot_filter_enabled(True, changed_by="t")
        svc.set_early_tp_filter_enabled(True, changed_by="t")
        svc.set_time_window_twf_filter_enabled(True, changed_by="t")
        s = _state()
        assert s.time_window_twf_filter_enabled is True
        assert s.time_window_3slot_filter_enabled is False
        assert s.early_tp_filter_enabled is True

    def test_early_tp_auto_off_only_when_both_modes_off(self):
        svc = _svc()
        svc.set_time_window_twf_filter_enabled(True, changed_by="t")
        svc.set_early_tp_filter_enabled(True, changed_by="t")
        svc.set_time_window_twf_filter_enabled(False, changed_by="t")
        s = _state()
        assert s.time_window_twf_filter_enabled is False
        assert s.time_window_3slot_filter_enabled is False
        assert s.early_tp_filter_enabled is False
        assert s.early_tp_filter_enabled_by == "AUTO_TWF_3SLOT_DISABLED"

    def test_two_3slot_modes_can_never_be_live_together(self):
        """조기익절 자동해제 분기에 있는 `and not <다른 3-SLOT 토글>` 가드는
        **방어적 코드**다 -- 두 모드가 동시에 살아 있는 상태 자체를 만들 수
        없기 때문. 상호배제(service)를 우회해 손으로 써 넣어도
        state_store.load_state 가 복원 시점에 떨어뜨린다."""
        svc = _svc()
        svc.set_time_window_3slot_filter_enabled(True, changed_by="t")
        s = state_store.load_state()
        s.time_window_twf_filter_enabled = True   # 상호배제 우회 시도
        state_store.save_state(s)
        back = _state()
        assert not (back.time_window_3slot_filter_enabled
                    and back.time_window_twf_filter_enabled)

    def test_tw2_3slot_off_disables_early_tp(self):
        """TW2 3-SLOT 을 끄면(그때 TWF 도 당연히 OFF) 조기익절이 함께 꺼진다 --
        도입 이전과 동일한 동작."""
        svc = _svc()
        svc.set_time_window_3slot_filter_enabled(True, changed_by="t")
        svc.set_early_tp_filter_enabled(True, changed_by="t")
        svc.set_time_window_3slot_filter_enabled(False, changed_by="t")
        s = _state()
        assert s.early_tp_filter_enabled is False
        assert s.early_tp_filter_enabled_by == "AUTO_TW2_3SLOT_DISABLED"
