"""READ-ONLY 1/2 (2026-09-04): 현행 TW2 3-SLOT + 조기익절 필터의 최근 60영업일
**실제 체결 거래**에 진입확정시점 피처를 붙여 반복 손실의 공통 특성을 찾는다.

  BIG_WIN  : net >= +3%
  WIN      : 0% < net < +3%
  LOSS     : net <= 0%          (BIG_LOSS 를 포함하는 상위 집합 — 사용자 정의 그대로)
  BIG_LOSS : net <= -1%

피처는 전부 **T+3 확정판정봉(진입 결정 봉) 이하 인덱스만** 참조한다(미래정보 없음).
계산은 연구 3의 _tmp_20260904_runner_dataset.build_features 를 재사용하고,
이번 요청에서 추가된 세 가지를 덧붙인다:
  - 최근 30분 range 내 위치 (진입방향 기준 0=역방향 극단 ~ 1=순방향 극단)
  - Trend Quality 5개 세부조건 (tw3.evaluate_trend_quality().conditions)
  - TEGv2 7개 세부조건        (teg_gate.evaluate_teg().conditions)
슬롯번호/세션은 체인이 실제로 부여한 값을 그대로 쓴다.

A 체인은 검증된 _tmp_20260904_teg4th_fullchain.run(extra_teg_entry=False)
(= 현행 production 경로) 을 그대로 호출한다. production 코드 무수정.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from app.trading.macd2 import teg_gate  # noqa: E402
from app.trading.macd2 import time_window_3slot as tw3  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

import _tmp_20260904_runner_dataset as ds  # noqa: E402
import _tmp_20260904_teg4th_fullchain as teg4  # noqa: E402

OUT = PROJECT_ROOT / "data" / "validation" / "lossveto"
OUT.mkdir(parents=True, exist_ok=True)
N_TRAIN = 40


def label_of(net: float) -> str:
    if net >= 3.0:
        return "BIG_WIN"
    if net > 0.0:
        return "WIN"
    if net <= -1.0:
        return "BIG_LOSS"
    return "SMALL_LOSS"          # -1% < net <= 0%  (LOSS = SMALL_LOSS + BIG_LOSS)


def extra_features(bars, pre, i, direction, bars_slice, flag_bar_dt, decision_at) -> dict:
    """이번 요청에서 추가된 피처. 전부 인덱스 <= i 만 참조."""
    f: dict = {}
    sign = 1.0 if direction == Direction.UP_RED else -1.0
    c = pre.close[i]

    # 최근 30분(직전 10봉) range 내 위치 — 진입방향 기준
    lo_i = max(i - ds.WIN30_BARS, pre.first_idx[i])
    if i - lo_i >= 3:
        hi30 = max(pre.high[lo_i:i])
        lo30 = min(pre.low[lo_i:i])
        rng = hi30 - lo30
        if rng > 0:
            pos = (c - lo30) / rng
            f["range30_pos"] = round(pos if sign > 0 else (1.0 - pos), 6)
        else:
            f["range30_pos"] = None
    else:
        f["range30_pos"] = None

    # Trend Quality 5개 세부조건
    q = tw3.evaluate_trend_quality(bars_slice, direction)
    for cond in tw3.ALL_QUALITY_CONDITIONS:
        f[f"tq_{cond}"] = 1 if q.conditions.get(cond, False) else 0
    f["tq_passed"] = int(q.passed_count)

    # TEGv2 7개 세부조건
    t = teg_gate.evaluate_teg(bars_slice, direction, flag_bar_dt, decision_at)
    for cond in teg_gate.ALL_CONDITIONS:
        f[f"teg_{cond}"] = 1 if t.conditions.get(cond, False) else 0
    f["teg_passed_n"] = int(sum(1 for cnd in teg_gate.ALL_CONDITIONS
                                if t.conditions.get(cnd, False)))
    f["teg_approved_flag"] = 1 if t.approved else 0
    return f


def main() -> int:
    ctx = ds.build_ctx(use_cache=True)
    pre = ds.precompute(ctx.hynix_bars_3m)
    bars = ctx.hynix_bars_3m
    dates = ctx.dates
    train = set(dates[:N_TRAIN])
    print(f"ctx {dates[0]}~{dates[-1]} (60일)  TRAIN {dates[0]}~{dates[N_TRAIN-1]} / "
          f"OOS {dates[N_TRAIN]}~{dates[-1]}")

    trades = teg4.run(ctx, extra_teg_entry=False)
    print(f"현행 A 실거래 {len(trades)}건")

    rows = []
    for t in trades:
        i = t.decision_idx
        fidx = i - 1
        direction = Direction.UP_RED if t.direction == "UP_RED" else Direction.DOWN_BLUE
        flag_bar_dt = pd.Timestamp(bars["datetime"].iloc[fidx]).to_pydatetime()
        decision_at = pd.Timestamp(t.entry_time)
        bars_slice = bars.iloc[: i + 1]
        base = ds.build_features(bars, pre, i, direction, decision_at.to_pydatetime(),
                                 bars_slice, flag_bar_dt)
        extra = extra_features(bars, pre, i, direction, bars_slice, flag_bar_dt,
                               decision_at.to_pydatetime())
        rows.append({
            "date": t.date,
            "split": "TRAIN" if t.date in train else "OOS",
            "decision_idx": i,
            "direction": t.direction,
            "entry_time": t.entry_time,
            "exit_time": t.exit_time,
            "exit_reason": t.exit_reason,
            "slot_number": t.slot_number,
            "session": t.session,
            "is_slot1": 1 if t.slot_number == 1 else 0,
            "is_slot2": 1 if t.slot_number == 2 else 0,
            "is_slot3": 1 if t.slot_number == 3 else 0,
            "is_morning": 1 if t.session == "MORNING" else 0,
            "entry_chop": 1 if t.entry_chop else 0,
            "net_pct": t.net_pct,
            "peak_net_pct": t.peak_net_pct,
            "trough_net_pct": t.trough_net_pct,
            "hold_bars": t.hold_bars,
            "label": label_of(t.net_pct),
            **base, **extra,
        })

    df = pd.DataFrame(rows)
    df.to_csv(OUT / "trades_features.csv", index=False, encoding="utf-8-sig")

    print("\n라벨 분포 (BIG_LOSS 는 LOSS 의 부분집합):")
    for split in ("TRAIN", "OOS", "ALL"):
        d = df if split == "ALL" else df[df.split == split]
        n = len(d)
        bw = int((d.label == "BIG_WIN").sum())
        w = int((d.label == "WIN").sum())
        sl = int((d.label == "SMALL_LOSS").sum())
        bl = int((d.label == "BIG_LOSS").sum())
        print(f"  {split:<6} n={n:<4} BIG_WIN={bw:<3} WIN={w:<3} "
              f"LOSS={sl+bl:<3}(그중 BIG_LOSS={bl:<3}) "
              f"단순합={d.net_pct.sum():+.2f}% 평균={d.net_pct.mean():+.4f}%")
    print(f"\nsaved {OUT / 'trades_features.csv'}  (컬럼 {len(df.columns)}개)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
