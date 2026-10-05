"""A: BOX-WIDTH 단변량 · B: RS × BOX-WIDTH 상호작용. READ-ONLY. 임계 최적화 없음.

box width = 진입 직전 30개 완성 1분봉의 (고가−저가)/종가 × 100.
3분위 경계는 train(05-27~07-31) 에서만 만들고 test(08-01~10-01) 에 그대로 적용.
RS 는 기존 rs_table.json / 공식 / 상위20% cutoff 를 그대로 쓴다.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
DATA = REPO + "/research_20261002_sept_strategy_replay/data"
KST = "Asia/Seoul"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
PREV = {}
for l in open(O3 + "/../jobs4.txt"):
    p = l.split()
    if len(p) >= 5 and p[1] == "A":
        PREV[p[2]] = (p[3], p[4])
PREV.setdefault("20261002", ("20261001", "20260930"))
_c = {}


def hy(day):
    if day not in _c:
        parts = []
        for x in (*PREV.get(day, ()), day):
            try:
                d = pd.read_csv(f"{DATA}/replay_{x}_hynix_1m.csv")
                d["datetime"] = pd.to_datetime(d["datetime"].astype(str).str[:19]).dt.tz_localize(KST)
                parts.append(d)
            except FileNotFoundError:
                pass
        _c[day] = pd.concat(parts).drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)
    return _c[day]


def apath(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
rows = []
for day in DAYS + ["20261002"]:
    for t in trades(json.load(open(apath(day), encoding="utf-8"))):
        d = hy(day)
        done = d[(d["datetime"] + pd.Timedelta(minutes=1) <= t["entry"]) & (d["datetime"].dt.date == t["entry"].date())]
        if len(done) < 30:
            continue
        w = done.iloc[-30:]
        px = float(w["close"].iloc[-1])
        rs_ = "+".join(dict.fromkeys(t["reasons"]))
        rows.append(dict(day=day, entry=t["entry"], box_w=float((w["high"].max() - w["low"].min()) / px * 100),
                         krw=t["krw"], net=t["net"], rs=rs_,
                         sl=int("STOP_LOSS" in rs_ or rs_ == "B3_SL"), run=int("TP2_FULL" in rs_)))
D = pd.DataFrame(rows)
D["seg"] = np.where(D.day < "20260801", "train", np.where(D.day <= "20261001", "test", "1002"))

# ── RS 소속 (기존 공식/표/cutoff 그대로) ──────────────────────────────
S = pd.read_csv(ROOT + "/score_features.csv"); S["day"] = S["day"].astype(str)
T = json.load(open(ROOT + "/rs_table.json", encoding="utf-8"))
SGN = {"atr60": 1.0, "min_open": -1.0, "xc30": 1.0}
S["key"] = S["day"] + " " + pd.to_datetime(S["entry"]).dt.strftime("%H:%M:%S")


def rs_hit(r):
    t = T.get(r["day"])
    if t is None:
        return False
    v = sum(SGN[k] * (r[k] - t["mu"][k]) / t["sd"][k] for k in SGN)
    return bool(v >= t["thr"])


S["rs_hit"] = S.apply(rs_hit, axis=1)
D["key"] = D["day"] + " " + D["entry"].dt.strftime("%H:%M:%S")
D = D.merge(S[["key", "rs_hit"]], on="key", how="left")
D["rs_hit"] = D["rs_hit"].fillna(False).astype(bool)

tr = D[D.seg == "train"]
q1, q2 = np.quantile(tr.box_w, [1 / 3, 2 / 3])
print(f"# 거래 {len(D)}건 (train {len(tr)} / test {int((D.seg=='test').sum())} / 10-02 {int((D.seg=='1002').sum())})")
print(f"# train 3분위 경계: Low < {q1:.3f}% <= Mid < {q2:.3f}% <= High  (test 에 그대로 적용)")
D["bw"] = pd.cut(D.box_w, [-1, q1, q2, 99], labels=["Low", "Mid", "High"])


def tab(z, title):
    print(f"\n### {title} ({len(z)}건, 총 {z.krw.sum():+,.0f}원)")
    print("| box width | 거래수 | 평균손익 | 승률% | 손절률% | runner율% | 총손익 |")
    print("|---|---|---|---|---|---|---|")
    for b in ("Low", "Mid", "High"):
        y = z[z.bw == b]
        if not len(y):
            print(f"| {b} | 0 | — | — | — | — | — |"); continue
        print(f"| {b} | {len(y)} | {y.krw.mean():+,.0f} | {(y.krw>0).mean()*100:.1f} | "
              f"{y.sl.mean()*100:.1f} | {y.run.mean()*100:.1f} | {y.krw.sum():+,.0f} |")


print("\n## A. BOX-WIDTH 단변량")
tab(D[D.seg == "train"], "train 05-27~07-31")
tab(D[D.seg == "test"], "test 08-01~10-01")


def auc(y, s):
    y = np.asarray(y, float); s = np.asarray(s, float)
    if y.sum() in (0, len(y)):
        return np.nan
    r = pd.Series(s).rank().to_numpy(); n1 = y.sum()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * (len(y) - n1))


print("\n  단변량 AUC(손절): train %.3f / test %.3f" % (auc(tr.sl, tr.box_w), auc(D[D.seg == 'test'].sl, D[D.seg == 'test'].box_w)))
print("  단변량 AUC(runner): train %.3f / test %.3f" % (auc(tr.run, tr.box_w), auc(D[D.seg == 'test'].run, D[D.seg == 'test'].box_w)))
for lab in ("train", "test"):
    z = D[D.seg == lab]
    m = z.groupby("bw", observed=True).krw.mean()
    print(f"  {lab} 평균손익 방향: Low {m.get('Low', float('nan')):+,.0f} → Mid {m.get('Mid', float('nan')):+,.0f} → High {m.get('High', float('nan')):+,.0f}")

print("\n## B. RS 상위20% 안에서 box width 3구간")
for lab in ("train", "test"):
    z = D[(D.seg == lab) & (D.rs_hit)]
    tab(z, f"RS 상위20% · {lab}")
    zz = D[(D.seg == lab) & (~D.rs_hit)]
    print(f"  (대조) RS 비해당 {len(zz)}건 평균 {zz.krw.mean():+,.0f} · 총 {zz.krw.sum():+,.0f}")

print("\n## RS 효과(= RS해당 − RS비해당 평균손익) 를 box width 구간별로")
print("| 구간 | train RS해당 | train 비해당 | train 차이 | test RS해당 | test 비해당 | test 차이 |")
print("|---|---|---|---|---|---|---|")
for b in ("Low", "Mid", "High"):
    out = [b]
    for lab in ("train", "test"):
        h = D[(D.seg == lab) & (D.bw == b) & (D.rs_hit)]
        n = D[(D.seg == lab) & (D.bw == b) & (~D.rs_hit)]
        hm = h.krw.mean() if len(h) else np.nan
        nm = n.krw.mean() if len(n) else np.nan
        out += [f"{hm:+,.0f} ({len(h)})" if len(h) else "—", f"{nm:+,.0f} ({len(n)})" if len(n) else "—",
                f"{hm-nm:+,.0f}" if len(h) and len(n) else "—"]
    print("| " + " | ".join(out) + " |")

print("\n## 10/01 · 10/02 거래의 box width 구간")
for _, r in D[D.day.isin(["20261001", "20261002"])].sort_values(["day", "entry"]).iterrows():
    print(f"  {r['day']} {r['entry']:%H:%M} box_w {r['box_w']:.3f}% [{r['bw']}] RS{'O' if r['rs_hit'] else '-'} · {r['rs']} {r['krw']:+,.0f}")
D.to_csv(ROOT + "/bw_trades.csv", index=False, encoding="utf-8")
