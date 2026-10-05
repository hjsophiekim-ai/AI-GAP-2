"""C1 + EMA20 cross count 보정. A(P3-R0) 실제 진입시점 기준, READ-ONLY.

PE  = |close_last - close_first| / sum(|diff(close)|)      (낮을수록 왕복)
RNG = (max(high) - min(low)) / close_last * 100             (낮을수록 저변동)
XC  = 최근 30분 1분봉 종가가 EMA20 을 위/아래로 가른 횟수   (많을수록 좁은 박스 왕복)
EMA20 은 당일 전체 완성봉으로 계산하고(워밍업), 교차 카운트만 마지막 30봉에서 센다.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
DATA = ROOT + "/wk/data"
KST = "Asia/Seoul"
WIN = 30
_cache = {}


def bars(day):
    if day not in _cache:
        d = pd.read_csv(f"{DATA}/replay_{day}_hynix_1m.csv")
        d["datetime"] = pd.to_datetime(d["datetime"].astype(str).str[:19]).dt.tz_localize(KST)
        _cache[day] = d.sort_values("datetime").reset_index(drop=True)
    return _cache[day]


def feat(day, at):
    d = bars(day)
    done = d[(d["datetime"] + pd.Timedelta(minutes=1) <= at) & (d["datetime"].dt.date == at.date())]
    if len(done) < WIN:
        return None
    c_all = done["close"].astype(float)
    ema = c_all.ewm(span=20, adjust=False).mean()
    w = done.iloc[-WIN:]
    c = w["close"].to_numpy(dtype=float)
    path = float(np.abs(np.diff(c)).sum())
    if path <= 0:
        return None
    pe = abs(c[-1] - c[0]) / path
    rng = float((w["high"].max() - w["low"].min()) / c[-1] * 100.0)
    s = np.sign((c_all - ema).to_numpy()[-WIN:])
    s = s[s != 0]
    xc = int((np.diff(s) != 0).sum()) if len(s) > 1 else 0
    return pe, rng, xc


def entries(path):
    o = json.load(open(path, encoding="utf-8"))
    exe = {pd.Timestamp(s["detected_at"]).strftime("%H:%M:%S"): s["signal_id"]
           for s in o["sig"] if s["order_result"] == "EXECUTED"}
    return [(o["D"], pd.Timestamp(x["t"]), exe.get(pd.Timestamp(x["t"]).strftime("%H:%M:%S"), ""))
            for x in o["orders"] if x["side"] == "BUY"]


DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
rows = []
for day in DAYS:
    for d, t, sid in entries(f"{O3}/REAL_A_{day}.json"):
        f = feat(d, t)
        if f:
            rows.append(dict(day=d, t=t, sid=sid, pe=f[0], rng=f[1], xc=f[2]))
df = pd.DataFrame(rows)
base = (df.pe < 0.20) & (df.rng < 1.0)
print(f"A 진입 {len(df)}건 · C1 기본조건(PE<0.20 & RNG<1.0) {int(base.sum())}건")
print("\n## C1 기본조건 통과 건의 EMA20 cross count 분포")
print(df[base]["xc"].value_counts().sort_index().to_string())
print("\n## 전체 진입의 cross count 분포 (대조)")
print(df["xc"].describe(percentiles=[.25, .5, .75, .9]).round(2).to_string())

print("\n## 10/02 세 signal")
for tt in ("10:00:00", "11:30:00", "11:51:00"):
    f = feat("20261002", pd.Timestamp(f"2026-10-02 {tt}", tz=KST))
    ok = "C1통과" if (f[0] < 0.20 and f[1] < 1.0) else "C1탈락"
    print(f"  {tt}  PE {f[0]:.3f}  RNG {f[1]:.3f}%  XC {f[2]}  [{ok}]")

print("\n## threshold 후보 비교")
print(f"{'조건':34s} {'적중':>5s} {'비율':>7s} {'일수':>5s}  10/02 적중")
o3 = {tt: feat("20261002", pd.Timestamp(f"2026-10-02 {tt}", tz=KST)) for tt in ("10:00:00", "11:30:00", "11:51:00")}
for name, k in (("C1 + XC>=3", 3), ("C1 + XC>=4", 4)):
    m = base & (df.xc >= k)
    hit = [tt[:5] for tt, f in o3.items() if f[0] < 0.20 and f[1] < 1.0 and f[2] >= k]
    print(f"{name:34s} {int(m.sum()):>3d}건 {m.mean()*100:>6.1f}% {df[m]['day'].nunique():>4d}일  {' '.join(hit) or '없음'}")
    print(f"      발동일: {' '.join(sorted(df[m]['day'].unique()))}")
df.to_csv(ROOT + "/calib2_entries.csv", index=False, encoding="utf-8")
