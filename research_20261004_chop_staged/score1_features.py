"""플래그 점수화 연구 1단계 — 진입시점 특징량 생성. 미래정보 없음. READ-ONLY.

각 A 거래(=확정 플래그 진입)마다, 그 시각까지의 하이닉스 1분봉/3분봉으로만
'장세 전반 + 플래그 모양' 특징을 만든다. 라벨은 그 거래의 실제 결과다.
방향성 특징은 보유방향 기준으로 부호를 맞춘다(UP 은 그대로, DOWN 은 반전).
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
_c = {}


PREV = {}
for _l in open(REPO + "/research_20261002_sept_strategy_replay/round3_85d/jobs4.txt"):
    _p = _l.split()
    if len(_p) >= 5 and _p[1] == "A":
        PREV[_p[2]] = (_p[3], _p[4])


def _one(day):
    d = pd.read_csv(f"{DATA}/replay_{day}_hynix_1m.csv")
    d["datetime"] = pd.to_datetime(d["datetime"].astype(str).str[:19]).dt.tz_localize(KST)
    return d


def hy(day):
    """지표 워밍업을 위해 전전일+전일+당일을 이어 붙인다 (production frame 과 같은 방식)."""
    if day not in _c:
        parts = []
        for x in (*PREV.get(day, ()), day):
            try:
                parts.append(_one(x))
            except FileNotFoundError:
                pass
        d = pd.concat(parts).drop_duplicates("datetime", keep="last")
        _c[day] = d.sort_values("datetime").reset_index(drop=True)
    return _c[day]


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def win_stats(c, h, l, n):
    """최근 n봉의 (path efficiency, realized range%, EMA20 교차수, 평균 절대변동%)"""
    c2, h2, l2 = c[-n:], h[-n:], l[-n:]
    path = float(np.abs(np.diff(c2)).sum())
    pe = abs(c2[-1] - c2[0]) / path if path > 0 else np.nan
    rng = (h2.max() - l2.min()) / c2[-1] * 100
    atr = float(np.abs(np.diff(c2)).mean()) / c2[-1] * 100 if len(c2) > 1 else np.nan
    return pe, rng, atr


def feats(day, at, direction, flag_dt):
    d = hy(day)
    done = d[d["datetime"] + pd.Timedelta(minutes=1) <= at]        # 연속 프레임(워밍업 포함)
    today = done[done["datetime"].dt.date == at.date()]              # 당일 구간
    if len(done) < 120 or len(today) < 30:
        return None
    c = done["close"].to_numpy(float)
    h = done["high"].to_numpy(float)
    l = done["low"].to_numpy(float)
    sgn = 1.0 if direction == "UP" else -1.0
    e20 = ema(done["close"].astype(float), 20).to_numpy()
    e50 = ema(done["close"].astype(float), 50).to_numpy()
    f = {}
    for n in (30, 60):
        pe, rng, atr = win_stats(c, h, l, n)
        f[f"pe{n}"], f[f"rng{n}"], f[f"atr{n}"] = pe, rng, atr
        f[f"ret{n}"] = sgn * (c[-1] - c[-n]) / c[-n] * 100
    s = np.sign((c - e20))[-30:]
    s = s[s != 0]
    f["xc30"] = int((np.diff(s) != 0).sum()) if len(s) > 1 else 0
    f["c_e20"] = sgn * (c[-1] - e20[-1]) / e20[-1] * 100
    f["c_e50"] = sgn * (c[-1] - e50[-1]) / e50[-1] * 100
    f["e20_e50"] = sgn * (e20[-1] - e50[-1]) / e50[-1] * 100
    dh, dl = today["high"].max(), today["low"].min()
    f["pos_in_day"] = (c[-1] - dl) / (dh - dl) if dh > dl else 0.5
    if direction != "UP":
        f["pos_in_day"] = 1 - f["pos_in_day"]
    f["day_rng"] = (dh - dl) / c[-1] * 100
    f["min_open"] = (at - at.normalize().tz_localize(None).tz_localize(KST) - pd.Timedelta(hours=9)).total_seconds() / 60
    f["dir_up"] = 1.0 if direction == "UP" else 0.0
    # 3분봉 MACD (특징 전용 계산)
    g = done.set_index("datetime").resample("3min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    if len(g) < 30:
        return None
    gc = g["close"].astype(float)
    macd = ema(gc, 12) - ema(gc, 26)
    sig = ema(macd, 9)
    gap = (macd - sig).to_numpy()
    f["gap"] = sgn * gap[-1] / c[-1] * 10000
    f["gap_exp"] = (abs(gap[-1]) / abs(gap[-2])) if len(gap) > 1 and gap[-2] != 0 else np.nan
    f["gap_slope"] = sgn * (gap[-1] - gap[-3]) / c[-1] * 10000 if len(gap) > 2 else np.nan
    # 플래그봉/확정봉 모양
    fb = g[g.index <= pd.Timestamp(flag_dt)] if flag_dt is not None else g.iloc[:-1]
    if len(fb):
        r = fb.iloc[-1]
        f["flag_body"] = sgn * (r["close"] - r["open"]) / r["open"] * 100
        f["flag_rng"] = (r["high"] - r["low"]) / r["close"] * 100
    cb = g.iloc[-1]
    f["conf_body"] = sgn * (cb["close"] - cb["open"]) / cb["open"] * 100
    f["conf_rng"] = (cb["high"] - cb["low"]) / cb["close"] * 100
    vm = g["volume"].iloc[-30:].mean()
    f["conf_vol"] = cb["volume"] / vm if vm > 0 else np.nan
    _pv = done["volume"].iloc[-90:-30].mean()
    f["vol30"] = done["volume"].iloc[-30:].mean() / _pv if _pv > 0 else np.nan
    return f


DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
rows = []
for day in DAYS:
    seq = sorted(trades(json.load(open(f"{O3}/REAL_A_{day}.json", encoding="utf-8"))), key=lambda t: t["entry"])
    nloss = 0
    for i, t in enumerate(seq, 1):
        sid = t.get("sid") or ""
        fdt = None
        try:
            p = sid.split(":")[0].split("_")
            fdt = pd.Timestamp(f"{p[0][:4]}-{p[0][4:6]}-{p[0][6:]} {p[1][:2]}:{p[1][2:4]}:{p[1][4:]}", tz=KST)
        except Exception:
            pass
        direction = "UP" if t["dir"].startswith("UP") else "DN"
        f = feats(day, t["entry"], direction, fdt)
        if f is None:
            continue
        rs = "+".join(dict.fromkeys(t["reasons"]))
        f.update(day=day, entry=t["entry"].isoformat(), ord=i, prior_loss=nloss,
                 krw=t["krw"], net=t["net"], rs=rs,
                 y_run=int("TP2_FULL" in rs), y_sl=int("STOP_LOSS" in rs or rs == "B3_SL"))
        rows.append(f)
        if t["krw"] < 0:
            nloss += 1
df = pd.DataFrame(rows)
df.to_csv(ROOT + "/score_features.csv", index=False, encoding="utf-8")
print(f"# 특징량 생성 {len(df)}건 / 전체 A 거래 중")
print(f"  러너(TP2) {int(df.y_run.sum())}건 · 손절 {int(df.y_sl.sum())}건 · 기타 {len(df)-int(df.y_run.sum())-int(df.y_sl.sum())}건")
print(f"  손익 합 {df.krw.sum():+,.0f}원 · 기간 {df.day.min()}~{df.day.max()}")
print(f"  특징 {len([c for c in df.columns if c not in ('day','entry','krw','net','rs','y_run','y_sl')])}개")
print("  결측 있는 특징:", {c: int(df[c].isna().sum()) for c in df.columns if df[c].isna().any()})
