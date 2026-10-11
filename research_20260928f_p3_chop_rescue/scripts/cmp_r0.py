import pickle,sys,bisect,numpy as np,pandas as pd
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8"); sys.path.insert(0,"proj"); sys.path.insert(0,"proj/scripts")
L=lambda n:pickle.load(open(f"out_{n}.pkl","rb"))["trades"]
D=list(pickle.load(open("_ctx80.pkl","rb"))["dates"]); R30=D[-30:]; SEP=[d for d in D if d[:6]=="202609"]
B,R0=L("BASE"),L("R0")
pnl=lambda t:t["net_pct"]*t["w1a"]; key=lambda t:(t["date"],str(pd.Timestamp(t["entry_time"])),t["direction"])
def comp(ts,ds):
    ds=set(ds); e=1.0
    for t in sorted((x for x in ts if x["date"] in ds),key=lambda x:x["exit_time"]): e*=1+pnl(t)/100
    return (e-1)*100
def pf(ts):
    g=sum(pnl(t) for t in ts if pnl(t)>0); l=-sum(pnl(t) for t in ts if pnl(t)<0); return g/l
def mdd(ts):
    e=pk=1.0; m=0
    for t in sorted(ts,key=lambda x:x["exit_time"]): e*=1+pnl(t)/100; pk=max(pk,e); m=min(m,e/pk-1)
    return m*100
def excl(ts,k):
    top=set(map(id,sorted(ts,key=pnl,reverse=True)[:k])); return comp([t for t in ts if id(t) not in top],D)
def fac(ts):
    f=defaultdict(lambda:1.0)
    for t in sorted(ts,key=lambda x:x["exit_time"]): f[t["date"]]*=1+pnl(t)/100
    return np.array([f[d] for d in D])
def daily(ts): return (fac(ts)-1)*100
rng=np.random.default_rng(20260928); IDX=rng.integers(0,len(D),size=(10000,len(D)))
F0=fac(R0); K0={key(t):t for t in R0}; BK={key(t):t for t in B}
rows=sorted(((pd.Timestamp(t["exit_time"]),t["h50_held"],t["tp1_hit"]) for t in B),key=lambda r:r[0]); ex=[r[0] for r in rows]
def rg(ts):
    k=bisect.bisect_left(ex,pd.Timestamp(ts))
    if k<10: return "WARMUP"
    r=rows[k-10:k]; return "CHOP" if sum(z[1] for z in r)>=4 and sum(z[2] for z in r)<=2 else "TREND"
print("%-4s %4s %9s %8s %8s %6s %7s %6s | %8s %6s %6s | 2%%일 손실일"%("전략","n","80일","최근30","9월","PF","MDD","WR","Δ80","Top5x","Top10x"))
for n in ["R0"]+sys.argv[1:]:
    ts=L(n); dd=daily(ts)
    print("%-4s %4d %9.3f %8.3f %8.3f %6.3f %7.3f %6.2f | %+8.3f %+6.2f %+6.2f | %3d %4d"%(n,len(ts),comp(ts,D),comp(ts,R30),comp(ts,SEP),pf(ts),mdd(ts),
        100*np.mean([pnl(t)>0 for t in ts]),comp(ts,D)-comp(R0,D),excl(ts,5)-excl(R0,5),excl(ts,10)-excl(R0,10),(dd>=2).sum(),(dd<0).sum()))
for n in sys.argv[1:]:
    ts=L(n); Fk=fac(ts); VK={key(t):t for t in ts}
    loo=[(np.prod(Fk)/Fk[i]-np.prod(F0)/F0[i])*100 for i in range(len(D))]
    bs=(np.prod(Fk[IDX],axis=1)-np.prod(F0[IDX],axis=1))*100
    print(f"\n[{n}] LOO 음수 {sum(x<0 for x in loo)}/80 min {min(loo):+.3f} | bootstrap {bs.mean():+.3f} [{np.percentile(bs,2.5):+.3f},{np.percentile(bs,97.5):+.3f}] P(>0) {100*(bs>0).mean():.1f}% P(<0) {100*(bs<0).mean():.1f}%")
    tr0={(key(t),round(t["net_pct"],8)) for t in R0 if rg(t["entry_time"])!="CHOP"}; trk={(key(t),round(t["net_pct"],8)) for t in ts if rg(t["entry_time"])!="CHOP"}
    print(f"   TREND/WARMUP diff: {len(tr0^trk)}")
    dd={D[i]:(Fk[i]-F0[i])*100 for i in range(len(D)) if abs(Fk[i]-F0[i])>1e-12}
    print("   다른 날:",", ".join(f"{d} {v:+.2f}" for d,v in dd.items()))
    for kk in sorted(set(K0)|set(VK)):
        a,b=K0.get(kk),VK.get(kk)
        if a is None or b is None or abs(a["net_pct"]-b["net_pct"])>1e-9:
            base=BK.get(kk)
            print(f"   {kk[0]} {kk[1][11:16]} {kk[2][:4]} | R0 {'-' if a is None else f'{a[chr(110)+chr(101)+chr(116)+chr(95)+chr(112)+chr(99)+chr(116)]:+.3f} {a[chr(101)+chr(120)+chr(105)+chr(116)+chr(95)+chr(114)+chr(101)+chr(97)+chr(115)+chr(111)+chr(110)][:16]}'} | {n} {'-' if b is None else f'{b[chr(110)+chr(101)+chr(116)+chr(95)+chr(112)+chr(99)+chr(116)]:+.3f} {b[chr(101)+chr(120)+chr(105)+chr(116)+chr(95)+chr(114)+chr(101)+chr(97)+chr(115)+chr(111)+chr(110)][:16]}'}{' Y' if b and b.get('b3_y3') else ''}{' YP' if b and b.get('b3_y3p') else ''} | BASE {'-' if base is None else f'{base[chr(110)+chr(101)+chr(116)+chr(95)+chr(112)+chr(99)+chr(116)]:+.3f} MFE {base[chr(112)+chr(101)+chr(97)+chr(107)+chr(95)+chr(110)+chr(101)+chr(116)+chr(95)+chr(112)+chr(99)+chr(116)]:.2f}'}")
