# -*- coding: utf-8 -*-
"""진입기준 연구 훅 — READ-ONLY. production 무수정.

추가하는 것 (전부 기본 off, 미지정이면 완전한 no-op):
  flag_log        : 모든 플래그의 판정+진입시점 특징을 기록 (미래값 없음)
  solo_idx        : 그 플래그 하나만 허용하고 강제진입 -> 독립 반사실 측정
  force           : 거절된 플래그를 승인으로 되돌리는 후보필터 훅
  force_ignore_slot : 슬롯한도까지 무시할지 (기본 False = 슬롯은 지킨다)
청산은 건드리지 않는다. N1 래더 + C1(pp) 그대로 쓴다.
"""
from pathlib import Path

p = Path("hengine5.py")
s = p.read_text(encoding="utf-8")

# -- 1) signature ---------------------------------------------------------
old = "              events: Optional[list] = None) -> list:\n"
new = ("              events: Optional[list] = None,\n"
       "              flag_log: Optional[list] = None,   # 모든 플래그 판정 기록\n"
       "              force: Optional[Callable] = None,  # 거절 -> 승인 후보필터\n"
       "              solo_idx: Optional[int] = None,     # 이 플래그만 허용+강제\n"
       "              force_ignore_slot: bool = False) -> list:\n")
assert s.count(old) == 1
s = s.replace(old, new)

# -- 2) Trade field -------------------------------------------------------
old = '    relax: str = ""          # 이 진입이 완화규칙으로 열렸는가 (C/D/TEG)\n'
new = old + '    forced: str = ""         # 연구용 강제/후보필터 진입 태그\n'
assert s.count(old) == 1
s = s.replace(old, new)

# -- 3) flag_log ----------------------------------------------------------
old = '''                                       "slot": slot_number, "session": session,
                                       "flat": position is None})
'''
new = old + '''
                    if flag_log is not None:
                        _tb = snap_table(bars); _fb = feat_table(bars)
                        _sg = 1.0 if p_direction == Direction.UP_RED else -1.0
                        _v = bars["volume"].astype(float).to_numpy()
                        _vb = _v[max(0, idx - 19): idx + 1]
                        _q = _memo("tq", p_idx,
                                   lambda: tw3.evaluate_trend_quality(bars_slice, p_direction))
                        _tg = _memo("teg", p_idx,
                                    lambda: teg_gate.evaluate_teg(bars_slice, p_direction,
                                                                  flag_bar_dt, recognition_at))
                        _rs2 = tr.snapshot(bars_slice, p_direction)
                        _cl = float(_tb["close"][idx]); _vw = float(_fb["vwap"][idx])
                        flag_log.append({
                            "date": current_day, "at": recognition_at.isoformat(),
                            "flag_at": pd.Timestamp(p_bar_ts).isoformat(),
                            "direction": p_direction.value, "flag_ord": flag_ord,
                            "idx": int(idx), "flag_idx": int(p_idx),
                            "approved": bool(final_approved), "reason": final_reason,
                            "base_approved": bool(base_decision.approved),
                            "base_reason": base_decision.block_reason,
                            "tw2_cleared": bool(tw2_cleared),
                            "slot": slot_number, "session": session,
                            "flat": position is None,
                            "held_symbol": None if position is None else position["symbol"],
                            "h50_handled": None, "regime_handled": None, "forced": "",
                            # -- entry-time features (completed bars only, no lookahead)
                            "tq_score": int(getattr(_q, "passed_count", 0)),
                            "tq_ok": bool(_q.approved),
                            "teg_ok": bool(_tg.approved),
                            "entry_chop": bool(entry_chop), "chop_score": int(chop_score),
                            "trend_ok": bool(_rs2.ok),
                            "gap": float(_fb["gap"][idx]) * _sg,
                            "gap_prev": (float(_fb["gap"][idx - 1]) * _sg if idx > 0 else None),
                            "gap_exp": (float(_fb["gap"][idx] - _fb["gap"][idx - 1]) * _sg
                                        if idx > 0 else None),
                            "gap_pct": (float(_fb["gap"][idx]) * _sg / _cl * 100.0
                                        if _cl > 0 else None),
                            "e20_e50_pct": ((float(_tb["e20"][idx] - _tb["e50"][idx]) * _sg
                                             / _cl * 100.0) if _cl > 0 else None),
                            "slope50": float(_tb["s50"][idx]) * _sg,
                            "slope20": float(_tb["s20"][idx]) * _sg,
                            "close_e20_pct": ((float(_cl - _tb["e20"][idx]) * _sg / _cl * 100.0)
                                              if _cl > 0 else None),
                            "vwap_pct": ((_cl - _vw) * _sg / _vw * 100.0
                                         if _vw > 0 else None),
                            "vol_ratio": (float(_v[idx] / _vb.mean())
                                          if len(_vb) and _vb.mean() > 0 else None),
                            "minute": recognition_at.astimezone(KST).strftime("%H:%M"),
                        })
'''
assert s.count(old) == 1
s = s.replace(old, new)

