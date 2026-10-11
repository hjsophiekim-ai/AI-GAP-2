"""엔진에 MAX-HOLD 연장 훅을 넣는다. gx["ext"] 가 없으면 완전 무해해야 한다.

max-hold 시점에 즉시 닫지 않고 그 시점까지의 값으로 추세를 본다.
  mode="promote" : 조건 충족 시 B3 를 해제하고 기존 N1/C1 래더에 넘긴다(무기한).
  mode="extend"  : 조건 충족 시 deadline 을 add 분 연장(최대 max_ext 회), 매 회 재평가.
조건 미충족이면 기존대로 GX_MAXHOLD 청산.
"""
import pathlib

p = pathlib.Path("hengine5.py")
s = p.read_text(encoding="utf-8")
ok = []

# (1) Trade 필드
a = '    gx_prom_conds: str = ""\n'
b = a + '''    gx_ext_count: int = 0
    gx_ext_at: str = ""
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("1 fields")

# (2) 연장 판정 함수 — _gx_promote 뒤
a = '''        return bool(okp), score, ",".join(k for k, v in conds.items() if v)
'''
b = a + '''
    def _gx_hold_ext(pos, i, tick, net):
        """max-hold 시점 판정. 그 시점까지의 값만 쓴다(미래정보 없음)."""
        ex = _gx.get("ext")
        if not ex:
            return "close", None, ""
        F = ex.get("feat")
        if F is None or i <= 0 or i >= len(F["dgap"]):
            return "close", None, ""
        up = (pos["rec"].direction == "UP_RED")
        sg = 1.0 if up else -1.0
        c_net = bool(net > 0)
        c_gap = bool(sg * F["dgap"][i] > 0)
        c_spr = bool(sg * F["dspread"][i] > 0)
        c_vwp = bool(sg * F["vwapd"][i] > 0)
        c_opp = bool(F["no_opp_up"][i] if up else F["no_opp_dn"][i])
        q = quotes[pos["symbol"]]
        p0 = q.at(tick - timedelta(minutes=3))
        p1 = q.at(tick)
        c_etf = bool(p0 is not None and p1 is not None and p1 >= p0)
        conds = {"net": c_net, "gap": c_gap, "spread": c_spr, "etf": c_etf,
                 "vwap": c_vwp, "noopp": c_opp}
        score = int(sum((c_gap, c_spr, c_etf, c_vwp, c_opp)))
        rule = ex.get("rule", "")
        if rule == "NG":
            okx = c_net and c_gap
        elif rule == "NGE":
            okx = c_net and c_gap and c_etf
        elif rule == "S3":
            okx = c_net and score >= 3
        else:
            okx = False
        if not okx:
            return "close", score, ",".join(k for k, v in conds.items() if v)
        if ex.get("mode") == "promote":
            return "promote", score, ",".join(k for k, v in conds.items() if v)
        if int(pos["rec"].gx_ext_count) >= int(ex.get("max_ext", 1)):
            return "close", score, ",".join(k for k, v in conds.items() if v)
        return "extend", score, ",".join(k for k, v in conds.items() if v)
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("2 ext fn")

# (3) max-hold 분기 교체
a = '''                    elif _gxe.get("maxmin") is not None and _hm >= float(_gxe["maxmin"]):
                        _why = "GX_MAXHOLD"
'''
b = '''                    elif (_gxe.get("maxmin") is not None
                          and _hm >= float(_gxe["maxmin"])
                          + int(position["rec"].gx_ext_count)
                          * float((_gx.get("ext") or {}).get("add", 20))):
                        _act, _sc, _cs = _gx_hold_ext(position, idx, tick, net)
                        if _act == "promote":
                            position["rec"].gx_promoted = True
                            position["rec"].gx_prom_at = pd.Timestamp(tick).isoformat()
                            position["rec"].gx_prom_score = _sc
                            position["rec"].gx_prom_conds = _cs
                            _gx_log.append({"kind": "GX_HOLD_PROMOTE", "date": current_day,
                                            "at": pd.Timestamp(tick).isoformat(),
                                            "entry": position["rec"].entry_time,
                                            "net": round(float(net), 4), "conds": _cs})
                            break
                        if _act == "extend":
                            position["rec"].gx_ext_count += 1
                            position["rec"].gx_ext_at = pd.Timestamp(tick).isoformat()
                            _gx_log.append({"kind": "GX_HOLD_EXTEND", "date": current_day,
                                            "at": pd.Timestamp(tick).isoformat(),
                                            "entry": position["rec"].entry_time,
                                            "n": int(position["rec"].gx_ext_count),
                                            "net": round(float(net), 4), "conds": _cs})
                            continue
                        _why = "GX_MAXHOLD"
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("3 maxhold branch")

p.write_text(s, encoding="utf-8")
print("적용:", " / ".join(ok))
