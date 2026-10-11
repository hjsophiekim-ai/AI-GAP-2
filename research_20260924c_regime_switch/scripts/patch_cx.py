"""엔진 사본에 CHOP MODE(cx) 훅을 넣는다. cx=None 이면 완전 무해해야 한다."""
import pathlib

p = pathlib.Path("hengine5.py")
s = p.read_text(encoding="utf-8")
ok = []

# (1) Trade 필드
a = '    rx_reason: str = ""\n'
b = a + '''    # -- CX: CHOP MODE (연구 전용) --
    cx_strategy: str = ""
    cx_on: bool = False
    cx_part_done: bool = False
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("1 fields")

# (2) 시그니처
a = "              rx: Optional[dict] = None,\n"
b = a + "              cx: Optional[dict] = None,\n"
assert s.count(a) == 1
s = s.replace(a, b); ok.append("2 sig")

# (3) 상태
a = "    _rx = dict(rx or {})\n"
b = '''    _cx = dict(cx or {})
    _cx_log = _cx.get("log") if isinstance(_cx.get("log"), list) else []
    _cx_state = {"on": False}
    import cxlib as _CX

''' + a
assert s.count(a) == 1
s = s.replace(a, b); ok.append("3 state")

# (4) cx regime (히스테리시스 선택)
a = "        return bool(on), round(h, 4), round(q, 4)\n"
b = a + '''
    def _cx_regime():
        """CHOP regime 판정 - 이미 청산된 거래만(지연계산). 히스테리시스 선택."""
        if not _cx:
            return False
        r = _cx.get("regime") or {"K": 10, "h50": 0.40, "tp1": 0.20}
        K = int(r.get("K", 10))
        prior = trades[-K:]
        if len(prior) < K:
            return False
        h = sum(1.0 for t in prior if getattr(t, "h50_held", False)) / len(prior)
        q = sum(1.0 for t in prior if getattr(t, "tp1_hit", False)) / len(prior)
        if not _cx.get("hyst"):
            return bool(h >= float(r.get("h50", 0.40)) and q <= float(r.get("tp1", 0.20)))
        if _cx_state["on"]:
            if h < float(r.get("off_h50", 0.30)) or q > float(r.get("off_tp1", 0.30)):
                _cx_state["on"] = False
        else:
            if h >= float(r.get("h50", 0.40)) and q <= float(r.get("tp1", 0.20)):
                _cx_state["on"] = True
        return bool(_cx_state["on"])
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("4 cx regime")

# (5) CHOP 진입 훅 - pending 처리 앞
a = "            if pending is not None:\n                p_direction, p_idx, p_bar_ts = pending\n"
b = '''            # -- CX: CHOP MODE 진입 (연구 전용) --------------------------
            # 슬롯/일예산/세션/동일방향 규칙은 tw3.resolve_slot 으로 그대로 탄다.
            # M0: 같은 봉에 N1 진입이 예정돼 있으면 양보 / M1: N1 신규진입 차단
            # 상태에서 CHOP 만 / M2: CHOP 우선.
            if _cx and _CXF is not None and position is None:
                _n1_due = (pending is not None and idx == pending[1] + 1)
                _mode = _cx.get("interact", "M0")
                _may = (_mode == "M2") or (_mode == "M1") or (not _n1_due)
                if _may and _cx_regime():
                    _t = recognition_at.astimezone(KST).time()
                    if config.SESSION_OPEN <= _t < config.NEW_ENTRY_CUTOFF:
                        _d = _CX.evaluate(_cx["strategy"], _CXF, idx, _cx.get("params", {}))
                        if _d is not None:
                            _sd = tw3.resolve_slot(
                                now=recognition_at, slots_used_today=slots_used_today,
                                morning_count=morning_count, afternoon_count=afternoon_count,
                                direction=_d, is_flat=True,
                                last_afternoon_direction=last_afternoon_direction)
                            if _sd.slot_allowed:
                                _tg = order_executor.target_symbol_for_direction(_d)
                                _fl = fill_at(_tg, recognition_at)
                                if _fl is not None:
                                    slots_used_today += 1
                                    if _sd.session == tw3.SESSION_MORNING:
                                        morning_count += 1
                                    else:
                                        afternoon_count += 1
                                        last_afternoon_direction = _d.value
                                    _m = _w1a_multiplier(entry_chop=True,
                                                         first_stop=w1a_first_stop,
                                                         exposure=w1a_exposure, extra=1.0)
                                    w1a_exposure += _m
                                    w1a_seq += 1
                                    _rec = Trade(date=current_day, slot_number=_sd.slot_number,
                                                 session=_sd.session, direction=_d.value,
                                                 decision_idx=idx, flag_ordinal=0,
                                                 entry_time=recognition_at.isoformat(),
                                                 entry_symbol=_tg, entry_price=_fl,
                                                 entry_bar_idx=idx + 1, entry_chop=True,
                                                 chop_score=0, tq=None, w1a=_m)
                                    _rec.cx_strategy = str(_cx["strategy"])
                                    _rec.cx_on = True
                                    position = {"symbol": _tg, "entry_idx": idx + 1, "mode": "",
                                                "entry_time": recognition_at, "tp1_done": False,
                                                "peak": 0.0, "session": _sd.session, "rec": _rec,
                                                "qty_frac": 1.0, "realized": 0.0,
                                                "runner_on": False, "tp2_part": False,
                                                "legs": [], "ms": set(),
                                                "ax": {"mode": None, "tp2": None, "floor": None,
                                                       "brk": 0, "brk_idx": None, "pend": None,
                                                       "flip": None},
                                                "regime_eligible": True, "cx": True}
                                    whipsaw_watch = None
                                    hold = _clr(hold)
                                    rhold = None
                                    _cx_log.append({"kind": "CX_ENTRY", "date": current_day,
                                                    "at": recognition_at.isoformat(),
                                                    "strategy": _cx["strategy"],
                                                    "direction": _d.value,
                                                    "slot": _sd.slot_number,
                                                    "slots_used": slots_used_today,
                                                    "n1_due_same_bar": bool(_n1_due)})

''' + a
assert s.count(a) == 1
s = s.replace(a, b); ok.append("5 cx entry")

