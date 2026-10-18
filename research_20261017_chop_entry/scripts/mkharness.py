"""CHOP 진입품질 연구 하네스 (2026-10-17). production 무수정 — monkeypatch 만 더한다.

입력 : bb/test_zz_tr2.py (2026-10-11 CHOP-STACK 연구 하네스, 그대로)
출력 : wt/tests/macd2/test_zz_v3.py

env (전부 없으면 BASE 와 바이트 동일해야 한다 = OFF parity):
  CHOPMODE=1   기준선을 CHOP 모드로. strategy_mode.MODE_P3 -> MODE_CHOP 한 줄만 바꾼다.
               (= E 진입 + P3 청산스택 + 왕복장 방어 3종: 2슬롯/MC-DELAY/B3 구조손절)
  FEATLOG=1    진입 '후보' 가 TEG 까지 간 모든 순간의 진입시점 feature 를 원장에 남긴다
               (판단에 쓰지 않는다 — 기록만. 스크리닝용)
  EMAV=<mode>  TEG 의 price_ema_stack_aligned 조건만 바꾼다 (연구 우선순위 1)
               OFF   : 이 조건을 항상 통과로 본다
               SOFT  : 'close 가 EMA10 을 신호방향으로 넘었다' 까지만 요구 (EMA10 vs EMA20 불문)
  CQ=<mode>    CHOP 라벨 진입 품질 필터 (연구 우선순위 2·3에서 고른 후보만)
               각 모드는 코드 안에 사전 고정돼 있고 임계값 탐색을 하지 않는다.
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
SP = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad"
SRC = SP + "/bb/test_zz_tr2.py"
DST = SP + "/wt/tests/macd2/test_zz_v3.py"

s = open(SRC, encoding="utf-8").read()

# ── 1) CHOP 모드 기준선 ──────────────────────────────────────────────────────
A = '''    strategy_mode.apply(state, strategy_mode.MODE_P3 if VAR in ("A", "E", "F", "L30", "L45", "ALT3", "UPFASTRS") else strategy_mode.MODE_N1)'''
B = '''    _MODE = strategy_mode.MODE_P3 if VAR in ("A", "E", "F", "L30", "L45", "ALT3", "UPFASTRS") else strategy_mode.MODE_N1
    # 연구(2026-10-17): 기준선을 CHOP 모드로 올린다. 모드 상수 한 개만 바꾸고
    # 나머지는 production strategy_mode.apply 가 알아서 정규화한다.
    if os.environ.get("CHOPMODE") and _MODE == strategy_mode.MODE_P3:
        _MODE = strategy_mode.MODE_CHOP
    strategy_mode.apply(state, _MODE)'''
assert s.count(A) == 1
s = s.replace(A, B, 1)

# ── 2) TEG EMA 정렬 조건 변형 + 진입시점 feature 기록 ────────────────────────
A2 = '''    if RSBRK:
        from app.trading.macd2 import teg_gate as _teg, time_window_3slot as _t3s'''
B2 = '''    # ── 연구(2026-10-17) : TEG price_ema_stack_aligned 변형 + feature 기록 ──
    #   EMAV=OFF  : 이 조건을 항상 통과로 본다 (다른 6조건은 그대로)
    #   EMAV=SOFT : 'close 가 EMA10 을 신호방향으로 넘었다' 까지만 요구
    #               (EMA10 vs EMA20 의 상대위치는 묻지 않는다)
    #   FEATLOG=1 : TEG 가 평가된 모든 순간의 조건·지표를 원장 brk 에 남긴다(기록만).
    EMAV = os.environ.get("EMAV") or None
    FEATLOG = bool(os.environ.get("FEATLOG"))
    if EMAV or FEATLOG:
        from app.trading.macd2 import teg_gate as _tegE
        from app.trading.macd2.major_flag_filter import _prepare_bars as _prepE, _session_vwap as _svwE
        _o_tegE = _tegE.evaluate_teg

        def _teg_e(bars_3m, flag_direction, flag_bar_dt, decision_at, **kw):
            dec = _o_tegE(bars_3m, flag_direction, flag_bar_dt, decision_at, **kw)
            up = str(getattr(flag_direction, "value", flag_direction)) == "UP_RED"
            if FEATLOG:
                try:
                    w = _prepE(bars_3m)
                    c = w["close"].astype(float)
                    e10 = c.ewm(span=10, adjust=False).mean()
                    e20 = c.ewm(span=20, adjust=False).mean()
                    vw = _svwE(w)
                    LOG.append(dict(ev="TEGFEAT", t=pd.Timestamp(decision_at).isoformat(),
                                    armed_at=pd.Timestamp(decision_at).isoformat(),
                                    up=int(up), approved=int(dec.approved),
                                    conds={k: int(v) for k, v in (dec.conditions or {}).items()},
                                    close=float(c.iloc[-1]), ema10=float(e10.iloc[-1]),
                                    ema20=float(e20.iloc[-1]), vwap=float(vw.iloc[-1]) if len(vw) else None))
                except Exception:
                    pass
            if not EMAV or dec.approved:
                return dec
            conds = dict(dec.conditions or {})
            if _tegE.COND_EMA_STACK not in conds or conds[_tegE.COND_EMA_STACK]:
                return dec
            ok = (EMAV == "OFF")
            if not ok and EMAV == "SOFT":
                w = _prepE(bars_3m)
                if w is not None and len(w) > 20:
                    c = w["close"].astype(float)
                    e10 = float(c.ewm(span=10, adjust=False).mean().iloc[-1])
                    ok = (float(c.iloc[-1]) > e10) if up else (float(c.iloc[-1]) < e10)
            if not ok:
                return dec
            conds[_tegE.COND_EMA_STACK] = True
            return dataclasses.replace(
                dec, approved=all(conds.get(k, False) for k in _tegE.ALL_CONDITIONS),
                conditions=conds,
                reject_reasons=tuple(r for r in dec.reject_reasons if r != _tegE.COND_EMA_STACK))
        monkeypatch.setattr(_tegE, "evaluate_teg", _teg_e)

''' + A2
assert s.count(A2) == 1
s = s.replace(A2, B2, 1)

# ── 3) PE60 보조필터 (연구 B) ───────────────────────────────────────────────
#   PE60MAX 가 있으면, 결정시각의 '최근 60분 완성 1분봉 path efficiency' 가
#   그 값 미만이면 신규진입을 막는다. 슬롯 판정 앞이라 슬롯을 쓰지 않는다.
#   식은 production `chop_defense._path_is_chop` 의 PE 부분과 **같다**
#   (완성봉 < CHOP_MC_MIN_BARS(30) 이면 판정불가 -> 막지 않는다 = fail-open).
A3 = '''            if not _cap_tick(now) and FIRE["on"] is None:'''
B3 = '''            if PE60MAX is not None and FIRE["on"] is None:
                _d60 = _win(now).tail(60)
                _d60 = _d60[_d60["datetime"].dt.strftime("%H%M") >= "0900"]
                if len(_d60) >= 30:
                    _c60 = _d60["close"].to_numpy(float)
                    _mv = float(np.abs(np.diff(_c60)).sum())
                    _pe = abs(float(_c60[-1] - _c60[0])) / _mv if _mv > 0 else None
                    if _pe is not None and _pe < PE60MAX:
                        LOG.append(dict(ev="PE60_BLOCK", t=now.isoformat(), sid=sid,
                                        dir=direction.value, pe=round(_pe, 4),
                                        armed_at=now.isoformat()))
                        state.processed_signal_ids = list(state.processed_signal_ids) + [sid]
                        return None
''' + A3
assert s.count(A3) == 1
s = s.replace(A3, B3, 1)

A4 = '''    NENT = {"n": 0, "d": None}'''
B4 = A4 + '''
    PE60MAX = float(os.environ["PE60MAX"]) if os.environ.get("PE60MAX") else None'''
assert s.count(A4) == 1
s = s.replace(A4, B4, 1)

# ── 4) 청산 오버레이 OVTP / OVSL (연구 2026-10-18) ──────────────────────────
#   기존 익절/손절 래더(틱TP·봉래더·TW손절·C1·조기익절·B3 TP/SL)를 끄고
#   **고정 TP/SL 한 쌍**으로 대체한다. max-hold / 연장 / 승격 / 반대신호 /
#   장마감 청산은 그대로 둔다(= 산술 오버레이와 같은 의미).
A5 = '''    SLOW_MIN = float(os.environ.get("WK_SLOW_MIN", "25"))'''
B5 = A5 + '''
    OVTP = float(os.environ["OVTP"]) if os.environ.get("OVTP") else None
    OVSL = float(os.environ["OVSL"]) if os.environ.get("OVSL") else None'''
assert s.count(A5) == 1
s = s.replace(A5, B5, 1)

A6 = '''    # ── 진입 순간 trend snapshot 기록 (모든 변형, 기록만) + E = P3-TREND-BYPASS ──'''
B6 = '''    if OVTP is not None or OVSL is not None:
        from app.trading.macd2 import p3_stack as _p3ov

        def _ov_tp_off(**kw):
            return tpm.PositionManagementDecision(
                exit_reason=None, sell_fraction=0.0, tp1_done=bool(kw.get("tp1_done")),
                peak_net_return=max(0.0, float(kw.get("net_return_pct") or 0.0)),
                label="OV_TICK_TP_OFF")
        monkeypatch.setattr(tpm, "evaluate_take_profit_immediate", _ov_tp_off)

        _o_pos_ov = tpm.evaluate_position

        def _pos_ov(**kw):
            net = float(kw.get("net_return_pct") or 0.0)
            pk = max(float(kw.get("peak_net_return") or 0.0), net)
            if OVSL is not None and net <= OVSL:
                return tpm.PositionManagementDecision(
                    exit_reason=config.EXIT_TW_STOP_LOSS, sell_fraction=1.0,
                    tp1_done=bool(kw.get("tp1_done")), peak_net_return=pk, label="OV_SL")
            if OVTP is not None and net >= OVTP:
                return tpm.PositionManagementDecision(
                    exit_reason=config.EXIT_TW_TP2_FULL, sell_fraction=1.0,
                    tp1_done=True, peak_net_return=pk, label="OV_TP")
            d = _o_pos_ov(**kw)
            if d.exit_reason is None:
                return d
            return dataclasses.replace(d, exit_reason=None, sell_fraction=0.0,
                                       label="OV_LADDER_OFF:" + str(d.exit_reason))
        monkeypatch.setattr(tpm, "evaluate_position", _pos_ov)
        monkeypatch.setattr(worker.risk_exit, "check_stop_loss", lambda *a, **k: False)
        monkeypatch.setattr(early_take_profit, "is_active", lambda state: False)
        monkeypatch.setattr(worker, "_advance_c1_peak_protection", lambda **kw: None)

        _o_p3_ov = _p3ov.evaluate

        def _p3_ov(**kw):
            net = float(kw.get("net_return_pct") or 0.0)
            if OVSL is not None and net <= OVSL:
                d0 = _o_p3_ov(**kw)
                return dataclasses.replace(d0, action=_p3ov.ACTION_EXIT,
                                           exit_reason=config.EXIT_B3_SL,
                                           sell_fraction=1.0, reason="OV_SL")
            if OVTP is not None and net >= OVTP:
                d0 = _o_p3_ov(**kw)
                return dataclasses.replace(d0, action=_p3ov.ACTION_EXIT,
                                           exit_reason=config.EXIT_B3_TP,
                                           sell_fraction=1.0, reason="OV_TP")
            d = _o_p3_ov(**kw)
            if d.exit_reason in (config.EXIT_B3_TP, config.EXIT_B3_SL):
                return dataclasses.replace(d, action=_p3ov.ACTION_HOLD, exit_reason=None,
                                           sell_fraction=0.0, reason="OV_HOLD")
            return d
        monkeypatch.setattr(_p3ov, "evaluate", _p3_ov)

''' + A6
assert s.count(A6) == 1
s = s.replace(A6, B6, 1)

# ── 5) 오전 게이트 면제 재검토 MGATE (연구 2026-10-18) ─────────────────────
#   현행 : 09:00~11:00 의 1·2번째 후보는 TEG·품질게이트 없이 통과(3번째만 Trend
#          Quality, 11:00 이후는 TEG 필수) — 이것이 '오전 시간가산점' 의 정체다.
#   HALF : 오전 1·2번째에도 **Trend Quality** 게이트를 요구한다 (오후 TEG 보다 약함)
#   OFF  : 오전 1·2번째에도 **TEG** 를 요구한다 (오후와 완전히 동일)
#   resolve_slot 이 돌려주는 requires_* 플래그만 바꾼다. 슬롯 배분·캡은 그대로.
A7 = '''    if RSBRK:
        from app.trading.macd2 import teg_gate as _teg, time_window_3slot as _t3s'''
B7 = '''    MGATE = os.environ.get("MGATE") or None
    SOFTQ = {"on": False}
    if MGATE:
        from app.trading.macd2 import time_window_3slot as _t3g
        _o_rs = _t3g.resolve_slot

        def _rs_mg(**kw):
            d = _o_rs(**kw)
            if (d.slot_allowed and d.session == _t3g.SESSION_MORNING
                    and not d.requires_quality_gate and not d.requires_teg_gate):
                if MGATE in ("HALF", "SOFT"):
                    d = dataclasses.replace(d, requires_quality_gate=True)
                    SOFTQ["on"] = (MGATE == "SOFT")
                elif MGATE == "OFF":
                    d = dataclasses.replace(d, requires_teg_gate=True)
            else:
                SOFTQ["on"] = False
            return d
        monkeypatch.setattr(_t3g, "resolve_slot", _rs_mg)

        # MORNING-SOFT : 오전 1·2번째에는 Trend Quality 5조건 전부가 아니라
        #   **macd_gap_signed_expanding 하나만** 요구한다 (사전 고정, 탐색 없음).
        #   근거: 확정플래그 667건 전수에서 품질게이트 5조건에 대응하는 feature 중
        #   MACD gap 계열이 AUC 가 가장 높았고(0.540), 5조건 중 유일하게
        #   "T+3 확인 뒤에도 신호가 강해지는가" 를 보는 조건이며, 오후 TEG 의
        #   조건 3 과 같은 것이라 오전/오후 기준이 일관된다.
        #   3번째 슬롯(원래 품질게이트 대상)은 건드리지 않는다.
        if MGATE == "SOFT":
            _o_tq = _t3g.evaluate_trend_quality

            def _tq_soft(bars_3m, flag_direction, **kw):
                dec = _o_tq(bars_3m, flag_direction, **kw)
                if not SOFTQ.get("on"):
                    return dec
                c = dict(dec.conditions or {})
                if not c:
                    return dec
                only = bool(c.get(_t3g.QUALITY_COND_MACD_GAP, False))
                return dataclasses.replace(dec, approved=only,
                                           reject_reasons=() if only else (_t3g.QUALITY_COND_MACD_GAP,))
            monkeypatch.setattr(_t3g, "evaluate_trend_quality", _tq_soft)

''' + A7
assert s.count(A7) == 1
s = s.replace(A7, B7, 1)

# ── 6) CHOP 라벨 진입 사이즈 계수 CSZ (연구 2026-10-18) ────────────────────
#   production 의 X2LITE_SIZING_CHOP_MULT(0.80) 는 **CHOP 모드에서 돌지 않는다**
#   (position_sizing.is_active 가 W1A 계열에서만 True). 그래서 CHOP 모드의 현행
#   사이즈는 1.00 이다. 여기서는 주문 배수에 직접 계수를 곱해 '축소'를 구현한다.
#   라벨 판정은 production 과 같은 chop_regime.current_regime 을 쓴다.
A8 = '''            _out = _orig_eow(budget_multiplier=bm, **kw)'''
B8 = '''            if CSZ is not None and (is3 or sid.endswith(":BRK")):
                try:
                    _rgz = chop_regime.current_regime(as_of=now.isoformat()).regime
                except Exception:
                    _rgz = None
                if _rgz == "CHOP":
                    bm = bm * CSZ
                    LOG.append(dict(ev="CSZ", t=now.isoformat(), sid=sid, mult=CSZ,
                                    bm=round(bm, 4), armed_at=now.isoformat()))
            _out = _orig_eow(budget_multiplier=bm, **kw)'''
assert s.count(A8) == 1
s = s.replace(A8, B8, 1)

A9 = '''    PE60MAX = float(os.environ["PE60MAX"]) if os.environ.get("PE60MAX") else None'''
B9 = A9 + '''
    CSZ = float(os.environ["CSZ"]) if os.environ.get("CSZ") else None'''
assert s.count(A9) == 1
s = s.replace(A9, B9, 1)

open(DST, "w", encoding="utf-8").write(s)
print("wrote", DST)
print("CHOPMODE:", s.count("MODE_CHOP"), "| EMAV:", s.count("COND_EMA_STACK"), "| FEATLOG:", s.count("TEGFEAT"), "| PE60:", s.count("PE60_BLOCK"), "| OV:", s.count("OV_LADDER_OFF"))
