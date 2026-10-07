"""엔진 사본에 CHOP RESPONSE(gx) 훅을 넣는다. gx=None 이면 완전 무해해야 한다.

regime 은 **SHADOW-BASE** 로 사전계산된 봉단위 배열(gx["on"])을 그대로 읽는다 —
대응전략이 거래를 바꿔도 detector 입력이 변하지 않는다(feedback loop 차단).
"""
import pathlib

p = pathlib.Path("hengine5.py")
s = p.read_text(encoding="utf-8")
ok = []

# (1) Trade 필드
a = '    cx_part_done: bool = False\n'
b = a + '''    # -- GX: CHOP RESPONSE (연구 전용) --
    gx_chop: bool = False
    gx_ar1: bool = False
    gx_part_done: bool = False
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("1 fields")

# (2) 시그니처
a = "              cx: Optional[dict] = None,\n"
b = a + "              gx: Optional[dict] = None,\n"
assert s.count(a) == 1
s = s.replace(a, b); ok.append("2 sig")

# (3) 상태
a = "    _cx = dict(cx or {})\n"
b = '''    _gx = dict(gx or {})
    _gx_on = _gx.get("on")
    _gx_log = _gx.get("log") if isinstance(_gx.get("log"), list) else []
    _gx_day = {"d": None, "n_ord": 0, "first_loss": None, "streak": 0}

    def _gx_is_chop(i):
        return bool(_gx_on is not None and 0 <= i < len(_gx_on) and _gx_on[i])

''' + a
assert s.count(a) == 1
s = s.replace(a, b); ok.append("3 state")

# (4) 일자 초기화에 gx 카운터 추가
a = "                nonlocal_day[0] = 0.0\n"
b = a + '''                _gx_day["d"] = day_key
                _gx_day["n_ord"] = 0
                _gx_day["first_loss"] = None
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("4 day reset")

# (5) 진입 게이트 — 실제 체결 직전
a = """                    else:
                        if (_cx and position is not None and position.get("cx")):
"""
b = """                    elif (_gx and _gx_is_chop(idx) and position is None
                          and _gx_skip(idx, recognition_at, current_day, p_direction,
                                       slot_number, session)):
                        _gx_log.append({"kind": "GX_SKIP", "date": current_day,
                                        "at": recognition_at.isoformat(),
                                        "direction": p_direction.value,
                                        "slot": slot_number, "session": session,
                                        "rule": _gx.get("skip", {}).get("rule", "")})
                    else:
                        if (_cx and position is not None and position.get("cx")):
"""
assert s.count(a) == 1
s = s.replace(a, b); ok.append("5 entry gate")

# (6) 스킵 판정 함수 — regime helper 뒤에 둔다
a = "        return bool(_cx_state[\"on\"])\n"
b = a + '''
    def _gx_skip(i, rec_at, day, direction, slot_no, sess):
        """CHOP ON 에서 ordinary N1 진입을 막을지. AR1 후보는 절대 막지 않는다."""
        sk = _gx.get("skip")
        if not sk:
            return False
        isar1 = False
        fn = _gx.get("is_ar1")
        if fn is not None:
            try:
                isar1 = bool(fn(day, rec_at))
            except Exception:
                isar1 = False
        if isar1:
            return False
        rule = sk.get("rule", "")
        F = _gx.get("feat")
        pr = sk.get("params", {})
        if rule == "T1":
            t = rec_at.astimezone(KST).time()
            return bool(dtime(10, 0) <= t < dtime(11, 30))
        if rule == "D1":
            return bool((slot_no or 1) >= 2)
        if rule == "D2":
            return bool(_gx_day["first_loss"] is True)
        if rule == "D3":
            return bool(_gx_day["n_ord"] >= int(pr.get("max", 2)))
        if rule == "D4":
            return bool(_gx_day["streak"] >= int(pr.get("n", 2))
                        and not _gx_day.get("skipped_once"))
        if F is None:
            return False
        if rule in ("A1", "A2", "A3", "C1", "C2", "C3"):
            sc = F.get("score")
            if rule == "C3":
                return not bool(F["teg_ok"][i])
            if sc is None or i >= len(sc):
                return False
            th = float(pr.get("th", 0.0))
            return bool(sc[i] < th)
        return False
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("6 skip fn")

# (7) 진입 시 gx 표식 + 카운터
a = """                                rec.rx_on = bool(_rx_on)
"""
b = """                                if _gx:
                                    rec.gx_chop = bool(_gx_is_chop(idx))
                                    _fn = _gx.get("is_ar1")
                                    try:
                                        rec.gx_ar1 = bool(_fn(current_day, recognition_at)) if _fn else False
                                    except Exception:
                                        rec.gx_ar1 = False
                                    if not rec.gx_ar1:
                                        _gx_day["n_ord"] += 1
                                rec.rx_on = bool(_rx_on)
"""
assert s.count(a) == 1
s = s.replace(a, b); ok.append("7 entry mark")

# (8) 청산 시 당일 첫거래 손익/연속손실 갱신
a = """        if w1a_seq == 1 and str(reason or "") == config.EXIT_TW_STOP_LOSS:
            w1a_first_stop = True
"""
b = a + """        if _gx:
            if _gx_day["first_loss"] is None:
                _gx_day["first_loss"] = bool((rec.net_pct or 0.0) <= 0)
            if (rec.net_pct or 0.0) <= 0:
                _gx_day["streak"] += 1
            else:
                _gx_day["streak"] = 0
"""
assert s.count(a) == 1
s = s.replace(a, b); ok.append("8 close bookkeeping")

# (9) 축 B 청산 — CHOP 진입 거래에만. replace 는 래더 대체, overlay 는 래더 유지.
a = "            # 틱 익절\n            if position is not None and not position.get(\"cx\"):\n"
b = '''            # -- GX: CHOP 진입 거래 전용 청산 (축 B) --
            _gxe = _gx.get("exit") if _gx else None
            if (_gxe and position is not None and not position.get("cx")
                    and position["rec"].gx_chop):
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
                    _pk = position["peak"]
                    _q = _gxe.get("partial")
                    if _q and not position["rec"].gx_part_done and net >= float(_q["at"]):
                        _r = float(_q.get("ratio", 0.5))
                        position["realized"] += position["qty_frac"] * _r * net
                        position["legs"].append((ce._fmt(tick), price,
                                                 round(position["qty_frac"] * _r, 6),
                                                 "GX_PARTIAL", round(net, 4)))
                        position["qty_frac"] *= (1.0 - _r)
                        position["rec"].gx_part_done = True
                    _hm = (tick - position["entry_time"]).total_seconds() / 60.0
                    _why = None
                    if _gxe.get("tp") is not None and net >= float(_gxe["tp"]):
                        _why = "GX_TP"
                    elif _gxe.get("sl") is not None and net <= -float(_gxe["sl"]):
                        _why = "GX_SL"
                    elif (_gxe.get("be_after") is not None
                          and _pk >= float(_gxe["be_after"]) and net <= 0.0):
                        _why = "GX_BREAKEVEN"
                    elif _gxe.get("maxmin") is not None and _hm >= float(_gxe["maxmin"]):
                        _why = "GX_MAXHOLD"
                    if _why:
                        close_trade(tick, price, _why, idx)
                        position = None
                        whipsaw_watch = None
                        hold = _clr(hold)
                        rhold = None
                        break

            # 틱 익절
            if position is not None and not position.get("cx") and not (
                    _gx and _gx.get("exit", {}).get("mode") == "replace"
                    and position["rec"].gx_chop):
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("9 axis B exit")

p.write_text(s, encoding="utf-8")
print("적용:", " / ".join(ok))
