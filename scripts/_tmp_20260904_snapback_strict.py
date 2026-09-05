"""READ-ONLY full-chain backtest (2026-09-04 사용자 요청 #5): 현행(A) 대비
**초강 SNAPBACK** 추가 1회(B).

  A. 현행 — TW2 3-SLOT + 조기익절 필터, PRE15 OFF, 하루 최대 3회
  B. A + 초강 SNAPBACK 추가 1회
       - 13:00 <= T+3 판정시각 < 14:50
       - 그날 3슬롯(config.TW2_3SLOT_DAILY_CAP) 소진 후, 하루 1회만
       - 기존 TW2/TEGv2 오후 시간창 제한 미적용
       - 아래 4조건 **전부(4/4)** 필수:
           C1  day_extreme_margin_pct <= -4.0
           C2  price_vs_vwap_pct      <= -1.0
           C3  ema20_slope_pct        <= 0
           C4  반대방향 MACD crossover 가 T+3 에서 유지될 뿐 아니라
               MACD gap 이 flag 시점보다 **확대**
       - 청산은 기존 TP1/TP2/trailing/SL/반대신호/whipsaw/조기익절 그대로
       - 하루 실제 총 신규진입 최대 4회

■ 직전 테스트(3/4, C4=유지만) 대비 바뀐 것은 딱 두 가지뿐이다.
    MIN_CONDITIONS  3 -> 4
    C4  gap_signed > 0        ->   gap_signed > 0  AND  gap_exp1 > 0
  나머지 임계값(-4.0 / -1.0 / 0 / 창 13:00~14:50)은 그대로 고정, 재튜닝 없음.

■ C4 가 production 판정식과 동일함
    production evaluate_time_window_entry 는
      gap_now <= 0            -> TW_REJECT_NOT_CONFIRMED
      not (gap_now > gap_flag)-> TW_REJECT_MACD_GAP_NOT_EXPANDING
    (gap = (MACD - Signal) * direction_sign, gap_flag = 플래그봉, gap_now = T+3봉)
    본 스크립트의 C4 = (gap_signed > 0) and (gap_exp1 > 0) 이고
      gap_signed = gap_now,  gap_exp1 = gap_now - gap_flag
    이므로 두 reject 를 동시에 면한 경우와 정확히 동치다. main() 에서 승인된
    SNAPBACK 전 건에 대해 production 함수를 실제 호출해 block_reason 이
    그 둘이 아님을 assert 로 확인한다.

■ 체인은 검증된 _tmp_20260904_snapback_fullchain.run_chain 을 그대로 재사용하고
  (A 재현이 직전 30일 검증엔진과 70거래 완전일치함이 이미 확인됨), 모듈 전역
  snapback_conditions / MIN_CONDITIONS 만 교체한다. production 코드는 무수정.
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from app.trading.macd2 import config  # noqa: E402
from app.trading.macd2 import time_window_filter as twf  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

import _tmp_20260904_runner_dataset as ds  # noqa: E402
import _tmp_20260904_snapback_fullchain as sb  # noqa: E402
import _tmp_20260904_snapback_report as rep  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "snapback_strict"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def snapback_conditions_strict(f: dict) -> dict:
    """4조건. C4 만 '유지 + gap 확대'로 강화, 나머지 임계값은 동일."""
    e = f.get("day_extreme_margin_pct")
    v = f.get("price_vs_vwap_pct")
    s = f.get("ema20_slope_pct")
    g = f.get("gap_signed")      # = gap_now (부호 적용)
    x = f.get("gap_exp1")        # = gap_now - gap_flag (부호 적용)
    return {
        "C1_day_extreme": bool(e is not None and e <= sb.THR_EXTREME),
        "C2_vwap": bool(v is not None and v <= sb.THR_VWAP),
        "C3_ema20_slope": bool(s is not None and s <= sb.THR_SLOPE),
        "C4_hold_and_expand": bool(g is not None and g > 0
                                   and x is not None and x > 0),
    }


def main() -> int:
    # ── 조건 교체 (임계값은 그대로) ──
    sb.snapback_conditions = snapback_conditions_strict
    sb.MIN_CONDITIONS = 4

    ctx = ds.build_ctx(use_cache=True)
    pre = ds.precompute(ctx.hynix_bars_3m)
    bars = ctx.hynix_bars_3m
    dates60 = ctx.dates
    dates30 = dates60[-30:]
    print(f"ctx {dates60[0]}~{dates60[-1]} (60일), 평가창 30일 {dates30[0]}~{dates30[-1]}")
    print(f"조건: 4/4 필수, C4 = T+3 유지 AND gap 확대 "
          f"(임계값 {sb.THR_EXTREME}/{sb.THR_VWAP}/{sb.THR_SLOPE}, "
          f"창 {sb.SNAPBACK_START}~{sb.SNAPBACK_END})")

    snaps: list = []
    trades_a = sb.run_chain(ctx, pre, snapback=False)
    trades_b = sb.run_chain(ctx, pre, snapback=True, snaps_out=snaps)

    # ── A 재현 검증 ──
    prev = PROJECT_ROOT / "data" / "validation" / "slot23_tq_fullchain" / "raw.json"
    if prev.exists():
        old = json.loads(prev.read_text(encoding="utf-8"))["A"]
        def k(t):
            return (t["date"], t["entry_time"], t["direction"],
                    round(t["net_pct"], 6), t["exit_reason"])
        o = {k(t) for t in old}
        n = {k(vars(t)) for t in trades_a if t.date in set(dates30)}
        print(f"A 재현 검증(30일, 직전 검증엔진과 동일): {o == n} [old {len(o)} / new {len(n)}]")

    # ── C4 가 production 판정과 동치인지 승인건 전수 검증 ──
    bad = []
    checked = 0
    for t in trades_b:
        if not t.is_snapback:
            continue
        i = t.decision_idx
        fidx = i - 1
        direction = Direction.UP_RED if t.direction == "UP_RED" else Direction.DOWN_BLUE
        flag_bar_dt = pd.Timestamp(bars["datetime"].iloc[fidx]).to_pydatetime()
        decision_at = pd.Timestamp(bars["datetime"].iloc[i]).to_pydatetime() + timedelta(minutes=3)
        dec = twf.evaluate_time_window_entry(
            bars.iloc[: i + 1], direction, flag_bar_dt, decision_at,
            position_direction=None,
            morning_entry_count=0, afternoon_entry_count=0, daily_entry_count=0)
        checked += 1
        if dec.block_reason in (config.TW_REJECT_NOT_CONFIRMED,
                                config.TW_REJECT_MACD_GAP_NOT_EXPANDING):
            bad.append((t.date, t.entry_time, dec.block_reason))
    print(f"C4 동치 검증: 승인 SNAPBACK {checked}건 중 "
          f"production NOT_CONFIRMED/GAP_NOT_EXPANDING 해당 {len(bad)}건 "
          f"(0이어야 정상)")
    assert not bad, bad

    n30 = [t for t in trades_b if t.date in set(dates30)]
    s30 = [t for t in n30 if t.is_snapback]
    print(f"30일: A {len([t for t in trades_a if t.date in set(dates30)])}건 / "
          f"B {len(n30)}건 (SNAPBACK {len(s30)}건)")
    print(f"60일: A {len(trades_a)} / B {len(trades_b)} "
          f"(SNAP {sum(1 for t in trades_b if t.is_snapback)})")
    c30 = [s for s in snaps if s.date in set(dates30)]
    print(f"SNAPBACK 후보 도달: 60일 {len(snaps)}건 / 30일 {len(c30)}건, "
          f"승인 30일 {sum(1 for s in c30 if s.approved)}건")

    out = {
        "dates60": dates60, "dates30": dates30,
        "thresholds": {"window": [str(sb.SNAPBACK_START), str(sb.SNAPBACK_END)],
                       "THR_EXTREME": sb.THR_EXTREME, "THR_VWAP": sb.THR_VWAP,
                       "THR_SLOPE": sb.THR_SLOPE, "MIN_CONDITIONS": 4,
                       "C4": "T+3 유지 AND gap 확대"},
        "A": [vars(t) for t in trades_a],
        "B": [vars(t) for t in trades_b],
        "B_TW2KEEP": [vars(t) for t in trades_b],   # 이번엔 민감도 변형 없음
        "snap_candidates_B": [vars(s) for s in snaps],
        "snap_candidates_K": [],
    }
    (OUTPUT_DIR / "raw.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ── 리포트 (기존 리포터 재사용, 출력 경로만 교체) ──
    rep.BASE = OUTPUT_DIR
    rep.RAW = OUTPUT_DIR / "raw.json"
    rep.VARIANTS = ["A", "B"]
    rep.LABEL = {"A": "A 현행 (TW2 3-SLOT + 조기익절)",
                 "B": "B A + 초강 SNAPBACK 추가 1회 (4/4)"}
    rep.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
