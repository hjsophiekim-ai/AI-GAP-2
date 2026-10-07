"""D1 — TP(+1.0%) 도달 시점의 **경로형태(path-shape)** 진단. 엔진 실행 없음.

B3 에서 GX_TP 로 청산된 CHOP 거래는 **청산시각 = +1.0% 최초 도달 시각**이다.
그 순간까지의 정보만으로 경로 특징을 만들고, "BASE 에서 러너였는가(MFE>=3%)"로 라벨한다.

전부 인과적:
  · 1분 호가(quotes)는 그 틱까지
  · 3분봉은 **마지막 완성봉(도달틱 이전에 완성된 봉)** 까지만
"""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 340); pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 300)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
import axlib as A
ce.CACHE_DIR = Path(r"G:\다른 컴퓨터\내 노트북 (2)\Desktop\AI-GAP 2\data\cache")
H._CTX_CACHE = HERE / "_ctx81.pkl"
ctx = H.build_ctx(81)
from app.trading.macd2.models import Direction

W1 = pickle.load(open(HERE / "w1.pkl", "rb"))
Z = pickle.load(open(HERE / "z2.pkl", "rb"))
SH = W1["shadow"]
D = Z["dates"]

b = ctx.hynix_bars_3m.reset_index(drop=True)
bt = pd.to_datetime(b["datetime"])
date = bt.dt.strftime("%Y%m%d").values
bstart = bt.dt.tz_convert("Asia/Seoul") if bt.dt.tz is not None else bt.dt.tz_localize("Asia/Seoul")
close = b["close"].astype(float).values
high = b["high"].astype(float).values
low = b["low"].astype(float).values
vol = b["volume"].astype(float).values
e20 = b["close"].ewm(span=20, adjust=False).mean().values
e50 = b["close"].ewm(span=50, adjust=False).mean().values
vwap = np.full(len(b), np.nan); dayopen = np.full(len(b), np.nan)
cur, pv, pvv, op = None, 0.0, 0.0, None
tp3 = (high + low + close) / 3.0
for i in range(len(b)):
    if date[i] != cur:
        cur, pv, pvv, op = date[i], 0.0, 0.0, close[i]
    v = vol[i] if vol[i] > 0 else 1.0
    pv += tp3[i] * v; pvv += v
    vwap[i] = pv / pvv; dayopen[i] = op
# 당일 첫봉 종가 vs 전일 마지막봉 종가 = 갭(프리마켓 방향 대용, 인과적)
gap_open = np.zeros(len(b))
first_idx = {}
for i in range(len(b)):
    if date[i] not in first_idx:
        first_idx[date[i]] = i
days_sorted = sorted(first_idx)
prev_last = {}
for k, d_ in enumerate(days_sorted):
    if k == 0:
        continue
    pd_ = days_sorted[k - 1]
    li = max(j for j in range(len(b)) if date[j] == pd_)
    prev_last[d_] = close[li]
for i in range(len(b)):
    pl = prev_last.get(date[i])
    gap_open[i] = ((dayopen[i] - pl) / pl * 100.0) if pl else 0.0
flags = ctx.flags_by_idx
# 봉별 변동성(직전 40봉 3분수익률 std) — 과열도 표준화용
ret = np.zeros(len(b))
for i in range(1, len(b)):
    if date[i] == date[i - 1]:
        ret[i] = (close[i] - close[i - 1]) / close[i - 1] * 100
vol40 = pd.Series(ret).shift(1).rolling(40, min_periods=20).std().values


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["mfe"] = d.peak_net_pct
    d["chop"] = [bool(SH[int(i)]) if int(i) < len(SH) else False for i in d.decision_idx]
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    return d


BASE = prep(W1["runs"]["A_BASE"])
B3 = prep(Z["runs"]["A_B3"])
mfe_base = {r.k: float(r.mfe) for _, r in BASE.iterrows()}
net_base = {r.k: float(r.net_pct) for _, r in BASE.iterrows()}

tp_hits = B3[(B3.chop) & (B3.exit_reason == "GX_TP")].copy()
print("B3 CHOP 거래 중 +1.0%% 도달(GX_TP) : %d건" % len(tp_hits))