# (6) M1 차단 + 밀어냄 로깅
a = "                    else:\n                        fill = fill_at(target, recognition_at)\n"
b = '''                    elif (_cx and _cx.get("interact") == "M1" and _cx_regime()
                          and position is None):
                        _cx_log.append({"kind": "N1_SUPPRESSED_M1", "date": current_day,
                                        "at": recognition_at.isoformat(),
                                        "direction": p_direction.value})
                    else:
                        if (_cx and position is not None and position.get("cx")):
                            _cx_log.append({"kind": "N1_BLOCKED_BY_CX", "date": current_day,
                                            "at": recognition_at.isoformat(),
                                            "direction": p_direction.value,
                                            "cx_symbol": position["symbol"],
                                            "cx_strategy": position["rec"].cx_strategy})
                        fill = fill_at(target, recognition_at)
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("6 M1 + push log")

# (7) CHOP 전용 청산 + 래더 가드
VW = ('                        if (position["rec"].direction == "UP_RED" and _vd >= 0) or '
      '(position["rec"].direction == "DOWN_BLUE" and _vd <= 0):\n')
a = "            # 틱 익절\n            if position is not None:\n"
b = '''            # -- CX: CHOP 포지션 전용 청산 (자체 TP/SL/maxhold). N1 래더 미사용.
            if _cx and position is not None and position.get("cx"):
                for mo in range(3):
                    tick = bar_start + timedelta(minutes=mo)
                    if tick <= position["entry_time"] or tick > recognition_at:
                        continue
                    price = quotes[position["symbol"]].exact.get(pd.Timestamp(tick))
                    if price is None:
                        continue
                    net = net_at(price)
                    if net > position["rec"].peak_net_pct:
                        position["rec"].peak_at = pd.Timestamp(tick).isoformat()
                    position["rec"].peak_net_pct = max(position["rec"].peak_net_pct, net)
                    position["rec"].mae_net_pct = min(position["rec"].mae_net_pct, net)
                    position["peak"] = max(position["peak"], net)
                    _q = _cx.get("partial")
                    if _q and not position["rec"].cx_part_done and net >= float(_q["at"]):
                        _r = float(_q.get("ratio", 0.5))
                        position["realized"] += position["qty_frac"] * _r * net
                        position["legs"].append((ce._fmt(tick), price,
                                                 round(position["qty_frac"] * _r, 6),
                                                 "CX_PARTIAL", round(net, 4)))
                        position["qty_frac"] *= (1.0 - _r)
                        position["rec"].cx_part_done = True
                    _hm = (tick - position["entry_time"]).total_seconds() / 60.0
                    _why = None
                    if net >= float(_cx["tp"]):
                        _why = "CX_TP"
                    elif net <= -float(_cx["sl"]):
                        _why = "CX_SL"
                    elif _hm >= float(_cx["maxmin"]):
                        _why = "CX_MAXHOLD"
                    elif _cx.get("vwap_exit") and _CXF is not None:
                        _vd = _CXF["vwap_dist"][idx]
''' + VW + '''                            _why = "CX_VWAP"
                    if _why:
                        close_trade(tick, price, _why, idx)
                        position = None
                        whipsaw_watch = None
                        hold = _clr(hold)
                        rhold = None
                        break

            # 틱 익절
            if position is not None and not position.get("cx"):
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("7 cx exit + guard")

# (8) 관리 블록 가드
for old, new, tag in [
    ('            if whipsaw_watch is not None and position is not None:\n',
     '            if whipsaw_watch is not None and position is not None and not position.get("cx"):\n',
     "8a whipsaw"),
    ('            if h50_on(position) and hold is not None and position is not None:\n',
     '            if h50_on(position) and hold is not None and position is not None and not position.get("cx"):\n',
     "8b h50"),
    ('            if regime_release and rhold is not None and position is not None:\n',
     '            if regime_release and rhold is not None and position is not None and not position.get("cx"):\n',
     "8c regime"),
]:
    assert s.count(old) == 1, tag
    s = s.replace(old, new); ok.append(tag)

# (9) pp(C1) 가드
a = '                        if (ax is not None and ax.get("pp") and position is not None):\n'
b = ('                        if (ax is not None and ax.get("pp") and position is not None\n'
     '                                and not position.get("cx")):\n')
assert s.count(a) == 1
s = s.replace(a, b); ok.append("9 pp guard")

# (10) 특징표 주입점
a = "    _dcfg_fixed = dict(D_VARIANTS[d_variant]) if d_variant else {}\n"
b = '    _CXF = _cx.get("feat") if _cx else None\n' + a
assert s.count(a) == 1
s = s.replace(a, b); ok.append("10 feat")

p.write_text(s, encoding="utf-8")
print("적용:", " / ".join(ok))
