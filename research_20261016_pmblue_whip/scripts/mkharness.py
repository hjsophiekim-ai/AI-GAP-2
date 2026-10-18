"""연구2/3 하네스 생성 (2026-10-16). production 무수정 — 기존 재생 하네스에
monkeypatch 만 더한다. 두 변형 전부 env 로 꺼지며, 꺼진 상태는 BASE 와 동일해야 한다.

입력 : bb/test_zz_tr2.py (2026-10-11 CHOP-STACK 연구에서 쓴 하네스, 그대로)
출력 : wt/tests/macd2/test_zz_v2.py

env:
  PMV     = RELAX | OFF   연구2. 13:30~14:30 DOWN_BLUE 에만 적용.
                          RELAX : TW2 VWAP veto 1.0%->2.0%, TEG COND_VWAP 여유 0%->1.0%
                          OFF   : 둘 다 비적용
  WHIP_ON = hhmm          연구3. 그 시각부터 11:00 까지
  WHIP_K  = 1 | 2         그날 오전 진입이 이미 K 번이면 신규진입 차단(슬롯 미소비)
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
SP = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad"
SRC = SP + "/bb/test_zz_tr2.py"
DST = SP + "/wt/tests/macd2/test_zz_v2.py"

s = open(SRC, encoding="utf-8").read()

# ── 연구2 : PM-BLUE VWAP 관문 변형 (RSBRK memo 보다 먼저 감싸야 한다) ─────────
A = '''    if RSBRK:
        from app.trading.macd2 import teg_gate as _teg, time_window_3slot as _t3s'''
B = '''    # ── 연구2 (2026-10-16) : PM-BLUE(13:30~14:30 DOWN_BLUE) VWAP 관문 변형 ──
    #   현행 production 은 이 창의 BLUE 에 VWAP 관문을 두 겹으로 건다:
    #     (1) evaluate_tw2_extra_vetoes 의 VWAP 역행 veto (불리 1.0% 초과면 차단)
    #     (2) 오후 필수 AND 게이트인 teg_gate 의 COND_VWAP (불리면 무조건 탈락)
    #   RELAX 는 둘 다 1.0%p 씩 느슨하게, OFF 는 둘 다 비적용.
    #   창/방향 밖에서는 원함수를 그대로 부른다 — 다른 시간대는 한 줄도 안 바뀐다.
    PMV = os.environ.get("PMV") or None
    if PMV:
        from datetime import time as _dtime
        from app.trading.macd2 import teg_gate as _tegv
        from app.trading.macd2.major_flag_filter import _prepare_bars as _prep_v, _session_vwap as _svw
        PM_LO, PM_HI, PM_EASE = _dtime(13, 30), _dtime(14, 30), 1.0

        def _in_pm(direction, decision_at):
            try:
                d = direction if isinstance(direction, Direction) else Direction(str(direction))
            except ValueError:
                return False
            return d == Direction.DOWN_BLUE and PM_LO <= pd.Timestamp(decision_at).time() < PM_HI

        _o_veto = time_window_filter.evaluate_tw2_extra_vetoes

        def _veto_v(bars_3m, flag_direction, flag_bar_dt, decision_at, **kw):
            if not _in_pm(flag_direction, decision_at):
                return _o_veto(bars_3m, flag_direction, flag_bar_dt, decision_at, **kw)
            old = config.TW2_VWAP_VETO_THRESHOLD_PCT
            config.TW2_VWAP_VETO_THRESHOLD_PCT = (old - PM_EASE) if PMV == "RELAX" else -1e9
            try:
                return _o_veto(bars_3m, flag_direction, flag_bar_dt, decision_at, **kw)
            finally:
                config.TW2_VWAP_VETO_THRESHOLD_PCT = old
        monkeypatch.setattr(time_window_filter, "evaluate_tw2_extra_vetoes", _veto_v)

        _o_teg = _tegv.evaluate_teg

        def _teg_v(bars_3m, flag_direction, flag_bar_dt, decision_at, **kw):
            dec = _o_teg(bars_3m, flag_direction, flag_bar_dt, decision_at, **kw)
            if dec.approved or not _in_pm(flag_direction, decision_at):
                return dec
            conds = dict(dec.conditions or {})
            if _tegv.COND_VWAP not in conds or conds[_tegv.COND_VWAP]:
                return dec                       # VWAP 말고 다른 이유로 떨어진 것은 그대로 둔다
            ok = (PMV == "OFF")
            if not ok:
                w = _prep_v(bars_3m)
                vw = _svw(w) if w is not None else None
                if w is not None and vw is not None and len(vw):
                    c, v = float(w["close"].iloc[-1]), float(vw.iloc[-1])
                    ok = bool(v > 0 and -(c / v - 1.0) * 100.0 >= -PM_EASE)
            if not ok:
                return dec
            conds[_tegv.COND_VWAP] = True
            LOG.append(dict(ev="PMV_PASS", t=pd.Timestamp(decision_at).isoformat(), mode=PMV,
                            armed_at=pd.Timestamp(decision_at).isoformat()))
            return dataclasses.replace(
                dec, approved=all(conds.get(c, False) for c in _tegv.ALL_CONDITIONS),
                conditions=conds,
                reject_reasons=tuple(r for r in dec.reject_reasons if r != _tegv.COND_VWAP))
        monkeypatch.setattr(_tegv, "evaluate_teg", _teg_v)

''' + A
assert s.count(A) == 1
s = s.replace(A, B, 1)

# ── 연구3 : 오전 휩쏘일 2·3번째 진입 차단 (슬롯 미소비) ───────────────────────
A2 = '''    NENT = {"n": 0, "d": None}'''
B2 = A2 + '''
    # ── 연구3 (2026-10-16) : MORNING-WHIP SLOT SAVE ─────────────────────
    #   WHIP_ON(hhmm) 부터 11:00 까지, 그날 오전 진입이 이미 WHIP_K 번이면
    #   신규진입을 막는다. 슬롯 판정 **앞**이라 슬롯을 쓰지 않는다 -> 오후에 남는다.
    #   휩쏘일/ON시각은 오프라인 사전계산(s3_whip.py, 입력은 A/B 동일).
    WHIP_ON = os.environ.get("WHIP_ON") or None
    WHIP_K = int(os.environ.get("WHIP_K", "1"))
    MENT = {"n": 0, "d": None}

    def _ment(now):
        dt = pd.Timestamp(now).date()
        if MENT["d"] != dt:
            MENT["d"] = dt
            MENT["n"] = 0
        return MENT["n"]'''
assert s.count(A2) == 1
s = s.replace(A2, B2, 1)

A3 = '''            if _out is not None and _out.final_state == worker.SignalState.EXECUTED:
                NENT["n"] += 1'''
B3 = A3 + '''
                if now.strftime("%H%M") < "1100":
                    _ment(now)
                    MENT["n"] += 1'''
assert s.count(A3) == 1
s = s.replace(A3, B3, 1)

A4 = '''            if not _cap_tick(now) and FIRE["on"] is None:'''
B4 = '''            if (WHIP_ON is not None and FIRE["on"] is None
                    and WHIP_ON <= now.strftime("%H%M") < "1100" and _ment(now) >= WHIP_K):
                LOG.append(dict(ev="WHIP_BLOCK", t=now.isoformat(), sid=sid, dir=direction.value,
                                n=MENT["n"], armed_at=now.isoformat()))
                state.processed_signal_ids = list(state.processed_signal_ids) + [sid]
                return None
''' + A4
assert s.count(A4) == 1
s = s.replace(A4, B4, 1)

open(DST, "w", encoding="utf-8").write(s)
print("wrote", DST, len(s), "chars")
print("PMV block:", s.count("PMV_PASS"), "| WHIP block:", s.count("WHIP_BLOCK"))
