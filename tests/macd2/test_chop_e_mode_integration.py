"""N1 / P3 / E / CHOP 네 모드 통합 — main(E) 위에 CHOP 을 얹은 뒤의 운영 계약 (2026-10-12).

``test_chop_mode_lifecycle.py`` 는 E 가 없던 브랜치에서 쓰였다. 여기서는
main-MACD2 의 E 와 합쳐진 뒤에도 다음이 지켜지는지 잠근다.

  * E 와 CHOP 은 별도 모드이며 동시에 켜지지 않는다
  * N1 -> P3 -> CHOP -> P3 (및 E 경유) 전환이 재시작을 넘어 유지된다
  * MC / 구조손절 하루상태가 직렬화·복원되고 날짜가 바뀌면 리셋된다
  * CHOP 이 아니면 방어 3종이 주문 판단에 영향을 주지 않는다
  * 하루 진입 한도: N1/P3 3, E(토글 OFF) 3, E(토글 ON) 2, CHOP 2
  * CHOP 필드가 없던 옛 state 파일도 그대로 읽힌다
"""

from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import pytest

from app.trading.macd2 import chop_defense, config, e_strategy, state_store, strategy_mode, worker
from app.trading.macd2.models import Direction

KST = config.KST
ALL = (strategy_mode.MODE_N1, strategy_mode.MODE_P3, strategy_mode.MODE_E, strategy_mode.MODE_CHOP)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(state_store, "STATE_DIR_PATH", tmp_path)
    monkeypatch.setattr(state_store, "STATE_PATH", tmp_path / "runtime.json")
    return state_store


def _t(hhmm: str, day: int = 8) -> dt.datetime:
    return dt.datetime(2026, 10, day, int(hhmm[:2]), int(hhmm[2:]), tzinfo=KST)


def _restart(store, st):
    st.mode = "mock"
    store.save_state(st)
    return store.load_state()