# -- 4) force / solo approval --------------------------------------------
old = '''                    if regime_handled or h50_handled:
                        pass
                    elif not final_approved:
'''
new = '''                    if flag_log is not None:
                        flag_log[-1]["h50_handled"] = bool(h50_handled)
                        flag_log[-1]["regime_handled"] = bool(regime_handled)
                    _forced = ""
                    if (not final_approved) and not (regime_handled or h50_handled):
                        _fr = None
                        if solo_idx is not None and int(p_idx) == int(solo_idx):
                            _fr = "SOLO"
                        elif force is not None:
                            _fr = force({
                                "date": current_day, "at": recognition_at,
                                "direction": p_direction, "flag_ord": flag_ord,
                                "idx": idx, "flag_idx": p_idx, "reason": final_reason,
                                "base_reason": base_decision.block_reason,
                                "tw2_cleared": tw2_cleared, "flat": position is None,
                                "bars_slice": bars_slice, "flag_bar_dt": flag_bar_dt,
                                "entry_chop": entry_chop, "chop_score": chop_score,
                                "feat": (flag_log[-1] if flag_log is not None else None)})
                        if _fr:
                            _sd2 = tw3.resolve_slot(
                                now=recognition_at, slots_used_today=slots_used_today,
                                morning_count=morning_count, afternoon_count=afternoon_count,
                                direction=p_direction, is_flat=(position is None),
                                last_afternoon_direction=last_afternoon_direction)
                            if _sd2.slot_allowed or force_ignore_slot:
                                if slot_number is None:
                                    slot_number, session = _sd2.slot_number, _sd2.session
                                if session is None:
                                    session = (tw3.SESSION_MORNING
                                               if recognition_at.astimezone(KST).time()
                                               < dtime(12, 0) else tw3.SESSION_AFTERNOON)
                                final_approved = True
                                final_reason = config.TW_APPROVED
                                _forced = str(_fr)
                                if flag_log is not None:
                                    flag_log[-1]["approved"] = True
                                    flag_log[-1]["forced"] = _forced
                                    flag_log[-1]["slot"] = slot_number
                                    flag_log[-1]["session"] = session

                    if regime_handled or h50_handled:
                        pass
                    elif not final_approved:
'''
assert s.count(old) == 1
s = s.replace(old, new)

# -- 5) solo flag gating --------------------------------------------------
old = '''            if idx in flags_by_idx:
                ft = bar_start.astimezone(KST).time()
                if config.SESSION_OPEN <= ft < config.NEW_ENTRY_CUTOFF:
                    pending = (flags_by_idx[idx], idx, bar_ts)
'''
new = '''            if idx in flags_by_idx and (solo_idx is None or int(idx) == int(solo_idx)):
                ft = bar_start.astimezone(KST).time()
                if config.SESSION_OPEN <= ft < config.NEW_ENTRY_CUTOFF:
                    pending = (flags_by_idx[idx], idx, bar_ts)
'''
assert s.count(old) == 1
s = s.replace(old, new)

# -- 6) tag the Trade -----------------------------------------------------
old = "                                            relax=_relax_hit)\n"
new = "                                            relax=_relax_hit, forced=_forced)\n"
assert s.count(old) == 1
s = s.replace(old, new)

p.write_text(s, encoding="utf-8")
print("ENT patch OK")
