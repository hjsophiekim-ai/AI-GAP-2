"""2026-09-30 (5차) 연구 훅: 손절 후 동일방향 1회 재진입 (opt-in b3["reent"] = 수량비율).

READ-ONLY 연구. lab/hengine5.py 사본에만 적용.
- 대상 손절: GX_SL (B3 −1%), TIME_WINDOW_STOP_LOSS (N1 오전/오후 완성봉 손절)
- 손절 시각 이후 시작한 완성 3분봉이 끝난 뒤부터, 봉마다:
    flat ∧ 동일방향 N1 추세(close/EMA20 vs EMA50/slope) ∧ 14:55 이전 ∧ 하루 슬롯 < 3
  이면 재진입 (fill = 그 봉 인식시각 가격). 하루 1회 (재진입이 다시 손절돼도 추가 없음).
- 수량 = 원래 진입 W1a × 비율, 일일 노출 3.0 잔여로 cap.
- 재진입 포지션 관리 = 일반 진입과 동일 (P3 regime -> CHOP 이면 B3, N1/C1/H50/ETP 그대로).
- 일반 플래그 진입이 먼저 열리면 후보는 폐기.
"""
from pathlib import Path

P = Path(__file__).resolve().parent / "hengine5.py"
src = P.read_text(encoding="utf-8")
assert "연구 훅 EXO" in src
assert "연구 훅 REENT" not in src, "already patched"


def rep(old, new, count=1):
    global src
    n = src.count(old)
    assert n == count, (n, old[:90])
    src = src.replace(old, new)


rep("""    plock_net: Optional[float] = None
""", """    plock_net: Optional[float] = None
    reentry: bool = False             # 연구 훅 REENT: 손절 후 동일방향 재진입 거래
    reent_sl_at: Optional[str] = None
    reent_sl_net: Optional[float] = None
    reent_orig_entry: Optional[str] = None
""")

# 상태 (run_chain 로컬 mutable)
rep("""    exop = None                       # 연구 훅 EXO: (dir, p_idx, p_bar_ts, defer_idx, exited_rec)
""", """    exop = None                       # 연구 훅 EXO: (dir, p_idx, p_bar_ts, defer_idx, exited_rec)
    _re = {"cand": None, "used": False}  # 연구 훅 REENT
""")
rep("""                pending = None
                r2p = None
                exop = None
""", """                pending = None
                r2p = None
                exop = None
                _re["cand"] = None
                _re["used"] = False
""")

# close_trade: 손절 후보 등록
rep("""        if w1a_seq == 1 and str(reason or "") == config.EXIT_TW_STOP_LOSS:
            w1a_first_stop = True
""", """        if w1a_seq == 1 and str(reason or "") == config.EXIT_TW_STOP_LOSS:
            w1a_first_stop = True
        # 연구 훅 REENT: 손절이면 동일방향 재진입 후보 등록 (하루 1회)
        if (b3 is not None and b3.get("reent") and not _re["used"]
                and str(reason or "") in ("GX_SL", config.EXIT_TW_STOP_LOSS)):
            _re["cand"] = {"dir": rec.direction, "sl_at": pd.Timestamp(exit_time), "w1a": float(rec.w1a),
                           "net": float(rec.net_pct), "entry": rec.entry_time}
""")

# 일반 진입이 열리면 후보 폐기
rep("""                                rec.exo_entry = bool(exo_force)
""", """                                rec.exo_entry = bool(exo_force)
                                _re["cand"] = None
""")

# 봉마다 재진입 판정 (whipsaw-watch 추적 직전 = pending/플래그 처리 뒤)
rep("""            # ── whipsaw-watch 추적 (production _advance_whipsaw_watch) ──────""", """            # ── 연구 훅 REENT: 손절 후 동일방향 1회 재진입 ──
            _rc = _re["cand"]
            if (_rc is not None and position is None and not _re["used"]
                    and pd.Timestamp(bar_start) >= _rc["sl_at"]
                    and recognition_at.astimezone(KST).time() < config.NEW_ENTRY_CUTOFF):
                _rdir = Direction(_rc["dir"])
                if slots_used_today >= int(config.TW2_3SLOT_DAILY_CAP):
                    _re["cand"] = None
                    if events is not None:
                        events.append({"date": current_day, "kind": "REENT_SLOT_FULL", "at": recognition_at.isoformat()})
                elif tr.snapshot(bars.iloc[: idx + 1], _rdir).ok:
                    _rsym = order_executor.target_symbol_for_direction(_rdir)
                    _rfill = fill_at(_rsym, recognition_at)
                    _rmult = min(_rc["w1a"] * float(b3["reent"]),
                                 max(0.0, float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP) - w1a_exposure))
                    if _rfill is not None and _rmult > 0:
                        _re["used"] = True
                        _re["cand"] = None
                        slots_used_today += 1
                        _rses = (tw3.SESSION_MORNING
                                 if recognition_at.astimezone(KST).time() < config.TW2_3SLOT_MORNING_WINDOW_END
                                 else tw3.SESSION_AFTERNOON)
                        if _rses == tw3.SESSION_MORNING:
                            morning_count += 1
                        else:
                            afternoon_count += 1
                            last_afternoon_direction = _rdir.value
                        w1a_exposure += _rmult
                        w1a_seq += 1
                        day_entry_seq += 1
                        _rbs = bars.iloc[: idx + 1]
                        _rcd = etp.evaluate_entry_chop(_rbs, _rdir, recognition_at)
                        rec = Trade(date=current_day, slot_number=slots_used_today, session=_rses,
                                    direction=_rdir.value, decision_idx=idx, flag_ordinal=0,
                                    entry_time=recognition_at.isoformat(), entry_symbol=_rsym,
                                    entry_price=_rfill, entry_bar_idx=idx + 1,
                                    entry_chop=bool(_rcd.is_chop) and not _rcd.insufficient_data,
                                    chop_score=0 if _rcd.insufficient_data else int(_rcd.score),
                                    tq=None, w1a=_rmult, relax="REENT")
                        rec.reentry = True
                        rec.reent_sl_at = _rc["sl_at"].isoformat()
                        rec.reent_sl_net = _rc["net"]
                        rec.reent_orig_entry = _rc["entry"]
                        _rs2 = tr.snapshot(_rbs, _rdir)
                        rec.trend_at_entry = bool(_rs2.ok)
                        rec.regime_eligible = True
                        rec.day_seq = int(day_entry_seq)
                        position = {"symbol": _rsym, "entry_idx": idx + 1, "mode": "",
                                    "entry_time": recognition_at, "tp1_done": False,
                                    "peak": 0.0, "session": _rses, "rec": rec,
                                    "qty_frac": 1.0, "realized": 0.0,
                                    "runner_on": False, "tp2_part": False,
                                    "legs": [], "ms": set(),
                                    "ax": {"mode": None, "tp2": None, "floor": None, "brk": 0,
                                           "brk_idx": None, "pend": None, "flip": None},
                                    "regime_eligible": True}
                        _rg = b3["regime"](recognition_at)
                        rec.entry_regime = _rg
                        if _rg == "CHOP" and b3.get("on", True):
                            rec.b3_on = True
                            position["b3"] = {"promoted": False, "trig": False}
                        whipsaw_watch = None
                        hold = _clr(hold)
                        rhold = None
            # ── whipsaw-watch 추적 (production _advance_whipsaw_watch) ──────""")

P.write_text(src, encoding="utf-8")
print("patched OK")
