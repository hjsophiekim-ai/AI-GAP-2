"""MU 선행 태그 v2 — 'HY 진입까지 MU 가 방향전환 하지 않았을 것' 조건 추가.

tag_hold = MU 선행 플래그 시각 이후 HY 플래그 시각까지, **반대 방향 MU 플래그가 없다**.
(MU 플래그는 repeat suppression 이 걸려 있어 같은 방향이 연속으로 뜨지 않는다 —
 즉 '그 사이 반대 플래그 0개' 가 곧 '방향 유지' 다.)
"""
import csv, sys
from datetime import datetime
sys.stdout.reconfigure(encoding="utf-8")
MU=[]
for r in csv.DictReader(open("out_mulead.csv",encoding="utf-8-sig")):
    g=lambda k: None if r[k] in ("",None) else float(r[k])
    MU.append(dict(day=r["day"],t=r["f_end"],dir=r["dir"],gap=g("gap"),
                   expand=(r["expand"]=="True")))
def dt(day,hhmm): return datetime.strptime(f"{day} {hhmm}","%Y-%m-%d %H:%M")
def tag(day, flag_hhmm, direction):
    ft=dt(day,flag_hhmm); same=None
    for m in MU:
        if m["day"]!=day or m["dir"]!=direction: continue
        mt=dt(m["day"],m["t"])
        if mt>ft: continue
        d=int((ft-mt).total_seconds()//60)
        if same is None or d<same[0]: same=(d,m,mt)
    if same is None: return None
    lead, m, mt = same
    opp = "DOWN_BLUE" if direction=="UP_RED" else "UP_RED"
    flipped = any(x["day"]==day and x["dir"]==opp and mt < dt(day,x["t"]) <= ft for x in MU)
    return dict(lead=lead, gap=m["gap"], expand=m["expand"], hold=not flipped)
OUT=[]
for r in csv.DictReader(open("out_regime2.csv",encoding="utf-8-sig")):
    g=lambda k: None if r[k] in ("",None) else float(r[k])
    if g("r30") is None or g("mae") is None: continue
    b=tag(r["day"],r["flag_at"],r["dir"])
    OUT.append(dict(day=r["day"],flag=r["flag_at"],t3=r["t3_at"],dir=r["dir"],
                    mu_status=r["mu_status"],
                    lead=None if b is None else b["lead"],
                    tgap=None if b is None else b["gap"],
                    texp=None if b is None else b["expand"],
                    thold=None if b is None else b["hold"],
                    r15=g("r15"),r30=g("r30"),mfe=g("mfe"),mae=g("mae"),touch=r["touch"]))
for o in OUT:
    o["win"]=o["touch"]=="UP"; o["pnl"]=-1.6 if o["mae"]<=-1.6 else o["r30"]
with open("out_tag2.csv","w",newline="",encoding="utf-8-sig") as fo:
    w=csv.DictWriter(fo,fieldnames=list(OUT[0])); w.writeheader(); w.writerows(OUT)
nh=sum(1 for o in OUT if o["thold"] is True)
print(f"HY 플래그 {len(OUT)}건 / 태그 {sum(1 for o in OUT if o['lead'] is not None)}건 "
      f"/ 그중 MU 방향유지 {nh}건", file=sys.stderr)
