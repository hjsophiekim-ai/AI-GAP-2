"""D1: 일자별 플래그 대장 + 플래그 크기 지표. READ-ONLY.

플래그 자체는 production 신호원장(replay JSON 의 sig)을 그대로 쓴다 -- 재구현하지 않는다.
크기 지표만 하이닉스 1분봉으로 오프라인 재계산한다(3분봉 = production resample 규약).
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
sys.path.insert(0, REPO)
DATA = REPO + "/research_20261004_chop_staged/wk/data"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
OUT = os.path.dirname(os.path.abspath(__file__))

KST = "Asia/Seoul"


def apath(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def bars3(day):
    """하이닉스 1분봉 -> 완성 3분봉. 08:50~08:59 단일가 padding 제외(production hotfix)."""
    f = f"{DATA}/replay_{day}_hynix_1m.csv"
    if not os.path.exists(f):
        return None
    df = pd.read_csv(f)
    dt = pd.to_datetime(df["datetime"])
    df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
    clk = df["datetime"].dt.time
    df = df[~((clk >= pd.Timestamp("08:50").time()) & (clk < pd.Timestamp("09:00").time()))]
    g = (df.set_index("datetime").resample("3min", label="left", closed="left")
           .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
           .dropna(subset=["close"]).reset_index())
    c = g["close"].astype(float)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    g["hist"] = macd - macd.ewm(span=9, adjust=False).mean()
    # ATR14 (3분봉)
    pc = c.shift(1)
    tr = pd.concat([(g["high"] - g["low"]).abs(), (g["high"] - pc).abs(), (g["low"] - pc).abs()], axis=1).max(axis=1)
    g["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    return g


rows = []
DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
DAYS = sorted(set(DAYS) | {"20261002"})
for day in DAYS:
    g = bars3(day)
    if g is None:
        continue
    idx = {t.strftime("%H%M%S"): i for i, t in enumerate(g["datetime"])}
    o = json.load(open(apath(day), encoding="utf-8"))
    for s in o["sig"]:
        cb = s["completed_bar_at"]
        i = idx.get(cb)
        det = pd.Timestamp(s["detected_at"])
        if i is None or i < 27:
            continue
        px = float(g["close"].iloc[i])
        atr = float(g["atr"].iloc[i]) or np.nan
        h0, h1 = float(g["hist"].iloc[i]), float(g["hist"].iloc[i - 1])
        rows.append(dict(
            day=day, at=det.strftime("%H:%M"), mins=det.hour * 60 + det.minute,
            dir=s["direction"], res=s["order_result"], why=s.get("block_reason", ""),
            # ① 히스토그램 충격 = 플래그봉의 hist 변화 / ATR  (major_flag_filter 규약)
            hi_atr=abs(h0 - h1) / atr if atr == atr else np.nan,
            # ② 히스토그램 절대크기 (bp)
            hi_bp=abs(h0) / px * 10000,
            # ③ 가격 충격 = 플래그봉 종가변화 / ATR
            pi_atr=abs(px - float(g["close"].iloc[i - 1])) / atr if atr == atr else np.nan,
            # ④ 플래그봉 몸통 / ATR
            body=abs(px - float(g["open"].iloc[i])) / atr if atr == atr else np.nan,
            # ⑤ 직전 12봉(36분) 실현 range %
            rng36=(float(g["high"].iloc[i - 11:i + 1].max()) - float(g["low"].iloc[i - 11:i + 1].min())) / px * 100,
            px=px,
        ))

T = pd.DataFrame(rows)
T.to_csv(OUT + "/flags.csv", index=False, encoding="utf-8")
print(f"# 플래그 {len(T)}건 / {T.day.nunique()}일")
print(T.groupby("dir").size().to_string())
print("\n## 크기 지표 분위수 (전체 플래그)")
print(T[["hi_atr", "hi_bp", "pi_atr", "body", "rng36"]].describe(
    percentiles=[.25, .5, .75, .9]).round(3).to_string())
