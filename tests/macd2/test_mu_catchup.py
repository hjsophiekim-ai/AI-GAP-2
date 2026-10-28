"""MU CATCH-UP (2026-10-28) — MU 선행·하이닉스 지연 RED 의 좁은 실거래 예외. 고정하는 계약:

  판정(mu_catchup.evaluate)
    · PASS = RED ∧ 플래그 09:00~11:00 ∧ MU 세션 ≥ +0.5% ∧ MU−HY ≥ +1.0%p (체결강도는 보조 기록값, 판정 무관)
    · MU/HY stale·세션 기준봉 결측 = UNAVAILABLE / BLUE 는 값만 계산하고 FAIL(BLUE_SHADOW_ONLY)
    · 판정 시각 이후 분봉은 쓰지 않는다
    · MU Assist 에 내장: MU Assist OFF 또는 MACD2_MU_CATCHUP_KILL 이면 판정하지 않는다(XS 토글과 무관)
  worker (_resolve_tw2_3slot_candidate_body)
    · PASS 일 때만 T+3 확인 2종 / CHOP_MORNING_NO_TRADE / MU 단기 ENTRY_BAD 를 풀고 진입
    · VWAP veto · 슬롯 한도 · 시간창(재판정) 은 PASS 여도 그대로 막는다
    · 신호원장에 mu_catchup_pass·세션값·체결강도·푼 게이트를 남긴다
  10/28 09:39 RED 실데이터(MU·하이닉스 1분봉 + 실제 체결강도 85.2) -> PASS, cttr_match=False 로 기록
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.trading.macd2 import config, exec_strength, ledger, mu_assist, mu_catchup, strategy_mode, worker
from app.trading.macd2.models import Direction, MajorFlagDecision
from app.trading.macd2.worker import run_once

KST = config.KST
DAY = datetime(2026, 10, 28)
F = datetime(2026, 10, 28, 9, 39, tzinfo=KST)
NOW = datetime(2026, 10, 28, 9, 45, 5, tzinfo=KST)
MATCH = {"cttr": 120.0, "state": exec_strength.MATCH}
MISMATCH = {"cttr": 95.0, "state": exec_strength.MISMATCH}
NOCTTR = {"cttr": None, "state": exec_strength.UNAVAILABLE}


def _mu(base=100.0, now_val=101.0, start="08:00", end="09:44", spike_after=None):
    idx = pd.date_range(f"2026-10-28 {start}", f"2026-10-28 {end}", freq="1min")
    vals = [base + (now_val - base) * i / (len(idx) - 1) for i in range(len(idx))]
    s = pd.Series(vals, index=idx)
    if spike_after:
        extra = pd.date_range(spike_after, periods=10, freq="1min")
        s = pd.concat([s, pd.Series([base * 0.5] * 10, index=extra)])
    return s


def _hy(base=1_000_000.0, now_val=998_000.0, start="08:00", end="09:44"):
    idx = pd.date_range(f"2026-10-28 {start}", f"2026-10-28 {end}", freq="1min", tz=KST)
    vals = [base + (now_val - base) * i / (len(idx) - 1) for i in range(len(idx))]
    return pd.DataFrame({"datetime": idx, "close": vals})


def _ev(direction=Direction.UP_RED, flag=F, now=NOW, mu=None, hy=None, cttr=MATCH):
    return mu_catchup.evaluate(direction, flag, now, hy if hy is not None else _hy(),
                               mu_series=mu if mu is not None else _mu(), cttr=cttr)


# ── 판정 ────────────────────────────────────────────────────────────────────

def test_pass_when_mu_leads_and_hynix_lags():
    ev = _ev()
    assert ev["status"] == mu_catchup.PASS, ev
    assert ev["mu_session_pct"] == pytest.approx(1.0, abs=1e-3)
    assert ev["hy_session_pct"] == pytest.approx(-0.2, abs=1e-3)
    assert ev["mu_hy_gap"] == pytest.approx(1.2, abs=1e-3) and ev["cttr_match"] is True


@pytest.mark.parametrize("kw, reason", [
    (dict(mu=_mu(now_val=100.4)), "MU_SESSION_LT_0.5"),                 # MU +0.4%
    (dict(hy=_hy(now_val=1_002_000.0)), "GAP_LT_1.0"),                  # 괴리 0.8%p
    (dict(flag=datetime(2026, 10, 28, 11, 0, tzinfo=KST), now=datetime(2026, 10, 28, 11, 6, 5, tzinfo=KST),
          mu=_mu(end="11:05"), hy=_hy(end="11:05")), "OUTSIDE_0900_1100"),
])
def test_fail_reasons(kw, reason):
    ev = _ev(**kw)
    assert ev["status"] == mu_catchup.FAIL and reason in ev["reason"], ev


def test_blue_is_shadow_only_even_when_values_qualify():
    ev = _ev(direction=Direction.DOWN_BLUE, mu=_mu(now_val=99.0), hy=_hy(now_val=1_002_000.0),
             cttr={"cttr": 90.0, "state": exec_strength.MATCH})
    assert ev["status"] == mu_catchup.FAIL and ev["reason"] == "BLUE_SHADOW_ONLY"
    assert ev["mu_session_pct"] == pytest.approx(1.0, abs=1e-3) and ev["mu_hy_gap"] == pytest.approx(1.2, abs=1e-3)


@pytest.mark.parametrize("kw, reason", [
    (dict(mu=_mu(end="09:30")), "MU_STALE"),
    (dict(mu=_mu(start="08:20")), "MU_SESSION_INCOMPLETE"),
    (dict(hy=_hy(end="09:30")), "HY_STALE"),
    (dict(hy=_hy(start="08:20")), "HY_SESSION_INCOMPLETE"),
])
def test_unavailable_falls_back(kw, reason):
    ev = _ev(**kw)
    assert ev["status"] == mu_catchup.UNAVAILABLE and ev["reason"] == reason, ev


@pytest.mark.parametrize("cttr, match", [(MISMATCH, False), (NOCTTR, None), (MATCH, True)])
def test_cttr_is_auxiliary_only(cttr, match):
    """체결강도는 판정에 쓰지 않는다 — 불일치·판정불가여도 PASS, 값은 그대로 기록."""
    ev = _ev(cttr=cttr)
    assert ev["status"] == mu_catchup.PASS and ev["cttr_match"] is match, ev


def test_cttr_lookup_error_never_blocks(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("snapshot broken")
    monkeypatch.setattr(exec_strength, "evaluate", boom)
    ev = mu_catchup.evaluate(Direction.UP_RED, F, NOW, _hy(), mu_series=_mu())
    assert ev["status"] == mu_catchup.PASS and ev["cttr_match"] is None


def test_no_future_bars_used():
    """판정 시각 뒤에 MU 가 폭락하는 봉이 파일에 있어도(재생) 쓰지 않는다."""
    ev = _ev(mu=_mu(spike_after="2026-10-28 09:45"))
    assert ev["status"] == mu_catchup.PASS and ev["mu_session_pct"] == pytest.approx(1.0, abs=1e-3)


def test_is_active_follows_mu_assist_not_xs_and_kill(monkeypatch):
    from app.trading.macd2 import state_store
    s = state_store.default_state()
    s.mu_assist_enabled, s.mu_assist_xs_enabled = True, False
    assert mu_catchup.is_active(s) is True, "MU Assist 내장 — XS OFF 여도 동작"
    s.mu_assist_enabled = False
    assert mu_catchup.is_active(s) is False
    s.mu_assist_enabled = True
    monkeypatch.setattr(config, "MU_CATCHUP_KILL", True)
    assert mu_catchup.is_active(s) is False


def test_anchor_20261028_0939_real_data_passes():
    """10/28 09:39 RED 실데이터: MU(KIS BAQ/NAS 1분봉) +1.19% / 하이닉스(NX) −0.58% → PASS.
    실제 체결강도(R2 스냅샷, 09:45 직전 60초) 85.2 = RED 불일치지만 보조값이라 PASS 그대로."""
    here = Path(__file__).with_name("data") / "mu_catchup_20261028.csv"
    raw = pd.read_csv(here, parse_dates=["minute"])
    mu = raw[raw.kind == "MU"].set_index("minute").close
    hy = raw[raw.kind == "HY"].rename(columns={"minute": "datetime"})[["datetime", "close"]]
    hy["datetime"] = hy["datetime"].dt.tz_localize(KST)
    real = {"cttr": 85.2, "state": exec_strength.MISMATCH}
    ev = mu_catchup.evaluate(Direction.UP_RED, F, NOW, hy, mu_series=mu, cttr=real)
    assert ev["status"] == mu_catchup.PASS and ev["cttr_match"] is False, ev
    assert ev["mu_session_pct"] == pytest.approx(1.19, abs=0.05)
    assert ev["hy_session_pct"] == pytest.approx(-0.58, abs=0.05)
    assert ev["mu_hy_gap"] >= 1.0


# ── worker: 예외는 세 자리만 ────────────────────────────────────────────────

def _gap_reject():
    return MajorFlagDecision(approved=False, score=0.0, required_score=0.0,
                             decision=config.TW_REJECT_MACD_GAP_NOT_EXPANDING, reasons=("gap",),
                             component_scores={}, metrics={"window": "W2_MORNING_SELECTIVE"},
                             is_reversal=False, fast_reversal=False,
                             block_reason=config.TW_REJECT_MACD_GAP_NOT_EXPANDING)


def _reject(reason):
    return MajorFlagDecision(approved=False, score=0.0, required_score=0.0, decision=reason, reasons=(reason,),
                             component_scores={}, metrics={}, is_reversal=False, fast_reversal=False,
                             block_reason=reason)


def _world(monkeypatch, tmp_path, *, cu_status="PASS", retry=None, veto=(False, None), mc=True, mu_bad=True):
    from tests.macd2.test_p3_retry_postentry import _setup
    from tests.macd2.test_tw2_3slot_worker_regression import _PRIOR_DAY, _approved, _prime_3slot_pending
    svc, now0, state, broker = _setup(monkeypatch, tmp_path)
    strategy_mode.apply(state, strategy_mode.MODE_CHOP)
    state.mu_assist_enabled, state.mu_assist_xs_enabled = True, True
    calls = {"skip": 0}

    def fake_entry(*a, skip_confirmation_check=False, **kw):
        if skip_confirmation_check:
            calls["skip"] += 1
            return retry if retry is not None else _approved()
        return _gap_reject()

    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", fake_entry)
    monkeypatch.setattr(worker.time_window_filter, "evaluate_tw2_extra_vetoes", lambda *a, **kw: veto)
    monkeypatch.setattr(worker.chop_defense, "morning_no_trade", lambda *a, **kw: mc)
    monkeypatch.setattr(worker.mu_assist, "entry_veto_reason",
                        lambda sid: config.MU_ASSIST_ENTRY_BAD_BLOCK_REASON if mu_bad else None)
    ev = {"status": cu_status, "reason": "" if cu_status == "PASS" else "GAP_LT_1.0", "direction": "UP_RED",
          "flag_bar_at": "", "evaluated_at": "", "mu_session_pct": 1.19, "hy_session_pct": -0.58,
          "mu_hy_gap": 1.77, "cttr": 120.0, "cttr_match": True}
    monkeypatch.setattr(worker.mu_catchup, "evaluate", lambda *a, **kw: dict(ev))
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    return svc, now0, state, broker, calls


def _row(state):
    sid = state.tw2_3slot_post_entry_signal_id or ""
    rows = ledger._load_rows(ledger.SIGNAL_LEDGER_PATH, limit=0)
    return [r for r in rows if r.get("signal_id", "").endswith(":TW2_3SLOT_CONFIRM")][-1]


def _buys(broker):
    return [o for o in broker.orders if o.side == "BUY" and o.success]


_GATES = (f"{mu_catchup.GATE_CONFIRMATION}:{config.TW_REJECT_MACD_GAP_NOT_EXPANDING}"
          f"|{mu_catchup.GATE_MORNING_NO_TRADE}|{mu_catchup.GATE_MU_ENTRY_BAD}")


def test_pass_overrides_exactly_three_gates_then_takes_production_e_wait(monkeypatch, tmp_path):
    """CHOP 은 E 를 상속한다 — 살아난 후보는 기존 production 경로 그대로 E 돌파대기로 간다."""
    svc, now0, state, broker, calls = _world(monkeypatch, tmp_path)
    r = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert calls["skip"] == 1
    assert r.actions == ["E_PENDING_ARMED:UP_RED"], r.actions
    row = _row(state)
    assert row["mu_catchup_pass"] == "PASS" and row["catchup_overridden_gates"] == _GATES
    assert row["mu_session_pct"] == "1.19" and row["mu_hy_gap"] == "1.77" and row["cttr_match"] == "True"
    assert mu_catchup.last()["status"] == "PASS"


def test_pass_with_e_immediate_executes_once_and_counts_slot(monkeypatch, tmp_path):
    from app.trading.macd2 import e_strategy
    svc, now0, state, broker, calls = _world(monkeypatch, tmp_path)
    imm = e_strategy.EarlyPassDecision(evaluated=True, immediate=True, trigger=None, dist_pct=None,
                                       c1=True, c2=True, c3=True, up=True, upfast=False, reason="TEST_IMMEDIATE")
    monkeypatch.setattr(worker.e_strategy, "evaluate_early_pass", lambda **kw: imm)
    r = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert len(_buys(broker)) == 1 and r.actions == [f"TW2_3SLOT_ENTRY:{config.LONG_SYMBOL}"], r.actions
    row = _row(state)
    assert row["order_result"] == "EXECUTED" and row["mu_catchup_pass"] == "PASS"
    assert row["catchup_overridden_gates"] == _GATES
    assert state.tw2_3slot_slots_used_today == 1


def test_fail_keeps_production_rejection(monkeypatch, tmp_path):
    svc, now0, state, broker, calls = _world(monkeypatch, tmp_path, cu_status="FAIL")
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert _buys(broker) == [] and calls["skip"] == 0
    row = _row(state)
    assert row["block_reason"] == config.TW_REJECT_MACD_GAP_NOT_EXPANDING and row["mu_catchup_pass"] == "FAIL"
    assert row["catchup_overridden_gates"] == ""


def test_kill_switch_restores_production(monkeypatch, tmp_path):
    svc, now0, state, broker, calls = _world(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "MU_CATCHUP_KILL", True)
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert _buys(broker) == [] and calls["skip"] == 0
    row = _row(state)
    assert row["block_reason"] == config.TW_REJECT_MACD_GAP_NOT_EXPANDING and row.get("mu_catchup_pass", "") == ""


def test_mu_assist_off_means_no_catchup(monkeypatch, tmp_path):
    svc, now0, state, broker, calls = _world(monkeypatch, tmp_path)
    state.mu_assist_enabled = False
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert _buys(broker) == [] and calls["skip"] == 0


def test_xs_off_still_runs_catchup_inside_mu_assist(monkeypatch, tmp_path):
    svc, now0, state, broker, calls = _world(monkeypatch, tmp_path)
    state.mu_assist_xs_enabled = False
    r = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert calls["skip"] == 1 and r.actions == ["E_PENDING_ARMED:UP_RED"], r.actions
    assert _row(state)["mu_catchup_pass"] == "PASS"


def test_vwap_veto_is_never_overridden(monkeypatch, tmp_path):
    svc, now0, state, broker, _ = _world(monkeypatch, tmp_path, veto=(True, config.TW2_REJECT_VWAP_VETO))
    state.mu_assist_xs_enabled = True
    monkeypatch.setattr(worker, "_mu_xs_strong", lambda *a, **kw: None)     # XS STRONG 아님(= XS 도 안 푼다)
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert _buys(broker) == []
    assert _row(state)["block_reason"] == config.TW2_REJECT_VWAP_VETO


def test_time_window_on_rejudge_is_never_overridden(monkeypatch, tmp_path):
    svc, now0, state, broker, calls = _world(monkeypatch, tmp_path, retry=_reject(config.TW_REJECT_TIME_WINDOW))
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert _buys(broker) == [] and calls["skip"] == 1
    assert _row(state)["block_reason"] == config.TW_REJECT_TIME_WINDOW


def test_daily_slot_cap_is_never_overridden(monkeypatch, tmp_path):
    svc, now0, state, broker, _ = _world(monkeypatch, tmp_path)
    state.tw2_3slot_slots_used_today = worker._tw2_3slot_daily_cap(state)
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert _buys(broker) == []
    assert "CAP" in _row(state)["block_reason"]


# ── UI ─────────────────────────────────────────────────────────────────────

def test_ui_shows_catchup_status_line(monkeypatch):
    from tests.macd2.test_mu_assist_xs import _ui
    today = datetime.now(KST).strftime("%Y%m%d")
    ev = {"status": "PASS", "reason": "", "direction": "UP_RED", "mu_session_pct": 1.19, "hy_session_pct": -0.58,
          "mu_hy_gap": 1.77, "cttr": 120.0, "cttr_match": True}
    mu_catchup.remember_last(f"{today}_093900_UP_RED:TW2_3SLOT_CONFIRM", ev, ["CHOP_MORNING_NO_TRADE"])
    at = _ui(True, True, monkeypatch)
    caps = [str(c.value) for c in at.caption]
    assert any(c.startswith("CATCHUP: PASS") and "푼 게이트 CHOP_MORNING_NO_TRADE" in c for c in caps), caps
    monkeypatch.setattr(config, "MU_CATCHUP_KILL", True)
    at = _ui(True, True, monkeypatch)
    assert any(str(c.value).startswith("CATCHUP: OFF") for c in at.caption)