rows = []
for _, r in tp_hits.iterrows():
    sym = r.entry_symbol
    ent = pd.Timestamp(r.entry_time)
    hit = pd.Timestamp(r.exit_time)          # = +1.0% 최초 도달 틱
    up = (r.direction == "UP_RED")
    sg = 1.0 if up else -1.0
    q = ctx.quotes[sym]
    # 마지막 **완성봉** = 그 틱 이전에 완성된 봉 (bar_start + 3분 <= hit)
    j = int(np.searchsorted((bstart + pd.Timedelta(minutes=3)).values,
                            np.datetime64(hit.tz_convert("UTC").tz_localize(None)), side="right")) - 1
    if j < 2:
        continue
    # ── 1. +1% 도달 속도 (분)
    spd = (hit - ent).total_seconds() / 60.0
    # ── 2. 도달 전 pullback 깊이 = 진입~도달 구간 최저 net
    mins = int(spd)
    lows = []
    for mm in range(0, mins + 1):
        p = q.at(ent + pd.Timedelta(minutes=mm))
        if p is not None:
            lows.append((p - r.entry_price) / r.entry_price * 100.0)
    pull = float(min(lows)) if lows else np.nan
    # ── 3. higher-high(또는 lower-low) 연속성 — 완성봉 기준
    hh = 0
    for kk in range(j, max(1, j - 8), -1):
        if date[kk] != date[j]:
            break
        okhh = (high[kk] > high[kk - 1]) if up else (low[kk] < low[kk - 1])
        if okhh:
            hh += 1
        else:
            break
    # ── 4. 과열도 — VWAP/EMA20 이격을 변동성으로 표준화
    vz = (sg * (close[j] - vwap[j]) / close[j] * 100.0) / vol40[j] if vol40[j] and vol40[j] == vol40[j] else np.nan
    ez = (sg * (close[j] - e20[j]) / close[j] * 100.0) / vol40[j] if vol40[j] and vol40[j] == vol40[j] else np.nan
    # ── 5. 직전 반대 flag 이후 경과봉
    opp = Direction.DOWN_BLUE if up else Direction.UP_RED
    prev_opp = [x for x in flags if x <= j and date[x] == date[j] and flags[x] == opp]
    since_opp = (j - prev_opp[-1]) if prev_opp else 99
    # ── 6. 당일 누적 방향 정합 / 갭 정합
    cum = sg * (close[j] - dayopen[j]) / dayopen[j] * 100.0
    gapa = sg * gap_open[j]
    # ── 라벨
    mb = mfe_base.get(r.k, np.nan)
    rows.append(dict(date=r.date, dir=r.direction, entry=str(r.entry_time)[11:16],
                     hit=str(r.exit_time)[11:16],
                     속도분=round(spd, 1), pullback=round(pull, 3) if pull == pull else None,
                     연속HH=hh, VWAPz=round(vz, 2) if vz == vz else None,
                     EMA20z=round(ez, 2) if ez == ez else None,
                     반대flag경과봉=since_opp, 당일누적=round(cum, 3),
                     갭정합=round(gapa, 3),
                     BASE_MFE=round(mb, 3) if mb == mb else None,
                     BASE_net=round(net_base.get(r.k, np.nan), 3),
                     러너=(bool(mb >= 3) if mb == mb else None)))
F = pd.DataFrame(rows)
print("\n[TP 도달 거래 전량 + 경로형태 특징]")
print(F.to_string(index=False))
F.to_csv(HERE / "d1_pathshape.csv", index=False, encoding="utf-8-sig")

print("\n[러너 vs 비러너 대비]")
FE = ["속도분", "pullback", "연속HH", "VWAPz", "EMA20z", "반대flag경과봉", "당일누적", "갭정합"]
rr = F[F.러너 == True]
nn = F[F.러너 == False]
rows = []
for c in FE:
    a = pd.to_numeric(rr[c], errors="coerce")
    d2 = pd.to_numeric(nn[c], errors="coerce")
    if a.notna().sum() == 0 or d2.notna().sum() == 0:
        continue
    v = pd.to_numeric(F[c], errors="coerce")
    ok = v.notna()
    rk = v[ok].rank()
    n1 = int(F.러너[ok].fillna(False).astype(bool).sum())
    n0 = int(ok.sum()) - n1
    auc = ((rk[F.러너[ok].fillna(False).astype(bool)].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)
           if n1 and n0 else np.nan)
    rows.append(dict(feature=c, 러너평균=round(float(a.mean()), 3), 러너최소=round(float(a.min()), 3),
                     러너최대=round(float(a.max()), 3),
                     비러너평균=round(float(d2.mean()), 3),
                     비러너최소=round(float(d2.min()), 3), 비러너최대=round(float(d2.max()), 3),
                     차=round(float(a.mean() - d2.mean()), 3),
                     AUC=round(float(auc), 3) if auc == auc else None))
print(pd.DataFrame(rows).to_string(index=False))
print("\n  러너 %d건 / 비러너 %d건 (AUC 는 '러너=양성')" % (len(rr), len(nn)))
print("  * 표본이 %d건이라 AUC 표준오차가 매우 크다. 분리 가능성 탐색용이다." % len(F))

print("\n[완전분리 여부 — 단일 임계로 러너만 통과시킬 수 있는가]")
for c in FE:
    a = pd.to_numeric(rr[c], errors="coerce").dropna()
    d2 = pd.to_numeric(nn[c], errors="coerce").dropna()
    if not len(a) or not len(d2):
        continue
    if a.min() > d2.max():
        print("  %s : 러너 최소 %.3f > 비러너 최대 %.3f  → **완전분리(>)**" % (c, a.min(), d2.max()))
    elif a.max() < d2.min():
        print("  %s : 러너 최대 %.3f < 비러너 최소 %.3f  → **완전분리(<)**" % (c, a.max(), d2.min()))
    else:
        # 몇 건을 무손실로 걸러낼 수 있나 (러너 전건 보존 기준)
        lo = a.min()
        keep = int((d2 >= lo).sum())
        hi = a.max()
        keep2 = int((d2 <= hi).sum())
        print("  %-12s 겹침 — 러너전건보존 시 비러너 통과 %d/%d(>=) · %d/%d(<=)"
              % (c, keep, len(d2), keep2, len(d2)))
