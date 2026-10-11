"""K1: 'B3 의 +1% 전량익절을 치우고 20분 Y3 판정에 맡기면 N1 으로 넘어가는가'. READ-ONLY.

Y3 조건은 production 함수를 그대로 호출한다:
  p3_stack.macd_gap_expanding(bars_3m, now, direction)   하이닉스 마지막 완성봉 hist 확대
  p3_stack.etf_following(prev, cur)                      보유 ETF 가 3분 전보다 높은가
  net > 0
"""
import glob, json, os, sys
from datetime import timedelta
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
sys.path.insert(0, REPO)
ROOT = REPO + "/research_20261004_chop_staged"
DATA = ROOT + "/wk/data"; TD = REPO + "/data/cache"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
OUT = os.path.dirname(os.path.abspath(__file__))
KST = "Asia/Seoul"; LONG = "0193T0"; DRAG = 0.05
from app.trading.macd2 import p3_stack, config
from app.trading.macd2.signal_engine import resample_completed_3m, exclude_preopen_padding_1m
from app.trading.macd2.models import Direction
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}; exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
ALLD = sorted({os.path.basename(p)[7:15] for p in glob.glob(DATA + "/replay_*_hynix_1m.csv")}
              | {os.path.basename(p)[7:15] for p in glob.glob(TD + "/replay_*_hynix_1m.csv")})
_c = {}


def one(day, tag):
    k = (day, tag)
    if k not in _c:
        r = None
        for sd in (DATA, TD):
            f = f"{sd}/replay_{day}_{tag}_1m.csv"
            if os.path.exists(f):
                df = pd.read_csv(f); dt = pd.to_datetime(df["datetime"])
                df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
                r = df.sort_values("datetime").reset_index(drop=True); break
        _c[k] = r
    return _c[k]


def hist3(day, nprev=3):
    i = ALLD.index(day)
    parts = [one(d, "hynix") for d in ALLD[max(0, i - nprev):i + 1]]
    parts = [p for p in parts if p is not None]
    return pd.concat(parts, ignore_index=True) if parts else None


def etf_px(day, sym, when):
    """그 시각의 ETF 가격 = 직전 완성 1분봉 종가 (하네스 OLD 규약과 같은 보수적 근사)."""
    e = one(day, "long" if sym == LONG else "inverse")
    if e is None:
        return None
    ts = pd.DatetimeIndex(e["datetime"])
    i = int(ts.searchsorted(when)) - 1
    return float(e["close"].iloc[i]) if 0 <= i < len(e) else None


def y3_at(day, entry, direction, px0, sym, minutes=20.0):
    now = entry + timedelta(minutes=minutes)
    h = hist3(day)
    if h is None:
        return None
    clean, _ = exclude_preopen_padding_1m(h)
    b3 = resample_completed_3m(clean, now=now.to_pydatetime())
    dd = Direction.UP_RED if direction.startswith("UP") else Direction.DOWN_BLUE
    gap_ok, gap_tag = p3_stack.macd_gap_expanding(b3, now, dd)
    cur = etf_px(day, sym, now)
    prv = etf_px(day, sym, now - timedelta(minutes=3))
    etf_ok, etf_tag = p3_stack.etf_following(prv, cur)
    net = ((cur - px0) / px0 * 100 - DRAG) if cur else None
    net_ok = net is not None and net > 0.0
    return dict(now=now, net=net, net_ok=net_ok, gap_ok=gap_ok, etf_ok=etf_ok,
                promote=bool(net_ok and gap_ok and etf_ok), gap=gap_tag, etf=etf_tag)


rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    for t in trades(json.load(open(ap(d), encoding="utf-8"))):
        if t.get("regime") != "CHOP":
            continue
        sym = LONG if t["dir"].startswith("UP") else "0197X0"
        r = y3_at(d, t["entry"], t["dir"], t["px_in"], sym)
        if r is None:
            continue
        rows.append(dict(day=d, at=t["entry"].strftime("%H:%M"), dir=t["dir"],
                         rs="+".join(dict.fromkeys(t["reasons"])), krw=t["krw"],
                         hold=t["hold_min"], **{k: v for k, v in r.items() if k != "now"}))
T = pd.DataFrame(rows)
T.to_csv(OUT + "/y3.csv", index=False, encoding="utf-8")
print(f"# CHOP 진입 {len(T)}건 — 진입 +20분 시점 Y3 판정 (production 함수)")
print(f"  승격 {int(T.promote.sum())}건 / 미승격 {int((~T.promote).sum())}")
print("\n## 조건별 통과율")
for c in ("net_ok", "gap_ok", "etf_ok"):
    print(f"   {c:8s} {T[c].mean()*100:5.1f}%  ({int(T[c].sum())}/{len(T)})")
print("\n## 실제 청산사유 x Y3 판정")
T["b3tp"] = T.rs.str.contains("B3_TP")
print(pd.crosstab(T.rs.str.slice(0, 26), T.promote).to_string())
print("\n## B3_TP(+1% 전량익절)로 끝난 거래만 — Y3 가 살렸을까")
S = T[T.b3tp]
print(f"   {len(S)}건 중 20분 Y3 승격 {int(S.promote.sum())}건")
print("| 일자 | 진입 | 방향 | 20분 net | net>0 | gap확대 | ETF추종 | **승격** | 실제 |")
print("|---|---|---|---|---|---|---|---|---|")
for _, r in S.sort_values("day").iterrows():
    print(f"| {r.day} | {r['at']} | {r['dir']} | {r.net:+.2f}% | {'O' if r.net_ok else 'X'} | "
          f"{'O' if r.gap_ok else 'X'} | {'O' if r.etf_ok else 'X'} | "
          f"**{'승격' if r.promote else '청산'}** | {r.krw:+,.0f} |")

# ── 오늘 ──
ent = pd.Timestamp("2026-10-07 10:03", tz=KST)
e7 = one("20261007", "inverse")
px0 = float(e7["open"].iloc[int(pd.DatetimeIndex(e7["datetime"]).searchsorted(ent))])
print(f"\n## 오늘 10/07 (진입 10:03 @ {px0:,.0f}, DOWN_BLUE 인버스)")
for m in (20, 25, 30):
    r = y3_at("20261007", ent, "DN 인버", px0, "0197X0", minutes=m)
    print(f"   +{m}분 ({r['now'].strftime('%H:%M')}): net {r['net']:+.2f}% / net>0 {'O' if r['net_ok'] else 'X'} "
          f"/ gap확대 {'O' if r['gap_ok'] else 'X'} ({r['gap']}) / ETF추종 {'O' if r['etf_ok'] else 'X'} ({r['etf']}) "
          f"-> **{'Y3 승격 (N1 래더로)' if r['promote'] else 'B3_MAXHOLD 청산'}**")
