"""실시간 WHIPSAW SCORE 검증 1단계 — 매 3분 점수 생성. 미래정보 없음. READ-ONLY.

매 3분 시점 t 에서 t 이전 데이터만으로 6개 feature 를 만들고 0~100 점으로 합성한다.
퍼센타일 변환표는 **train(05-27~07-31) 구간에서만** 만들어 test 에 그대로 적용한다
(test 분포를 보지 않는다). 임계 탐색·가중치 튜닝 없음 — 6개 동일가중 평균.
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
STOP_PCT = 1.0            # P3 B3 손절 기준선
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
        d = pd.concat(parts).drop_duplicates("datetime", keep="last")
        _c[day] = d.sort_values("datetime").reset_index(drop=True)
    return _c[day]


def apath(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def flags(day):
    """production 확정 플래그 검출 시각/방향 (signal_id 접미사 없는 행 = 플래그봉 검출)."""
    o = json.load(open(apath(day), encoding="utf-8"))
    out = []
    for s in o["sig"]:
        sid = str(s.get("signal_id") or "")
        if ":" in sid:
            continue
        try:
            out.append((pd.Timestamp(s["detected_at"]), str(s.get("direction") or "")))
        except Exception:
            pass
    return sorted(set(out))


def feats_at(day, t, fl):
    """t 시점(미만 데이터만)의 6개 raw feature. 부족하면 None."""
    d = hy(day)
    done = d[(d["datetime"] + pd.Timedelta(minutes=1) <= t) & (d["datetime"].dt.date == t.date())]
    if len(done) < 60:
        return None
    w30 = done.iloc[-30:]
    w60 = done.iloc[-60:]
    p30 = done.iloc[-60:-30]
    c = w30["close"].to_numpy(float)
    path = float(np.abs(np.diff(c)).sum())
    if path <= 0:
        return None
    pe30 = abs(c[-1] - c[0]) / path                       # ① 낮을수록 왕복
    rng30 = float((w30["high"].max() - w30["low"].min()) / c[-1] * 100)   # ② 최근 range
    ca = done["close"].astype(float)
    ema = ca.ewm(span=20, adjust=False).mean()
    s = np.sign((ca - ema).to_numpy())[-30:]
    s = s[s != 0]
    xc30 = int((np.diff(s) != 0).sum()) if len(s) > 1 else 0              # ③ EMA20 교차
    fw = [x for x in fl if t - pd.Timedelta(minutes=60) <= x[0] < t]
    flip = sum(1 for i in range(1, len(fw)) if fw[i][1] != fw[i - 1][1])  # ④ RED<->BLUE flip
    pr = (p30["high"].max() - p30["low"].min()) if len(p30) else np.nan
    cur = w30["high"].max() - w30["low"].min()
    expand = (cur / pr) if (pr and pr > 0) else np.nan                    # ⑤ 고저 확장비
    # ⑥ 최근 swing 이 -1% 손절을 건드릴 폭인가: 30분 내 최대 역행폭(양방향 중 큰 쪽)/1.0%
    hi = w30["high"].to_numpy(float); lo = w30["low"].to_numpy(float)
    dd_long = float(np.max([(np.max(hi[:i + 1]) - lo[i]) / np.max(hi[:i + 1]) * 100 for i in range(len(lo))]))
    dd_short = float(np.max([(hi[i] - np.min(lo[:i + 1])) / np.min(lo[:i + 1]) * 100 for i in range(len(hi))]))
    swing = max(dd_long, dd_short) / STOP_PCT
    return dict(pe30=pe30, rng30=rng30, xc30=xc30, flip=flip, expand=expand, swing=swing)


DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
ALL = DAYS + ["20261002"]
rows = []
for day in ALL:
    fl = flags(day)
    t0 = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} 09:00:00", tz=KST)
    for k in range(0, 121):
        t = t0 + pd.Timedelta(minutes=3 * k)
        if t.hour >= 15:
            break
        f = feats_at(day, t, fl)
        if f:
            f.update(day=day, t=t.isoformat())
            rows.append(f)
F = pd.DataFrame(rows)
print(f"# 3분 격자 {len(F)}개 / {F.day.nunique()}일")

# ── train 구간 퍼센타일 변환표 (test 분포 미사용) ─────────────────────
TR = F[(F.day < "20260801")]
COLS = ["pe30", "rng30", "xc30", "flip", "expand", "swing"]
ORI = {"pe30": -1, "rng30": +1, "xc30": +1, "flip": +1, "expand": +1, "swing": +1}
QS = np.linspace(0, 1, 101)
TBL = {c: np.quantile(TR[c].dropna(), QS) for c in COLS}


def sub(c, v):
    if pd.isna(v):
        return np.nan
    p = float(np.searchsorted(TBL[c], v) )
    p = max(0.0, min(100.0, p))
    return p if ORI[c] > 0 else 100.0 - p


for c in COLS:
    F["s_" + c] = F[c].map(lambda v: sub(c, v))
F["score"] = F[["s_" + c for c in COLS]].mean(axis=1)
F.to_csv(ROOT + "/ws_scores.csv", index=False, encoding="utf-8")
print(f"# score 분포 train {TR.index.size}개 기준 변환")
print(F.groupby(F.day < "20260801")["score"].describe(percentiles=[.25, .5, .75, .9]).round(1).to_string())
print("\n# 구성요소 상관 (train)")
print(F[F.day < "20260801"][["s_" + c for c in COLS] + ["score"]].corr().round(2).to_string())
