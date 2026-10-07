"""엔진에 GRACE-WINDOW 훅을 넣는다. gx["grace"] 가 없으면 완전 무해해야 한다.

인과성: 틱은 bar_start+0/1/2분에 돌고 봉 idx 는 bar_start+3분에야 완성된다.
따라서 조건은 **idx-1(마지막 완성봉)** 로 본다 — 실시간에 실제로 알 수 있는 값이다.
(엔진 자체 래더는 관례상 idx 를 쓰지만 그건 공표 앵커에 묶여 있어 건드리지 않는다.)

grace(min=0) 이면 "유예 없는 인과봉 snapshot" 이 되어 X1c 가 된다.
"""
import pathlib

p = pathlib.Path("hengine5.py")
s = p.read_text(encoding="utf-8")
ok = []

# (1) Trade 필드
a = '    gx_ext_at: str = ""\n'
b = a + '''    gx_grace: bool = False
    gx_grace_at: str = ""
    gx_grace_end: str = ""
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("1 fields")

# (2) grace 조건 함수
a = '''    def _gx_hold_ext(pos, i, tick, net):
'''
b = '''    def _gx_grace_ok(pos, i, gr):
        """마지막 **완성봉**(idx-1) 기준. consec=2 면 직전 2개 완성봉 모두 충족."""
        F = gr.get("feat")
        if F is None:
            return False
        up = (pos["rec"].direction == "UP_RED")
        sg = 1.0 if up else -1.0
        need = gr.get("need", ["gap"])
        consec = int(gr.get("consec", 1))
        j = i - 1
        for k in range(consec):
            b_ = j - k
            if b_ < 1 or b_ >= len(F["dgap"]):
                return False
            if "gap" in need and not (sg * F["dgap"][b_] > 0):
                return False
            if "spread" in need and not (sg * F["dspread"][b_] > 0):
                return False
        return True

''' + a
assert s.count(a) == 1
s = s.replace(a, b); ok.append("2 grace fn")

# (3) max-hold 분기에 grace 삽입 (ext 분기보다 앞)
a = '''                        _act, _sc, _cs = _gx_hold_ext(position, idx, tick, net)
'''
b = '''                        _gr = _gx.get("grace")
                        if _gr is not None:
                            if position.get("gx_grace_until") is None:
                                if net <= 0:
                                    _why = "GX_MAXHOLD"
                                else:
                                    position["gx_grace_until"] = (
                                        tick + timedelta(minutes=int(_gr.get("min", 6))))
                                    position["rec"].gx_grace = True
                                    position["rec"].gx_grace_at = pd.Timestamp(tick).isoformat()
                                    position["rec"].gx_grace_end = pd.Timestamp(
                                        position["gx_grace_until"]).isoformat()
                                    _gx_log.append({"kind": "GX_GRACE_IN", "date": current_day,
                                                    "at": pd.Timestamp(tick).isoformat(),
                                                    "entry": position["rec"].entry_time,
                                                    "net": round(float(net), 4)})
                            if _why is None and position.get("gx_grace_until") is not None:
                                if net <= 0:
                                    _why = "GX_GRACE_FAIL"
                                elif _gx_grace_ok(position, idx, _gr):
                                    position["rec"].gx_promoted = True
                                    position["rec"].gx_prom_at = pd.Timestamp(tick).isoformat()
                                    _gx_log.append({"kind": "GX_GRACE_PROMOTE",
                                                    "date": current_day,
                                                    "at": pd.Timestamp(tick).isoformat(),
                                                    "entry": position["rec"].entry_time,
                                                    "net": round(float(net), 4)})
                                    break
                                elif tick >= position["gx_grace_until"]:
                                    _why = "GX_MAXHOLD"
                            if _why is None:
                                continue
                            _act, _sc, _cs = ("close", None, "")
                        else:
                            _act, _sc, _cs = _gx_hold_ext(position, idx, tick, net)
'''
assert s.count(a) == 1
s = s.replace(a, b); ok.append("3 grace branch")

p.write_text(s, encoding="utf-8")
print("적용:", " / ".join(ok))
