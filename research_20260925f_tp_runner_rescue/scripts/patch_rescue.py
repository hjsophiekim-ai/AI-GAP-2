"""엔진에 TP-RUNNER-RESCUE 훅을 넣는다. gx["rescue"] 가 없으면 완전 무해해야 한다.

CHOP 거래가 +1.0% 에 **최초 도달한 틱**에서 **마지막 완성봉(idx-1)** 기준으로만 평가.
통과: ratio(50%) 를 +1.0% 에서 익절하고 잔량을 N1/C1 러너 관리로 승격.
실패: 기존 B3 대로 전량 익절.
SL −1.0% / max-hold 20분 / Y3 max-hold 승격은 전부 그대로 유지된다.
"""
import pathlib

p = pathlib.Path("hengine5.py")
s = p.read_text(encoding="utf-8")
ok = []

# (1) Trade 필드
a = '    gx_grace_end: str = ""\n'
b = a + '''    gx_rescue: bool = False
    gx_rescue_at: str = ""
    gx_rescue_conds: str = ""
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("1 fields")

# (2) rescue 판정 함수 — _gx_grace_ok 앞
a = '''    def _gx_grace_ok(pos, i, gr, tick=None):
'''
b = '''    def _gx_rescue_ok(pos, i, rs, tick):
        """+1% 최초도달 틱 판정. 봉 조건은 **마지막 완성봉(i-1)**, ETF 는 1분 호가."""
        F = rs.get("feat")
        j = i - 1
        if F is None or j < 1 or j >= len(F["dgap"]):
            return False, ""
        up = (pos["rec"].direction == "UP_RED")
        sg = 1.0 if up else -1.0
        c_gap = bool(sg * F["dgap"][j] > 0)
        c_spr = bool(sg * F["dspread"][j] > 0)
        _q = quotes[pos["symbol"]]
        _p0 = _q.at(tick - timedelta(minutes=3))
        _p1 = _q.at(tick)
        c_etf = bool(_p0 is not None and _p1 is not None and _p1 >= _p0)
        rule = rs.get("rule", "R1")
        if rule == "R1":
            okr = c_gap and c_etf
        elif rule == "R2":
            okr = c_gap and c_etf and c_spr
        elif rule == "R3":
            okr = (int(c_gap) + int(c_etf) + int(c_spr)) >= 2
        else:
            okr = False
        return bool(okr), ",".join(k for k, v in
                                   (("gap", c_gap), ("etf", c_etf), ("spread", c_spr)) if v)

''' + a
assert s.count(a) == 1
s = s.replace(a, b); ok.append("2 rescue fn")

# (3) TP 분기에 rescue 삽입 (기존 prom 분기 뒤, GX_TP 확정 앞)
a = '''                            break
                        _why = "GX_TP"
'''
b = '''                            break
                        _rs = _gx.get("rescue")
                        if _rs is not None and not position["rec"].gx_rescue:
                            _rok, _rc = _gx_rescue_ok(position, idx, _rs, tick)
                            if _rok:
                                _rt = float(_rs.get("ratio", 0.5))
                                position["realized"] += position["qty_frac"] * _rt * net
                                position["legs"].append(
                                    (ce._fmt(tick), price,
                                     round(position["qty_frac"] * _rt, 6),
                                     "GX_TP_PARTIAL", round(net, 4)))
                                position["qty_frac"] *= (1.0 - _rt)
                                position["rec"].gx_rescue = True
                                position["rec"].gx_rescue_at = pd.Timestamp(tick).isoformat()
                                position["rec"].gx_rescue_conds = _rc
                                position["rec"].gx_promoted = True
                                position["rec"].gx_prom_at = pd.Timestamp(tick).isoformat()
                                _gx_log.append({"kind": "GX_TP_RESCUE", "date": current_day,
                                                "at": pd.Timestamp(tick).isoformat(),
                                                "entry": position["rec"].entry_time,
                                                "net": round(float(net), 4), "conds": _rc})
                                break
                        _why = "GX_TP"
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("3 rescue branch")

p.write_text(s, encoding="utf-8")
print("적용:", " / ".join(ok))
