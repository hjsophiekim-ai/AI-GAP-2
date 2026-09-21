# -*- coding: utf-8 -*-
"""weak regime 판정 — **production 함수만** 재사용. 미래정보 없음. READ-ONLY.

판정 시점은 **진입 시각**이고, 그 시각 **이전에 완성된 3분봉만** 쓴다.
+1.5% 도달보다 항상 앞선다(진입 <= 도달). 새 임계값을 만들지 않는다.

쓸 수 없는 것 (데이터 부재):
  KOSPI/KOSDAQ intraday, breadth(상승/하락 종목수) — data/cache 에 2026-07-10
  스냅샷 1건뿐이고 advancers/decliners 가 전부 None 이다. 78일 이력이 없으므로
  지수/breadth 기반 regime 은 이 저장소에서 백테스트할 수 없다.
"""
from __future__ import annotations
import sys, pickle
import pandas as pd

sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
from app.trading.macd2 import n1_adaptive as NA
from app.trading.macd2.models import Direction

_CTX = None


def ctx():
    global _CTX
    if _CTX is None:
        _CTX = pickle.load(open("_ctx_B.pkl", "rb"))
    return _CTX


def _bars_before(ts) -> pd.DataFrame:
    """ts **이전에 완성된** 하이닉스 3분봉만. (봉 시작 + 3분 <= ts)"""
    b = ctx()["hynix_bars_3m"]
    t = pd.Timestamp(ts)
    return b.loc[b["datetime"] + pd.Timedelta(minutes=3) <= t]


def n1_trend_ok(trade) -> bool:
    """production n1_adaptive.snapshot 그대로 — 보유방향 상위추세인가."""
    d = Direction.UP_RED if trade["direction"] == "UP_RED" else Direction.DOWN_BLUE
    return bool(NA.snapshot(_bars_before(trade["entry_time"]), d).ok)


# ── regime 후보 (전부 production feature, 새 임계값 0개) ──────────────────
def R1(trade) -> bool:
    """R1 OFF_TREND — N1 이 이미 봉마다 계산하는 상위추세가 아님.
    '지수'가 없으니 MACD2 가 보는 유일한 추세(하이닉스 EMA20/50)를 쓴다."""
    return not n1_trend_ok(trade)


def R2(trade) -> bool:
    """R2 CHOP — production early_take_profit.evaluate_entry_chop 결과.
    원장 entry_chop 컬럼이 그 값이다(진입 확정시점 계산, 미래정보 없음)."""
    return bool(trade["entry_chop"])


def R3(trade) -> bool:
    """R3 = R1 AND R2 — 추세도 아니고 진입봉도 횡보. 가장 보수적."""
    return R1(trade) and R2(trade)


def R4(trade) -> bool:
    """R4 = R1 OR R2 — 둘 중 하나라도 약하면 약세로 본다. 가장 넓음."""
    return R1(trade) or R2(trade)


REGIMES = {"R1 OFF_TREND": R1, "R2 CHOP": R2, "R3 R1&R2": R3, "R4 R1|R2": R4}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    import k1_core as K
    TS = K.load()
    D = ctx()["dates"]
    print(f"{'regime':16s} {'weak 거래':>8s} {'strong 거래':>10s} {'weak 비율':>9s} {'weak 일수':>9s}")
    print("-" * 62)
    for name, fn in REGIMES.items():
        w = [t for t in TS if fn(t)]
        wd = {t["date"] for t in w}
        print(f"{name:16s} {len(w):8d} {len(TS)-len(w):10d} {len(w)/len(TS)*100:8.1f}% "
              f"{len(wd):9d}")
    print(f"\n전체 거래 {len(TS)} / 거래일 {len({t['date'] for t in TS})} / 창 {len(D)}영업일")
    print(f"N1 상위추세 ok 거래 = {sum(1 for t in TS if n1_trend_ok(t))}")
