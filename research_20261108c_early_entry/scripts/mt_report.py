"""MU 선행 태그 — 괴리 크기별로 HY 진입 성과가 갈리는가 (분위수만, 새 임계값 없음)."""
import csv, statistics as st, sys
sys.stdout.reconfigure(encoding="utf-8")
R=[]
for r in csv.DictReader(open("out_tag.csv",encoding="utf-8-sig")):
    g=lambda k: None if r[k] in ("",None) else float(r[k])
    r.update(lead=g("lead"), tgap=g("tgap"), er60=g("er60"),
             r15=g("r15"), r30=g("r30"), mfe=g("mfe"), mae=g("mae"))
    r["texp"]=r["texp"]=="True"; r["win"]=r["touch"]=="UP"
    r["pnl"]=-1.6 if r["mae"]<=-1.6 else r["r30"]
    R.append(r)
def blk(lab,g):
    if not g: print(f"  {lab:40s} 0건"); return
    w=sum(1 for x in g if x["win"]); p=sum(1 for x in g if x["pnl"]>0)
    print(f"  {lab:40s} {len(g):3d}건 승률 {100*w/len(g):5.1f}% 실익 {p:3d} "
          f"손절반영 {sum(x['pnl'] for x in g)/len(g):+6.3f}% 합계 {sum(x['pnl'] for x in g):+8.2f} "
          f"MFE {sum(x['mfe'] for x in g)/len(g):+5.2f} MAE {sum(x['mae'] for x in g)/len(g):+6.2f}")
print(f"HY 플래그 {len(R)}건 (10/19~11/06). 진입 시점은 기존 그대로, MU 는 태그만.")
print()
print("=== ① 태그 유무 (변별력 없음 확인) ===")
blk("MU 선행 태그 있음", [x for x in R if x["lead"] is not None])
blk("태그 없음", [x for x in R if x["lead"] is None])
print()
T=[x for x in R if x["tgap"] is not None]
T.sort(key=lambda x:x["tgap"]); n=len(T); q=n//4
print(f"=== ② 선행 MU 플래그의 괴리 4분위 (n={n}) ===")
for lab,g in (("Q1 최소",T[:q]),("Q2",T[q:2*q]),("Q3",T[2*q:3*q]),("Q4 최대",T[3*q:])):
    if g: blk(f"{lab} ({g[0]['tgap']:+.2f}~{g[-1]['tgap']:+.2f})", g)
print()
print("=== ③ 괴리 상위 25% 안에서 선행시간 ===")
hi=T[3*q:]; hi2=[x for x in hi if x["lead"] is not None]
hi2.sort(key=lambda x:x["lead"]); m=len(hi2)
if m>=8:
    h=m//2
    blk(f"  선행 짧음 ({hi2[0]['lead']:.0f}~{hi2[h-1]['lead']:.0f}분)", hi2[:h])
    blk(f"  선행 긺   ({hi2[h]['lead']:.0f}~{hi2[-1]['lead']:.0f}분)", hi2[h:])
print()
print("=== ④ 괴리 상위 25% × MU 가 T+3 에 유지·확대였나 ===")
blk("  확대 O", [x for x in hi if x["texp"]])
blk("  확대 X", [x for x in hi if not x["texp"]])
print()
print("=== ⑤ 실제 진입 후보(CONFIRM/WAIT)로 좁히면 ===")
C=[x for x in R if x["mu_status"] in ("ENTRY_CONFIRM","ENTRY_WAIT") and x["tgap"] is not None]
C.sort(key=lambda x:x["tgap"]); nc=len(C); qc=max(1,nc//3)
print(f"  (n={nc})")
for lab,g in (("괴리 하",C[:qc]),("괴리 중",C[qc:nc-qc]),("괴리 상",C[nc-qc:])):
    if g: blk(f"  {lab} ({g[0]['tgap']:+.2f}~{g[-1]['tgap']:+.2f})", g)
print()
print("=== ⑥ 괴리 상위 25% 전수 ===")
print(f"  {'날짜 플래그':17s} {'방향':10s} {'MU선행':>6s} {'태그괴리':>8s} {'확대':4s} {'MU상태':14s} "
      f"{'MFE':>6s} {'MAE':>7s} {'30분':>7s} {'손절반영':>8s}")
for x in sorted(hi,key=lambda z:(z["day"],z["flag"])):
    print(f"  {x['day'][5:]+' '+x['flag']:17s} {x['dir']:10s} "
          f"{('-' if x['lead'] is None else f'{int(x[chr(108)+chr(101)+chr(97)+chr(100)])}분'):>6s} "
          f"{x['tgap']:8.2f} {'O' if x['texp'] else 'X':^4s} {(x['mu_status'] or '-'):14s} "
          f"{x['mfe']:6.2f} {x['mae']:7.2f} {x['r30']:7.3f} {x['pnl']:8.3f}")
