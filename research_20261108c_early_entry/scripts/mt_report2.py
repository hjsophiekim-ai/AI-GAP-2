import csv, sys
sys.stdout.reconfigure(encoding="utf-8")
R=[]
for r in csv.DictReader(open("out_tag2.csv",encoding="utf-8-sig")):
    g=lambda k: None if r[k] in ("",None) else float(r[k])
    r.update(lead=g("lead"),tgap=g("tgap"),r15=g("r15"),r30=g("r30"),mfe=g("mfe"),mae=g("mae"))
    r["texp"]=r["texp"]=="True"; r["thold"]=r["thold"]=="True"
    r["win"]=r["touch"]=="UP"; r["pnl"]=-1.6 if r["mae"]<=-1.6 else r["r30"]
    R.append(r)
def blk(lab,g):
    if not g: print(f"  {lab:44s} 0건"); return
    w=sum(1 for x in g if x["win"]); p=sum(1 for x in g if x["pnl"]>0)
    print(f"  {lab:44s} {len(g):3d}건 승률 {100*w/len(g):5.1f}% 실익 {p:3d} "
          f"손절반영 {sum(x['pnl'] for x in g)/len(g):+6.3f}% 합계 {sum(x['pnl'] for x in g):+8.2f} "
          f"MFE {sum(x['mfe'] for x in g)/len(g):+5.2f} MAE {sum(x['mae'] for x in g)/len(g):+6.2f}")
print(f"HY 플래그 {len(R)}건 — 진입 시점은 기존 그대로, MU 는 태그만")
print()
print("=== ① MU 방향유지 조건의 효과 ===")
blk("태그 있음 ∧ MU 방향유지", [x for x in R if x["thold"]])
blk("태그 있음 ∧ MU 가 중간에 반대로 돔", [x for x in R if x["lead"] is not None and not x["thold"]])
blk("태그 없음", [x for x in R if x["lead"] is None])
print()
H=[x for x in R if x["thold"] and x["tgap"] is not None]
H.sort(key=lambda x:x["tgap"]); n=len(H); q=max(1,n//3)
print(f"=== ② MU 방향유지 안에서 괴리 3등분 (n={n}) ===")
for lab,g in (("괴리 하",H[:q]),("괴리 중",H[q:n-q]),("괴리 상",H[n-q:])):
    if g: blk(f"  {lab} ({g[0]['tgap']:+.2f}~{g[-1]['tgap']:+.2f})", g)
print()
print("=== ③ 방향유지 ∧ 괴리 상위 — 진입 후보(CONFIRM/WAIT)만 ===")
hi=H[n-q:]
blk("  전체", hi)
blk("  CONFIRM/WAIT 만", [x for x in hi if x["mu_status"] in ("ENTRY_CONFIRM","ENTRY_WAIT")])
print()
print("=== ④ 방향유지 ∧ 괴리 상위 전수 ===")
print(f"  {'날짜 플래그':17s} {'방향':10s} {'선행':>5s} {'괴리':>7s} {'MU상태':14s} {'MFE':>6s} {'MAE':>7s} {'30분':>7s} {'손절반영':>8s}")
for x in sorted(hi,key=lambda z:(z["day"],z["flag"])):
    print(f"  {x['day'][5:]+' '+x['flag']:17s} {x['dir']:10s} {int(x['lead']):4d}분 {x['tgap']:7.2f} "
          f"{(x['mu_status'] or '-'):14s} {x['mfe']:6.2f} {x['mae']:7.2f} {x['r30']:7.3f} {x['pnl']:8.3f}")
