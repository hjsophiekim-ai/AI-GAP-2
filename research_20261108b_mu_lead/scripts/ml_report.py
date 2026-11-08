"""MU 선행 플래그 — 조건 깔때기 + 괴리 분위수 + 11/06 사례."""
import csv, statistics as st, sys
sys.stdout.reconfigure(encoding="utf-8")
R=[]
for r in csv.DictReader(open("out_mulead.csv",encoding="utf-8-sig")):
    g=lambda k: None if r[k] in ("",None) else float(r[k])
    b=lambda k: r[k]=="True"
    if g("r30") is None or g("mae") is None: continue
    r.update(er60=g("er60"),gap=g("gap"),gap3=g("gap3"),r15=g("r15"),r30=g("r30"),
             mfe=g("mfe"),mae=g("mae"),lead=g("hy_lead_min"),
             chop=b("chop"),hold=b("hold"),expand=b("expand"),widen=b("gap_widen"))
    r["win"]=r["touch"]=="UP"; r["pnl"]=-1.6 if r["mae"]<=-1.6 else r["r30"]
    R.append(r)
def blk(lab,g):
    if not g: print(f"  {lab:46s} 0건"); return
    w=sum(1 for x in g if x["win"]); p=sum(1 for x in g if x["pnl"]>0)
    print(f"  {lab:46s} {len(g):3d}건 승률 {100*w/len(g):5.1f}% 실익 {p:3d} "
          f"손절반영 {sum(x['pnl'] for x in g)/len(g):+6.3f}% 합계 {sum(x['pnl'] for x in g):+8.2f} "
          f"MFE {sum(x['mfe'] for x in g)/len(g):+5.2f} MAE {sum(x['mae'] for x in g)/len(g):+6.2f}")
print(f"MU 플래그 전수 {len(R)}건 (15 거래일, 09:00~15:20)")
print()
print("=== 조건 깔때기 ===")
blk("전체", R)
a=[x for x in R if x["chop"] is False]; blk("① 비왕복장 (ER60 >= 0.1223)", a)
b=[x for x in a if x["hold"]];          blk("①+② T+3 까지 방향 유지", b)
c=[x for x in b if x["expand"]];        blk("①+②+ hist 확대", c)
d=[x for x in c if x["widen"]];         blk("①+②+확대+ MU-HY 괴리도 확대", d)
print()
print("=== (참고) 반대 조건 ===")
blk("왕복장 (ER60 < 0.1223)", [x for x in R if x["chop"] is True])
blk("T+3 에 방향 꺾임", [x for x in a if not x["hold"]])
print()
print("=== ③ 큰 괴리 — ①+②+확대 집합 안에서 괴리 3등분 ===")
h=[x for x in c if x["gap"] is not None]
h.sort(key=lambda x:x["gap"]); n=len(h); q=n//3
for lab,g in (("하",h[:q]),("중",h[q:n-q]),("상",h[n-q:])):
    if g: blk(f"  괴리 {lab} ({g[0]['gap']:+.2f}~{g[-1]['gap']:+.2f})", g)
print()
print("=== ④ 선행성 — ①+②+확대 집합 ===")
blk("하이닉스 같은방향 플래그가 뒤따름", [x for x in c if x["lead"] is not None])
blk("  그 중 30분 이내", [x for x in c if x["lead"] is not None and x["lead"]<=30])
blk("하이닉스 플래그 안 옴", [x for x in c if x["lead"] is None])
lead=[x["lead"] for x in c if x["lead"] is not None]
if lead: print(f"  선행 분수 중앙 {st.median(lead):.0f}분 (n={len(lead)})")
print()
print("=== 11/06 14:00~15:20 MU 플래그 ===")
print(f"  {'F_end':6s} {'T+3':6s} {'방향':10s} {'ER60':>7s} {'왕복':4s} {'유지':4s} {'확대':4s} "
      f"{'괴리':>7s} {'괴리확대':6s} {'HY선행':>6s} {'30분':>7s} {'MAE':>7s} {'손절반영':>8s}")
for x in sorted([x for x in R if x["day"]=="2026-11-06" and x["f_end"]>="14:00"],key=lambda z:z["f_end"]):
    print(f"  {x['f_end']:6s} {x['t3']:6s} {x['dir']:10s} {(x['er60'] or 0):7.4f} "
          f"{'왕복' if x['chop'] else '추세':4s} {'O' if x['hold'] else 'X':^4s} {'O' if x['expand'] else 'X':^4s} "
          f"{(x['gap'] if x['gap'] is not None else float('nan')):7.2f} {'O' if x['widen'] else 'X':^6s} "
          f"{('-' if x['lead'] is None else f'{x[chr(39)+chr(39)] if False else int(x['lead'])}분'):>6s} "
          f"{x['r30']:7.3f} {x['mae']:7.2f} {x['pnl']:8.3f}")
