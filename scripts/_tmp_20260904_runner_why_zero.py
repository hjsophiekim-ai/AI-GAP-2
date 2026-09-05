"""READ-ONLY 진단 (2026-09-04): AFTERNOON RUNNER FILTER A/B/C 가 full-chain
에서 추가거래 0건인 이유를 후보별로 분해한다.

추가진입 게이트에 도달하려면 순서대로 전부 통과해야 한다:
  (1) time_window_filter.evaluate_time_window_entry (TW2 기본 T+3 게이트)
  (2) time_window_filter.evaluate_tw2_extra_vetoes  (VWAP 역행 veto + 최근크로스 veto)
  (3) resolve_slot 이 REJECT_DAILY_SLOT_CAP (= 그날 3슬롯 소진)
  (4) 그날 추가 1회 미사용
  (5) RUNNER FILTER 통과
+3% 러너 12건과 A/B/C 통과 후보 전체가 어느 단계에서 탈락하는지 센다.
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from app.trading.macd2 import config  # noqa: E402
from app.trading.macd2 import time_window_3slot as tw3  # noqa: E402
from app.trading.macd2 import time_window_filter as twf  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

import _tmp_20260904_runner_dataset as ds  # noqa: E402
import _tmp_20260904_runner_filters as rf  # noqa: E402

KST = config.KST
BASE = PROJECT_ROOT / "data" / "validation" / "afternoon_runner"


def main() -> int:
    ctx = ds.build_ctx(use_cache=True)
    pre = ds.precompute(ctx.hynix_bars_3m)
    bars = ctx.hynix_bars_3m
    df = pd.read_csv(BASE / "candidates.csv")

    # BASE 체인(현행)에서의 진입 목록 -> 슬롯 소진 시점
    base_trades = rf.run_chain(ctx, pre, gate=None)
    by_day: dict = {}
    for t in base_trades:
        by_day.setdefault(t.date, []).append(t)

    L: list[str] = []
    add = L.append

    def stage_of(row) -> tuple[str, str]:
        i = int(row["decision_idx"])
        direction = (Direction.UP_RED if row["direction"] == "UP_RED"
                     else Direction.DOWN_BLUE)
        fidx = int(row["flag_idx"])
        flag_bar_dt = pd.Timestamp(bars["datetime"].iloc[fidx]).to_pydatetime()
        bar_close_at = pd.Timestamp(bars["datetime"].iloc[i]).to_pydatetime() + timedelta(minutes=3)
        sl = bars.iloc[: i + 1]

        # 그 시점 BASE 체인 상태
        day_trades = by_day.get(row["date"], [])
        slots_before = sum(1 for t in day_trades if t.decision_idx < i)
        held = None
        for t in day_trades:
            if t.entry_bar_idx is not None and t.entry_bar_idx <= i:
                ex = t.exit_bar_idx if hasattr(t, "exit_bar_idx") else None
                if ex is None or i < ex:
                    held = t
        pos_dir = None
        if held is not None and held.entry_bar_idx < i:
            pos_dir = (Direction.UP_RED if held.direction == "UP_RED" else Direction.DOWN_BLUE)

        dec = twf.evaluate_time_window_entry(
            sl, direction, flag_bar_dt, bar_close_at,
            position_direction=pos_dir,
            morning_entry_count=0, afternoon_entry_count=0, daily_entry_count=0)
        if not dec.approved:
            return "1_TW2_기본_탈락", str(dec.block_reason)
        vetoed, vr = twf.evaluate_tw2_extra_vetoes(sl, direction, flag_bar_dt, bar_close_at)
        if vetoed:
            return "2_TW2_추가veto_탈락", str(vr)
        sd = tw3.resolve_slot(now=bar_close_at, slots_used_today=slots_before,
                              morning_count=0, afternoon_count=0,
                              direction=direction, is_flat=(pos_dir is None))
        if sd.slot_allowed:
            return "3_슬롯_미소진(정규진입_경로)", f"slot_number={sd.slot_number}"
        if sd.reject_reason != config.TW2_3SLOT_REJECT_SLOT_CAP:
            return "3_슬롯_기타거절", str(sd.reject_reason)
        return "4_추가게이트_도달", "OK"

    add("=" * 100)
    add("[I] +3% 러너 12건 — 실제 체인에서 어느 단계에서 막히는가")
    add("=" * 100)
    runners = df[df["label"].isin(["BIG_WIN", "SUPER_WIN"])].sort_values(
        "net_pct", ascending=False)
    cnt = Counter()
    for _, r in runners.iterrows():
        stage, why = stage_of(r)
        cnt[stage] += 1
        passed = [k for k, fn in rf.FILTERS.items() if fn(r.to_dict())]
        add(f"  {r['date']} {r['decision_at'][11:16]} {r['direction']:<10} "
            f"{r['net_pct']:+.3f}%  통과필터={','.join(passed) or '(없음)':<8} "
            f"-> {stage}  [{why}]")
    add("")
    add("  단계별 집계: " + ", ".join(f"{k}={v}" for k, v in sorted(cnt.items())))
    add("")

    add("=" * 100)
    add("[J] RUNNER FILTER A 통과 후보 전체(93건) — 단계별 탈락 분포")
    add("=" * 100)
    for fname in ("A", "B", "C", "S"):
        fn = rf.FILTERS[fname]
        sel = df[df.apply(lambda r: fn(r.to_dict()), axis=1)]
        c = Counter()
        detail = Counter()
        for _, r in sel.iterrows():
            stage, why = stage_of(r)
            c[stage] += 1
            if stage.startswith(("1_", "2_")):
                detail[why] += 1
        add(f"-- 필터 {fname}: 통과후보 {len(sel)}건")
        for k, v in sorted(c.items()):
            add(f"     {k:<30} {v:>4}건")
        if detail:
            add("     탈락사유 상세: " + ", ".join(f"{k}={v}" for k, v in detail.most_common(8)))
        add("")

    out = BASE / "why_zero.txt"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
