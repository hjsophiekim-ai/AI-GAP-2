"""D12: 돌파대기 거리(dist) 가 '기다리면 손해'를 가르는가. READ-ONLY.

E 는 승인 즉시 사지 않고 (플래그봉, 확정봉) 극값을 trigger 로 최대 15분 기다린다.
dist = 승인시점 가격에서 trigger 까지의 거리(%, 하이닉스 기초자산).
dist 가 크다 = 플래그봉이 컸다 = 기다렸다 사면 그만큼 비싸게 산다.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
ROOT = REPO + "/research_20261004_chop_staged"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
BRKD = REPO + "/research_20261003_breakout_confirm/wk/out6"
OUT = os.path.dirname(os.path.abspath(__file__))
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
pb = lambda d: (f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json" if os.path.exists(f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json") else f"{BRKD}/REAL_BRK15_{d}.json")
pc = lambda d: (f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json" if os.path.exists(f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json") else pb(d))
pe = lambda d: (f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json" if os.path.exists(f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json") else pc(d))

D = pd.read_csv(OUT + "/days.csv", dtype={"day": str}).set_index("day")
rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    oe = json.load(open(pe(d), encoding="utf-8"))
    A = trades(json.load(open(ap(d), encoding="utf-8")))
    E = trades(oe)
    pair = float(D.loc[d, "pair_min_mfe"]) if d in D.index else 0.0
    ev = oe.get("brk") or []
    dist = {e["sid"]: e["dist"] for e in ev if e.get("ev") in ("EARLY_FAIL", "EARLY_PASS")}
    fired = {e["sid"]: e for e in ev if e.get("ev") == "FIRED"}
    armed = {e["sid"]: e for e in ev if e.get("ev") in ("ARMED_BRK15", "ARMED")}
    expired = {e["sid"] for e in ev if e.get("ev") == "EXPIRED"}
    for sid in armed:
        base = sid.split(":")[0]
        at = pd.Timestamp(armed[sid]["armed_at"])
        # 같은 신호의 A 거래 = 승인시각 +-20분, 같은 방향
        dirn = armed[sid]["dir"]
        want = "UP" if dirn == "UP_RED" else "DN"
        ma = next((t for t in A if t["dir"].startswith(want)
                   and abs((t["entry"] - at).total_seconds()) <= 20 * 60), None)
        f = fired.get(sid)
        me = None
        if f:
            ft = pd.Timestamp(f["at"])
            me = next((t for t in E if t["dir"].startswith(want)
                       and abs((t["entry"] - ft).total_seconds()) <= 120), None)
        rows.append(dict(day=d, pair=pair, at=at.strftime("%H:%M"), dir=dirn,
                         dist=dist.get(sid, np.nan),
                         fired=bool(f), delay=(f or {}).get("delay_min", np.nan),
                         expired=sid in expired,
                         a_krw=(ma["krw"] if ma else np.nan),
                         e_krw=(me["krw"] if me else (0.0 if sid in expired else np.nan)),
                         a_px=(ma["px_in"] if ma else np.nan),
                         e_px=(me["px_in"] if me else np.nan)))
B = pd.DataFrame(rows)
B["d"] = B.e_krw - B.a_krw
B.to_csv(OUT + "/brk.csv", index=False, encoding="utf-8")
print("# ev 라벨:", {k: int(v) for k, v in B.dist.isna().value_counts().items()}, "(True=dist 기록 없는 구버전 파일)")
print(f"# 돌파대기 등록(ARMED) {len(B)}건 · 체결 {int(B.fired.sum())} · 폐기 {int(B.expired.sum())}")

print("\n## dist 구간별 — '기다린 결과' E-A (A 와 매칭된 건만)")
M = B.dropna(subset=["d"])
M = M.assign(bin=pd.cut(M.dist, [-0.01, 0.3, 0.6, 1.0, 1.5, 99],
                        labels=["~0.3%", "0.3~0.6%", "0.6~1.0%", "1.0~1.5%", "1.5%+"]))
print("| dist | n | 체결 | 폐기 | E-A 합 | 건당 | 체결건 E-A | 폐기건 E-A |")
print("|---|---|---|---|---|---|---|---|")
for b, s in M.groupby("bin", observed=True):
    fo, ex = s[s.fired], s[s.expired]
    print(f"| {b} | {len(s)} | {int(s.fired.sum())} | {int(s.expired.sum())} | **{s.d.sum():+,.0f}** | "
          f"{s.d.mean():+,.0f} | {fo.d.sum():+,.0f} | {ex.d.sum():+,.0f} |")

print("\n## 같은 분해를 '오늘형 날' 로 한정")
MB = M[M.pair >= 1.0]
print("| dist | n | E-A 합 | 체결건 E-A | 폐기건 E-A |")
print("|---|---|---|---|---|")
for b, s in MB.groupby("bin", observed=True):
    print(f"| {b} | {len(s)} | **{s.d.sum():+,.0f}** | {s[s.fired].d.sum():+,.0f} | {s[s.expired].d.sum():+,.0f} |")

print("\n## 체결건: 진입가 불리폭 (하이닉스 dist vs 실제 ETF 진입가 차이)")
F = M[M.fired & M.a_px.notna() & M.e_px.notna()].copy()
F["pxgap"] = (F.e_px - F.a_px) / F.a_px * 100
F["pxgap"] = np.where(F.dir == "DOWN_BLUE", F.pxgap, F.pxgap)   # 인버는 이미 방향 반영된 ETF 가격
print(F.groupby("bin", observed=True).agg(n=("pxgap", "size"), ETF진입가차=("pxgap", "mean"),
                                          지연분=("delay", "mean"), EA=("d", "sum")).round(2).to_string())
