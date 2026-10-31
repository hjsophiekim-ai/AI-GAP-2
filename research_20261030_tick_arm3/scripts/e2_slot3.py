"""사전테스트 — 슬롯 상한으로 막힌 '3번째 후보'의 품질 (재생 불필요).

S3(슬롯 2→3 + 건당 예산 축소)는 **막혔던 3번째 후보의 품질이 좋아야** 성립한다.
34회 재생을 돌리기 전에 그 전제를 싸게 검증한다.

두 단계:
  ① 그 후보가 슬롯을 통과했다면 **TEG / CHOP TEG / TW2 veto** 를 통과했을까?
     → production 함수를 판정시점 완성봉으로 직접 호출한다(재생 불필요).
  ② 통과했다면 그 시각 진입해서 어떻게 됐을까?
     → 실제 1초 틱으로 진입가(ask1)부터 장 끝까지 경로를 잰다.
        production N1 손절 -1.30% 도달이 먼저인지, +1.5%/+3% 도달이 먼저인지.

    python .../e2_slot3.py --tag CHOPX_A
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import a3_peaks as P  # noqa: E402
from c1_vwapcand import hy_frame  # noqa: E402
from app.trading.macd2 import config, early_take_profit as ETP  # noqa: E402
from app.trading.macd2 import signal_engine as SE, teg_gate as TEG  # noqa: E402
from app.trading.macd2 import time_window_filter as TWF  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

SP = Path(os.environ.get("REPLAY_SP", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/b3t_sp"))
KST = config.KST
DAYS = ["20261016", "20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]
STOP = -1.30          # config.X2LITE_MORNING_STOP_LOSS (N1 계열 오전 손절)
ETF_TICK = 5.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="CHOPX_A")
    ap.add_argument("--days", nargs="+", default=DAYS)
    a = ap.parse_args()
    comp = sorted({f.stem.split("_")[1] for f in (SP / "data").glob("replay_*_hynix_1m.csv")})

    cands = []
    for d in a.days:
        p = P.OUT / f"{a.tag}_{d}.json"
        if not p.exists():
            continue
        j = json.loads(p.read_text(encoding="utf-8"))
        g = [x for x in j.get("gates", []) if x["t"][:10].replace("-", "") == d]
        seen = set()
        for x in g:
            # 하루 슬롯 상한에 **실제로** 걸린 것만 (used == cap)
            if x["ev"] != "SLOT" or x.get("allowed"):
                continue
            if x.get("reject") != "TW2_3SLOT_REJECT_DAILY_SLOT_CAP":
                continue
            if x.get("cap") is None or x.get("used") != x.get("cap"):
                continue
            k = (x["t"], x.get("dir"))
            if k in seen:
                continue
            seen.add(k)
            cands.append((d, x["t"], x.get("dir"), x.get("used"), x.get("cap")))

    print(f"\n{'='*150}")
    print(f"[{a.tag}] 슬롯 상한으로 막힌 3번째 후보 — 게이트 통과 여부 + 진입했다면의 경로")
    print(f"{'='*150}")
    print(f"{'날짜':8s}{'판정':9s}{'방향':10s}{'TEG':6s}{'TEG사유':34s}{'TW2veto':9s}"
          f"{'진입가':>8s}{'틱최고%':>8s}{'틱최저%':>8s}{'먼저':8s}{'+1.5%':6s}{'+3%':5s}{'종가%':>7s}")
    ok_gate = 0
    good = bad = 0
    for d, ts, dr, used, cap in cands:
        t = datetime.fromisoformat(ts)
        dirn = Direction.UP_RED if dr == "UP_RED" else Direction.DOWN_BLUE
        sym = config.LONG_SYMBOL if dirn == Direction.UP_RED else config.INVERSE_SYMBOL
        i = comp.index(d)
        hy = hy_frame([comp[i - 2], comp[i - 1], d])
        df1 = hy[hy["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(t)].reset_index(drop=True)
        bars = SE.resample_completed_3m(df1, now=t)
        # 플래그봉 = 판정시각 -6분 (T+3 판정 관례)
        fb = (t - timedelta(minutes=6)).replace(second=0, microsecond=0)
        try:
            teg = TEG.evaluate_teg(bars, dirn, fb, t)
            teg_ok, teg_rs = bool(teg.approved), list(teg.reject_reasons or [])
        except Exception as exc:
            teg_ok, teg_rs = False, [f"ERR:{exc}"[:30]]
        try:
            v, vr = TWF.evaluate_tw2_extra_vetoes(bars, dirn, fb, t)
        except Exception:
            v, vr = False, None
        gate_pass = teg_ok and not v
        ok_gate += gate_pass

        # 진입했다면: 진입가 = 그 시각 ask1(실틱), 이후 장 끝까지 경로
        day = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        tk = P.ticks(sym, day)
        px_in = hi = lo = cl = float("nan")
        first = "-"
        r15 = r30 = "."
        if tk is not None:
            nv = pd.Timestamp(t).tz_localize(None)
            row = tk[tk.index <= nv]
            if len(row):
                last = row.iloc[-1]
                px_in = (float(last["best_ask"]) if pd.notna(last.get("best_ask"))
                         else float(last["price"]) + ETF_TICK)
                w = tk[(tk.index > nv) & (tk.index <= pd.Timestamp(f"{day} 15:00:00"))]
                if len(w) and px_in > 0:
                    # 수수료 모형은 보수적으로 생략 — 상대비교용 총수익률
                    hi = (float(w["price"].max()) / px_in - 1) * 100
                    lo = (float(w["price"].min()) / px_in - 1) * 100
                    bid = w["best_bid"].dropna()
                    cl = (float(bid.iloc[-1]) / px_in - 1) * 100 if len(bid) else float("nan")
                    # 손절(-1.30%) 과 +1.5% 중 무엇이 먼저인가 — 틱 순서로
                    s = (w["price"] / px_in - 1) * 100
                    t_stop = s[s <= STOP].index.min() if (s <= STOP).any() else None
                    t_15 = s[s >= 1.5].index.min() if (s >= 1.5).any() else None
                    t_30 = s[s >= 3.0].index.min() if (s >= 3.0).any() else None
                    r15 = "O" if t_15 is not None else "."
                    r30 = "O" if t_30 is not None else "."
                    if t_stop is None and t_15 is None:
                        first = "무출현"
                    elif t_stop is None:
                        first = "+1.5%"
                    elif t_15 is None:
                        first = "손절"
                    else:
                        first = "+1.5%" if t_15 < t_stop else "손절"
        if gate_pass:
            if first == "+1.5%":
                good += 1
            elif first == "손절":
                bad += 1
        print(f"{d[4:6]+'/'+d[6:]:8s}{ts[11:19]:9s}{str(dr):10s}"
              f"{('통과' if teg_ok else '거절'):6s}{(','.join(teg_rs))[:34]:34s}"
              f"{('veto' if v else 'ok'):9s}{px_in:>8,.0f}{hi:>8.2f}{lo:>8.2f}{first:8s}"
              f"{r15:6s}{r30:5s}{cl:>7.2f}")

    print(f"\n후보 {len(cands)}건 · 게이트(TEG∧TW2veto) 통과 **{ok_gate}건**")
    print(f"그 중 진입했다면: +1.5% 선도달 {good}건 / 손절(-1.30%) 선도달 {bad}건")
    print("\n주1: 슬롯 판정은 TEG 보다 **앞**이라 이 후보들은 TEG 를 거치지 않았다 — 여기서 처음 평가한다.")
    print("주2: 진입가는 판정시각의 실제 ask1, 수익률은 수수료 미반영 총수익률(상대비교용)이다.")
    print("주3: 3번째 슬롯은 **그 날 이미 2건이 돌고 있는 상태**라, 같은 방향이면 추가진입이 막히고")
    print("     반대 방향이면 기존 포지션 전환이 일어난다 — 이 근사는 그 상호작용을 반영하지 않는다.")


if __name__ == "__main__":
    main()
