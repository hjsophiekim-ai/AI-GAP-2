"""엔진에 RUNNER PROMOTION 훅을 넣는다. gx["prom"] 이 없으면 완전 무해해야 한다.

CHOP 포지션이 +1.0% 를 **처음 도달한 그 틱**에서 추세강도를 본다(그 시점까지의 정보만).
강하면 승격: B3 의 TP/SL/maxhold 를 해제하고 기존 N1/C1 래더에 넘긴다.
약하면 기존 B3 대로 +1.0% 전량익절.
"""
import pathlib

p = pathlib.Path("hengine5.py")
s = p.read_text(encoding="utf-8")
ok = []

# (1) Trade 필드
a = '    gx_part_done: bool = False\n'
b = a + '''    gx_promoted: bool = False
    gx_prom_at: Optional[str] = None
    gx_prom_score: Optional[int] = None
    gx_prom_conds: str = ""
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("1 fields")

# (2) 승격 판정 함수 — _gx_skip 뒤
a = "        if F is None:\n            return False\n"
b = """        if F is None:
            return False
"""
assert s.count(a) == 1  # 위치 확인용(치환 없음)

a = '''    def _gx_is_chop(i):
        return bool(_gx_on is not None and 0 <= i < len(_gx_on) and _gx_on[i])
'''
b = a + '''
    def _gx_promote(pos, i, tick):
        """+1% 도달 틱에서 승격 여부. 그 시점까지의 값만 쓴다(미래정보 없음)."""
        pr = _gx.get("prom")
        if not pr:
            return False, None, ""
        F = pr.get("feat")
        if F is None or i <= 0 or i >= len(F["dgap"]):
            return False, None, ""
        up = (pos["rec"].direction == "UP_RED")
        sg = 1.0 if up else -1.0
        c_gap = bool(sg * F["dgap"][i] > 0)
        c_spr = bool(sg * F["dspread"][i] > 0)
        c_vwp = bool(sg * F["vwapd"][i] > 0)
        c_opp = bool(F["no_opp_up"][i] if up else F["no_opp_dn"][i])
        # ETF 추종 — 보유 ETF 의 직전 3분 수익률이 음수가 아니면 추종으로 본다.
        q = quotes[pos["symbol"]]
        p0 = q.at(tick - timedelta(minutes=3))
        p1 = q.at(tick)
        c_etf = bool(p0 is not None and p1 is not None and p1 >= p0)
        conds = {"gap": c_gap, "spread": c_spr, "etf": c_etf, "vwap": c_vwp, "noopp": c_opp}
        score = int(sum(conds.values()))
        rule = pr.get("rule", "")
        if rule == "R1":
            okp = c_gap and c_etf
        elif rule == "R2":
            okp = c_gap and c_etf and c_spr
        elif rule == "R3":
            okp = c_gap and c_etf and c_spr and c_vwp
        elif rule == "R4":
            okp = c_gap and c_etf and c_spr and c_vwp and c_opp
        elif rule == "S3":
            okp = score >= 3
        elif rule == "S4":
            okp = score >= 4
        else:
            okp = False
        return bool(okp), score, ",".join(k for k, v in conds.items() if v)
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("2 promote fn")

# (3) gx 청산 블록 진입조건에 승격 제외
a = '''            if (_gxe and position is not None and not position.get("cx")
                    and position["rec"].gx_chop):
'''
b = '''            if (_gxe and position is not None and not position.get("cx")
                    and position["rec"].gx_chop and not position["rec"].gx_promoted):
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("3 gx guard")

# (4) TP 도달 시 승격 판정 — 승격이면 닫지 않고 래더로 넘긴다
a = '''                    _why = None
                    if _gxe.get("tp") is not None and net >= float(_gxe["tp"]):
                        _why = "GX_TP"
'''
b = '''                    _why = None
                    if _gxe.get("tp") is not None and net >= float(_gxe["tp"]):
                        _pk, _sc, _cs = _gx_promote(position, idx, tick)
                        if _pk:
                            position["rec"].gx_promoted = True
                            position["rec"].gx_prom_at = pd.Timestamp(tick).isoformat()
                            position["rec"].gx_prom_score = _sc
                            position["rec"].gx_prom_conds = _cs
                            _gx_log.append({"kind": "GX_PROMOTE", "date": current_day,
                                            "at": pd.Timestamp(tick).isoformat(),
                                            "direction": position["rec"].direction,
                                            "entry": position["rec"].entry_time,
                                            "net": round(float(net), 4),
                                            "score": _sc, "conds": _cs})
                            break
                        _why = "GX_TP"
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("4 promote at TP")

# (5) 래더 가드 — 승격된 포지션은 래더가 받는다
a = '''            if position is not None and not position.get("cx") and not (
                    _gx and _gx.get("exit", {}).get("mode") == "replace"
                    and position["rec"].gx_chop):
'''
b = '''            if position is not None and not position.get("cx") and not (
                    _gx and _gx.get("exit", {}).get("mode") == "replace"
                    and position["rec"].gx_chop and not position["rec"].gx_promoted):
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("5 ladder guard")

# (6) 승격 포지션은 whipsaw/h50/regime/pp 관리도 기존대로 받아야 한다 -> 이미
#     cx 가드만 걸려 있으므로 추가 변경 불필요(gx_chop 은 그 블록들을 막지 않는다).

p.write_text(s, encoding="utf-8")
print("적용:", " / ".join(ok))