def _chop_path_bars(day: int = 8, n: int = 70) -> pd.DataFrame:
    """09:00 부터 n 분 — 좁은 박스 안 톱니(PE 낮고 EMA20 교차 많음)."""
    ts = pd.date_range(dt.datetime(2026, 10, day, 9, 0), periods=n, freq="1min", tz=KST)
    close = [100.0 + (1.0 if (i // 3) % 2 else -1.0) for i in range(n)]
    return pd.DataFrame({"datetime": ts, "open": close, "high": [c + 0.5 for c in close],
                         "low": [c - 0.5 for c in close], "close": close, "volume": 1})


# ── 모드 정의 ─────────────────────────────────────────────────────────────
def test_four_modes_exist():
    assert strategy_mode.ALL_MODES == ALL
    assert strategy_mode.P3_BASED_MODES == (strategy_mode.MODE_P3, strategy_mode.MODE_E,
                                            strategy_mode.MODE_CHOP)


@pytest.mark.parametrize("mode", ALL)
def test_e_and_chop_are_never_both_on(store, mode):
    st = store.default_state()
    strategy_mode.apply(st, mode)
    flags = strategy_mode.derive(mode)
    assert not (flags["E"] and flags["CHOP_DEFENSE"])
    assert bool(st.e_enabled) is (mode == strategy_mode.MODE_E)
    assert bool(st.chop_defense_enabled) is (mode == strategy_mode.MODE_CHOP)
    assert e_strategy.is_active(st) is (mode == strategy_mode.MODE_E)
    assert chop_defense.is_active(st) is (mode == strategy_mode.MODE_CHOP)
    assert strategy_mode.infer_from_legacy(st)[0] == mode


def test_hand_made_e_plus_chop_state_falls_back_to_n1(store):
    """apply() 로는 만들 수 없는 조합 — 애매하면 N1."""
    st = store.default_state()
    strategy_mode.apply(st, strategy_mode.MODE_P3)
    st.e_enabled = True
    st.chop_defense_enabled = True
    mode, reason = strategy_mode.infer_from_legacy(st)
    assert mode == strategy_mode.MODE_N1
    assert reason == "CONFLICT_E_AND_CHOP"


def test_switching_e_to_chop_and_back_clears_the_other_side(store):
    st = store.default_state()
    strategy_mode.apply(st, strategy_mode.MODE_E)
    st.e_pending = {"signal_id": "x", "armed_at": _t("0910").isoformat()}
    strategy_mode.apply(st, strategy_mode.MODE_CHOP)
    assert st.e_enabled is False and st.e_pending is None      # E 대기주문 정리
    assert st.chop_defense_enabled is True and st.p3_enabled is True

    chop_defense.note_morning_flag(st, _t("0924"), Direction.UP_RED)
    st.chop_mc_on_at = "1012"
    strategy_mode.apply(st, strategy_mode.MODE_E)
    assert st.chop_defense_enabled is False
    assert st.chop_mc_on_at is None and st.chop_mc_flags == []  # CHOP 하루상태 정리
    assert st.e_enabled is True and st.p3_enabled is True


# ── 전환 + 재시작 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("account", ["mock", "real"])
def test_n1_p3_chop_p3_sequence_survives_restart_at_every_step(store, account):
    st = store.default_state()
    for mode in (strategy_mode.MODE_N1, strategy_mode.MODE_P3, strategy_mode.MODE_CHOP,
                 strategy_mode.MODE_P3, strategy_mode.MODE_E, strategy_mode.MODE_CHOP,
                 strategy_mode.MODE_N1):
        strategy_mode.apply(st, mode)
        st.mode = account
        store.save_state(st)
        st = store.load_state()
        assert strategy_mode.current(st) == mode, f"재시작 후 {mode} 가 유지되지 않았다"
        assert bool(st.chop_defense_enabled) is (mode == strategy_mode.MODE_CHOP)
        assert bool(st.e_enabled) is (mode == strategy_mode.MODE_E)


def test_mc_and_structural_stop_state_survive_restart(store):
    st = store.default_state()
    strategy_mode.apply(st, strategy_mode.MODE_CHOP)
    chop_defense.note_morning_flag(st, _t("0924"), Direction.UP_RED)
    chop_defense.note_morning_flag(st, _t("0936"), Direction.DOWN_BLUE)
    st.chop_mc_on_at = "1012"
    entry = _t("1015")
    st.chop_stop_key = entry.isoformat()
    st.chop_stop_first_at = _t("1021").isoformat()

    back = _restart(store, st)
    assert strategy_mode.current(back) == strategy_mode.MODE_CHOP
    assert back.chop_mc_day == "20261008"
    assert back.chop_mc_on_at == "1012"
    assert [f[1] for f in back.chop_mc_flags] == ["UP_RED", "DOWN_BLUE"]
    assert back.chop_stop_key == entry.isoformat()
    assert back.chop_stop_first_at == _t("1021").isoformat()
    # 같은 날 재시작 직후에도 no-trade 구간은 풀리지 않는다
    assert chop_defense.morning_no_trade(back, _t("1020"), _chop_path_bars()) is True
    # 유예 클럭도 재시작으로 리셋되지 않는다: 첫 터치 후 CHOP_STOP_DEFER_MIN 이 지나면 집행
    back.chop_box = [101.5, 98.5, 100.0]
    back.chop_last_close = 100.0
    later = _t("1021") + dt.timedelta(minutes=float(config.CHOP_STOP_DEFER_MIN) + 0.5)
    assert chop_defense.b3_stop_action(back, now=later, entry_at=entry, net_return_pct=-1.1,
                                       direction=Direction.UP_RED) == chop_defense.STOP_EXECUTE


# ── 날짜 rollover ─────────────────────────────────────────────────────────
def test_rollover_resets_mc_even_before_the_first_flag(store):
    """다음 날 첫 플래그 전에 morning_no_trade 가 먼저 불려도 전날 ON 이 남지 않는다."""
    st = store.default_state()
    strategy_mode.apply(st, strategy_mode.MODE_CHOP)
    chop_defense.note_morning_flag(st, _t("0924"), Direction.UP_RED)
    chop_defense.note_morning_flag(st, _t("0936"), Direction.DOWN_BLUE)
    st.chop_mc_on_at = "0950"
    st = _restart(store, st)

    nxt = _t("0905", day=9)
    assert chop_defense.morning_no_trade(st, nxt, _chop_path_bars(day=9, n=5)) is False
    assert st.chop_mc_day == "20261009"
    assert st.chop_mc_on_at is None and st.chop_mc_off_at is None
    assert st.chop_mc_flags == []


# ── CHOP 이 아니면 no-op ───────────────────────────────────────────────────
@pytest.mark.parametrize("mode", (strategy_mode.MODE_N1, strategy_mode.MODE_P3, strategy_mode.MODE_E))
def test_defenses_are_noop_outside_chop(store, mode):
    st = store.default_state()
    strategy_mode.apply(st, mode)
    bars = _chop_path_bars()
    # 오전 플래그 기록은 모드와 무관하게 남지만(설계) 판단에는 쓰이지 않는다
    for hhmm, d in (("0912", Direction.UP_RED), ("0921", Direction.DOWN_BLUE),
                    ("0930", Direction.UP_RED), ("0942", Direction.DOWN_BLUE)):
        chop_defense.note_morning_flag(st, _t(hhmm), d)
    assert chop_defense.morning_no_trade(st, _t("1000"), bars) is False
    assert st.chop_mc_on_at is None
    chop_defense.note_bars(st, _t("1000"), bars)
    assert st.chop_box is None and st.chop_bars_at is None
    assert chop_defense.b3_stop_action(st, now=_t("1001"), entry_at=_t("0955"), net_return_pct=-1.2,
                                       direction=Direction.UP_RED) == chop_defense.STOP_EXECUTE
    assert st.chop_stop_key is None


# ── 하루 진입 한도 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode, cap2_toggle, expected", [
    (strategy_mode.MODE_N1, False, 3), (strategy_mode.MODE_N1, True, 3),
    (strategy_mode.MODE_P3, False, 3), (strategy_mode.MODE_P3, True, 3),
    (strategy_mode.MODE_E, False, 3), (strategy_mode.MODE_E, True, 2),
    (strategy_mode.MODE_CHOP, False, 2), (strategy_mode.MODE_CHOP, True, 2),
])
def test_daily_entry_cap_per_mode(store, mode, cap2_toggle, expected):
    st = store.default_state()
    strategy_mode.apply(st, mode)
    st.e_daily_cap2_enabled = cap2_toggle
    assert int(config.TW2_3SLOT_DAILY_CAP) == 3
    assert worker._tw2_3slot_daily_cap(st) == expected


# ── 옛 state 파일 ─────────────────────────────────────────────────────────
_CHOP_KEYS = ("chop_defense_enabled", "chop_defense_version", "chop_mc_day", "chop_mc_flags",
              "chop_mc_on_at", "chop_mc_off_at", "chop_stop_key", "chop_stop_first_at")


@pytest.mark.parametrize("mode", (strategy_mode.MODE_N1, strategy_mode.MODE_P3, strategy_mode.MODE_E))
def test_state_file_written_before_chop_existed_still_loads(store, mode):
    """main-MACD2 619cd51 이 쓴 state(CHOP 키 없음)를 그대로 읽는다."""
    st = store.default_state()
    strategy_mode.apply(st, mode)
    st.mode = "mock"
    raw = state_store.serialize(st)
    for k in _CHOP_KEYS:
        raw.pop(k, None)
    store.ensure_paths()
    state_store.STATE_PATH.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    back = store.load_state()
    assert strategy_mode.current(back) == mode
    assert back.chop_defense_enabled is False
    assert back.chop_mc_flags == [] and back.chop_mc_on_at is None
    assert worker._tw2_3slot_daily_cap(back) == 3 or mode == strategy_mode.MODE_E


def test_state_file_from_the_pre_e_chop_branch_keeps_chop(store):
    """feature 브랜치(E 이전)가 쓴 CHOP state — e_* 키가 없어도 CHOP 유지."""
    st = store.default_state()
    strategy_mode.apply(st, strategy_mode.MODE_CHOP)
    st.mode = "mock"
    raw = state_store.serialize(st)
    for k in [k for k in raw if k.startswith("e_")]:
        raw.pop(k)
    store.ensure_paths()
    state_store.STATE_PATH.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    back = store.load_state()
    assert strategy_mode.current(back) == strategy_mode.MODE_CHOP
    assert back.e_enabled is False
