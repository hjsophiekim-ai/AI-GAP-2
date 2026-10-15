"""연구용 join — MACD2 결정시점 × 1초 스냅샷. 로거와 별개로 장 마감 후에만 돌린다(실시간 판단에 쓰지 않음).

입력 결정표 (CSV/pickle): eid, day(YYYYMMDD), dp(A|B|C), t(결정 시각), dir(UP_RED|DOWN_BLUE), entry_price(보유 ETF 체결가)
  A = 첫 net <= -0.8 / B = 첫 net >= +0.8 / C = 첫 반대 플래그  (research_20261014c 결정표와 같은 정의·컬럼)
출력: 결정시점별 feature (t 이하 스냅샷만) + label_* (t 이후, 사후 연구 전용).

feature 접두사: e_ = 보유 ETF(이미 방향 반영), h_ = 하이닉스(보유방향 s 로 부호 정렬: imb·d_imb 는 s 곱,
  buy_ratio 는 s=-1 이면 1-ratio, 체결강도는 s*ln(cttr/100)).
라벨 (사용자 정의, 보유 ETF 체결가 기준):
  A: 15분 안에 가격 >= 진입가 → recover / net <= -2.0 → collapse (먼저 닿은 쪽) / 둘 다 아니면 neutral
  B: net >= +3.0 → cont3, net >= +2.0 → cont2 가 net <= +0.2 (giveback) 보다 먼저 / 15:00 까지 미결 neutral
  C: 15분 뒤 net >= 결정시점 net → whipsaw(원래 방향 유지) / 아니면 true_reversal
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

LONG, INV, HYNIX = "0193T0", "0197X0", "000660"
WINDOWS = (10, 30, 60)
MAX_AGE_S = 5.0          # 결정시점 직전 스냅샷이 이보다 오래되면 data_ok=False (끊김/장외)


def load_snap(root, day, sym) -> pd.DataFrame | None:
    d = str(day)
    d = d if "-" in d else f"{d[:4]}-{d[4:6]}-{d[6:]}"          # 저장 디렉토리는 YYYY-MM-DD
    p = Path(root) / "snapshots" / d / f"{sym}.csv"
    if not p.exists():
        return None
    x = pd.read_csv(p); x["ts"] = pd.to_datetime(x["ts"])
    return x.sort_values("ts").reset_index(drop=True)


def _feat(x: pd.DataFrame, t, s: int, pre: str) -> dict:
    f = {}
    h = x[x.ts <= t]
    if h.empty:
        return {f"{pre}n_snap": 0}
    last = h.iloc[-1]
    sg = lambda v: (s * v) if pd.notna(v) else np.nan
    cl = lambda v: (s * math.log(v / 100)) if (pd.notna(v) and v > 0) else np.nan
    br = lambda v: (v if s == 1 else 1 - v) if pd.notna(v) else np.nan
    f[f"{pre}age_s"] = (t - last.ts).total_seconds()
    for c in ("imb_1", "imb_5", "imb_10", "imb_total", "d_imb1_1s", "d_imb1_5s", "d_imb1_10s", "d_imb5_1s", "d_imb5_5s", "d_imb5_10s"):
        f[f"{pre}{c}"] = sg(last.get(c))
    f[f"{pre}cttr_log"] = cl(last.get("cttr"))
    for k in (1, 5, 10):
        f[f"{pre}d_cttr_{k}s"] = sg(last.get(f"d_cttr_{k}s"))
    f[f"{pre}buy_ratio_10s"] = br(last.get("buy_ratio_10s"))
    f[f"{pre}trades_per_s_10"] = last.get("trades_per_s_10"); f[f"{pre}spread_bp"] = last.get("spread_bp")
    for w in WINDOWS:
        g = h[h.ts > t - pd.Timedelta(seconds=w)]
        b, sl = g.buy_vol_1s.sum(), g.sell_vol_1s.sum()
        f[f"{pre}imb5_mean_{w}s"] = sg(g.imb_5.mean())
        f[f"{pre}buy_ratio_{w}s"] = br(b / (b + sl)) if (b + sl) > 0 else np.nan
        f[f"{pre}trade_rate_{w}s"] = g.trades_1s.sum() / w
        old = h[h.ts <= t - pd.Timedelta(seconds=w)]
        f[f"{pre}d_cttr_log_{w}s"] = (f[f"{pre}cttr_log"] - cl(old.iloc[-1].cttr)) if len(old) else np.nan
        f[f"{pre}d_spread_bp_{w}s"] = (last.spread_bp - old.iloc[-1].spread_bp) if len(old) else np.nan
    f[f"{pre}n_snap"] = len(h)
    return f


def _label(x: pd.DataFrame, t, dp, entry, net_t):
    a = x[(x.ts > t) & x.price.notna()]
    net = (a.price / entry - 1) * 100
    if dp == "A":
        w = net[a.ts <= t + pd.Timedelta(minutes=15)]
        for v in w:
            if v >= 0: return "recover"
            if v <= -2.0: return "collapse"
        return "neutral"
    if dp == "B":
        w = net[a.ts.dt.time <= pd.Timestamp("15:00").time()]
        for v in w:
            if v <= 0.2: return "giveback"
            if v >= 3.0: return "cont3"
            if v >= 2.0: return "cont2"
        return "neutral"
    after = a[a.ts >= t + pd.Timedelta(minutes=15)]
    if after.empty: return None
    return "whipsaw" if (after.price.iloc[0] / entry - 1) * 100 >= net_t else "true_reversal"


def build(decisions: pd.DataFrame, root) -> pd.DataFrame:
    rows = []
    for day, g in decisions.groupby("day"):
        snaps = {s: load_snap(root, str(day), s) for s in (LONG, INV, HYNIX)}
        for r in g.itertuples():
            t = pd.Timestamp(r.t); s = 1 if r.dir == "UP_RED" else -1
            sym = LONG if s == 1 else INV
            e, hy = snaps[sym], snaps[HYNIX]
            row = dict(eid=r.eid, day=day, dp=r.dp, t=t, dir=r.dir, entry_price=r.entry_price)
            if e is None or hy is None:
                row["data_ok"] = False; rows.append(row); continue
            ep = e[e.ts <= t].price.dropna()
            row["net_t"] = (ep.iloc[-1] / r.entry_price - 1) * 100 if len(ep) else np.nan
            row.update(_feat(e, t, 1, "e_")); row.update(_feat(hy, t, s, "h_"))
            row["data_ok"] = bool(row.get("e_n_snap", 0) > 0 and row.get("h_n_snap", 0) > 0
                                  and row.get("e_age_s", 1e9) <= MAX_AGE_S and row.get("h_age_s", 1e9) <= MAX_AGE_S)
            row["label"] = _label(e, t, r.dp, r.entry_price, row["net_t"])
            rows.append(row)
    return pd.DataFrame(rows)
