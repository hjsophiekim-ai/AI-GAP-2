"""MU 선행 태그 — HY 진입 후보에 '직전 같은방향 MU 플래그' 를 붙여 승패가 갈리는지 (READ ONLY).

진입은 **기존 HY 플래그 시점 그대로**다. MU 는 진입을 만들지 않고 태그만 붙인다.
  tag_lead_min : HY 플래그봉 시각 기준, 같은 방향 MU 플래그가 몇 분 전에 떴나 (없으면 None)
  tag_gap      : 그 MU 플래그 시점의 mu_hy_gap (방향부호)
  tag_expand   : 그 MU 플래그가 T+3 에 유지·확대였나
새 임계값 없음 — 전부 분위수로만 가른다.
"""
import csv, sys
from datetime import datetime
sys.stdout.reconfigure(encoding="utf-8")

MU = []
for r in csv.DictReader(open("out_mulead.csv", encoding="utf-8-sig")):
    g = lambda k: None if r[k] in ("", None) else float(r[k])
    MU.append(dict(day=r["day"], t=r["f_end"], dir=r["dir"], gap=g("gap"),
                   expand=(r["expand"] == "True"), hold=(r["hold"] == "True")))

def tag(day, flag_hhmm, direction):
    """그 HY 플래그 직전의 같은 방향 MU 플래그."""
    ft = datetime.strptime(f"{day} {flag_hhmm}", "%Y-%m-%d %H:%M")
    best = None
    for m in MU:
        if m["day"] != day or m["dir"] != direction:
            continue
        mt = datetime.strptime(f"{m['day']} {m['t']}", "%Y-%m-%d %H:%M")
        if mt > ft:
            continue
        d = int((ft - mt).total_seconds() // 60)
        if best is None or d < best[0]:
            best = (d, m)
    return best

OUT = []
for r in csv.DictReader(open("out_regime2.csv", encoding="utf-8-sig")):
    g = lambda k: None if r[k] in ("", None) else float(r[k])
    if g("r30") is None or g("mae") is None:
        continue
    b = tag(r["day"], r["flag_at"], r["dir"])
    OUT.append(dict(day=r["day"], flag=r["flag_at"], t3=r["t3_at"], dir=r["dir"],
                    mu_status=r["mu_status"], er60="",
                    lead=None if b is None else b[0],
                    tgap=None if b is None else b[1]["gap"],
                    texp=None if b is None else b[1]["expand"],
                    r15=g("r15"), r30=g("r30"), mfe=g("mfe"), mae=g("mae"), touch=r["touch"]))
# er_60 보강 (out_mu.csv 에 있는 CONFIRM/WAIT 만)
ER = {}
for r in csv.DictReader(open("out_mu.csv", encoding="utf-8-sig")):
    if r["er_60"] not in ("", None):
        ER[(r["day"], r["t3_at"], r["dir"])] = float(r["er_60"])
for o in OUT:
    o["er60"] = ER.get((o["day"], o["t3"], o["dir"]))
    o["win"] = o["touch"] == "UP"
    o["pnl"] = -1.6 if o["mae"] <= -1.6 else o["r30"]

cols = list(OUT[0])
with open("out_tag.csv", "w", newline="", encoding="utf-8-sig") as fo:
    w = csv.DictWriter(fo, fieldnames=cols); w.writeheader(); w.writerows(OUT)
print(f"HY 플래그 {len(OUT)}건에 MU 선행 태그 부착 -> out_tag.csv", file=sys.stderr)
print(f"  태그 있음 {sum(1 for o in OUT if o['lead'] is not None)}건 / 없음 {sum(1 for o in OUT if o['lead'] is None)}건", file=sys.stderr)
