"""READ-ONLY analysis script - MOCK 계좌 전용, 시세/분봉 조회만 수행.

주문/체결/포지션 변경 API를 절대 호출하지 않는다 (broker/order_executor/
worker.run_once/ledger 모듈은 import조차 하지 않는다). 목적:
  1) 오늘(현재 시각까지) 실제 000660 1분봉을 KIS에서 조회해 완성된 3분봉으로
     재구성하고, 현재(수정된) 코드의 confirmed color-flag 로직으로 오늘 실제
     몇 개의 플래그가 언제 떴는지 재현한다.
  2) 강한 플래그(MAJOR FILTER) OFF 시나리오(오늘 오전처럼 전부 진입)와,
     하루 종일 ON이었다면 major_flag_filter가 각 플래그를 승인/거부했을지
     시뮬레이션해 거래 횟수를 비교한다.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.macd2 import config, major_flag_filter  # noqa: E402
from app.trading.macd2.market_data import MarketDataService, filter_complete_3m_bars  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402
from app.trading.macd2.signal_engine import (  # noqa: E402
    calculate_macd,
    confirmed_macd_flag_condition,
    resample_completed_3m,
)
from app.trading.macd2 import worker  # noqa: E402
from app.trading.macd2.signal_engine import ConfirmedColorFlagResult  # noqa: E402

KST = config.KST


# ---------------------------------------------------------------------------
# PRE-FIX (before commit 4747db4, this morning 10:19) color-onset logic,
# reconstructed from that commit's diff, to reproduce what the LIVE bot
# actually judged this morning/early afternoon before the fix landed.
# ---------------------------------------------------------------------------
def old_color_publication_regime(macd_snapshot, raw_color):
    if raw_color == Direction.UP_RED:
        previous_diff = macd_snapshot.previous_diff
        if macd_snapshot.hist > 0 and previous_diff is not None and previous_diff <= 0:
            return "POSITIVE_BREAKOUT_RED", 1
        if macd_snapshot.hist < 0 and macd_snapshot.macd < 0 and macd_snapshot.signal < 0:
            return "NEGATIVE_REGIME_RED", 3
        return None, 0
    if raw_color == Direction.DOWN_BLUE:
        if macd_snapshot.hist > 0 and macd_snapshot.macd > 0 and macd_snapshot.signal > 0:
            return "POSITIVE_REGIME_BLUE", 3
        return None, 0
    return None, 0


def old_evaluate_confirmed_macd_color_onset(
    macd_snapshot, previous_color_state, pending_direction=None, pending_count=0,
    *, previous_regime=None, publishable=True,
):
    raw_color = confirmed_macd_flag_condition(macd_snapshot)
    regime, required_count = old_color_publication_regime(macd_snapshot, raw_color)
    if (
        raw_color != Direction.HOLD
        and regime is None
        and (
            previous_color_state is None
            or previous_regime == "RAW_DIRECT"
            or (raw_color == Direction.DOWN_BLUE and previous_regime == "POSITIVE_BREAKOUT_RED")
        )
    ):
        regime = "RAW_DIRECT"
        required_count = 1
    if (
        raw_color == Direction.DOWN_BLUE
        and regime == "POSITIVE_REGIME_BLUE"
        and previous_regime == "POSITIVE_BREAKOUT_RED"
    ):
        required_count = 1

    if raw_color == Direction.HOLD or regime is None:
        return ConfirmedColorFlagResult(
            raw_color=raw_color, previous_color_state=previous_color_state,
            current_color_state=previous_color_state, direction=Direction.HOLD, onset=False,
            pending_direction=None, pending_count=0, required_count=required_count,
            regime=regime, publishable=publishable,
        )
    next_count = pending_count + 1 if pending_direction == raw_color else 1
    onset = publishable and previous_color_state != raw_color and next_count >= required_count
    direction = raw_color if onset else Direction.HOLD
    current_color_state = raw_color if onset else previous_color_state
    return ConfirmedColorFlagResult(
        raw_color=raw_color, previous_color_state=previous_color_state,
        current_color_state=current_color_state, direction=direction, onset=onset,
        pending_direction=None if onset else raw_color, pending_count=0 if onset else next_count,
        required_count=required_count, regime=regime, publishable=publishable,
    )


def old_compute_today_signal_overview(df_1m, *, now):
    """Reconstruction of worker.compute_today_signal_overview as it existed
    BEFORE commit 4747db4 (this morning 10:19) — no pos==0 baseline reset, and
    the pre-fix _color_publication_regime/onset rules."""
    bars_3m = resample_completed_3m(df_1m, now=now)
    bars_3m, _dropped = filter_complete_3m_bars(bars_3m, df_1m)
    if bars_3m.empty:
        return []
    today_str = now.astimezone(KST).strftime("%Y%m%d")
    today_mask = bars_3m["datetime"].dt.strftime("%Y%m%d") == today_str
    today_indices = list(bars_3m.index[today_mask])
    if not today_indices:
        return []
    overview = []
    last_direction = None
    pending_direction = None
    pending_count = 0
    last_regime = None
    for pos, idx in enumerate(today_indices):
        window = bars_3m.iloc[: idx + 1]
        snap = calculate_macd(window)
        if snap is None:
            continue
        from datetime import timedelta as _td
        bar_end = snap.bar_dt + _td(minutes=3)
        decision = old_evaluate_confirmed_macd_color_onset(
            snap, last_direction, pending_direction, pending_count,
            previous_regime=last_regime, publishable=bar_end.time() < config.NEW_ENTRY_CUTOFF,
        )
        pending_direction = decision.pending_direction
        pending_count = decision.pending_count
        direction = decision.direction
        if direction == Direction.HOLD:
            continue
        last_direction = direction
        last_regime = decision.regime
        overview.append({
            "bar_start_at": snap.bar_dt.isoformat(),
            "bar_end_at": bar_end.isoformat(),
            "direction": direction.value,
        })
    return overview


def main() -> None:
    now = datetime.now(KST)
    print(f"=== NOW (KST) = {now.isoformat()} ===")

    market_data = MarketDataService(mode="mock")
    boot = market_data.bootstrap(now=now)
    print("bootstrap ok:", boot.ok, "reason:", boot.reason)
    print("bootstrap diag:", json.dumps(market_data.get_last_bootstrap_diag(), ensure_ascii=False, default=str)[:2000])

    df_1m = market_data.get_history_df()
    print(f"df_1m rows={len(df_1m)}")
    if df_1m.empty:
        print("NO DATA - abort")
        return
    today_str = now.strftime("%Y%m%d")
    today_rows = df_1m[df_1m["datetime"].dt.strftime("%Y%m%d") == today_str]
    print(f"today 1m bars: {len(today_rows)}")
    if not today_rows.empty:
        print("today first bar:", today_rows["datetime"].iloc[0])
        print("today last bar:", today_rows["datetime"].iloc[-1])

    # 1) today's confirmed flags via the SAME (already-fixed) live logic.
    overview = worker.compute_today_signal_overview(df_1m, now=now, session_started_at=None)
    print(f"\n=== TODAY CONFIRMED FLAGS (current FIXED code), count={len(overview)} ===")
    for row in overview:
        print(row["bar_end_at"], row["direction"], row["signal_id"])

    old_overview = old_compute_today_signal_overview(df_1m, now=now)
    print(f"\n=== TODAY CONFIRMED FLAGS (PRE-FIX code, as it ran before 10:19 today), count={len(old_overview)} ===")
    for row in old_overview:
        print(row["bar_end_at"], row["direction"])

    # 2) build completed 3m bars (today+prior, for MAJOR filter's own lookback).
    bars_3m_all = resample_completed_3m(df_1m, now=now)
    bars_3m_all, _dropped = filter_complete_3m_bars(bars_3m_all, df_1m)

    def idx_for_bar_end(bar_end_iso: str):
        target = datetime.fromisoformat(bar_end_iso) - timedelta(minutes=3)
        matches = bars_3m_all.index[bars_3m_all["datetime"] == target]
        return int(matches[0]) if len(matches) else None

    # Scenario A: MAJOR FILTER OFF - every alternating flag executes (entry / full switch).
    posA = None
    tradesA = []
    for row in overview:
        direction = Direction(row["direction"])
        if posA is None:
            tradesA.append({"at": row["bar_end_at"], "type": "INITIAL_BUY", "direction": direction.value})
            posA = direction
        elif direction != posA:
            tradesA.append({"at": row["bar_end_at"], "type": "SWITCH_SELL", "direction": posA.value})
            tradesA.append({"at": row["bar_end_at"], "type": "SWITCH_BUY", "direction": direction.value})
            posA = direction

    # Scenario B: MAJOR FILTER ON for the whole day - simulate evaluate_major_flag + apply_major_trade_gates.
    posB = None
    last_entry_at = None
    last_exit_direction = None
    last_exit_at = None
    daily_count = 0
    tradesB = []
    decisionsB = []
    for row in overview:
        direction = Direction(row["direction"])
        bar_end = datetime.fromisoformat(row["bar_end_at"])
        idx = idx_for_bar_end(row["bar_end_at"])
        if idx is None:
            decisionsB.append({"at": row["bar_end_at"], "direction": direction.value, "decision": "NO_BAR_WINDOW"})
            continue
        window = bars_3m_all.iloc[: idx + 1]
        decision = major_flag_filter.evaluate_major_flag(
            window, direction, posB, last_entry_at, daily_count, bar_end,
        )
        same_dir_exit_at = last_exit_at if last_exit_direction == direction else None
        decision = major_flag_filter.apply_major_trade_gates(
            decision, flag_direction=direction, position_direction=posB,
            last_entry_at=last_entry_at, last_same_direction_exit_at=same_dir_exit_at,
            daily_major_entry_count=daily_count, now=bar_end,
        )
        decisionsB.append({
            "at": row["bar_end_at"], "direction": direction.value, "approved": decision.approved,
            "decision": decision.decision, "score": round(decision.score, 1),
            "required_score": decision.required_score, "is_reversal": decision.is_reversal,
        })
        if decision.approved:
            if posB is not None and posB != direction:
                tradesB.append({"at": row["bar_end_at"], "type": "SWITCH_SELL", "direction": posB.value})
                last_exit_direction, last_exit_at = posB, bar_end
                tradesB.append({"at": row["bar_end_at"], "type": "SWITCH_BUY", "direction": direction.value})
            elif posB is None:
                tradesB.append({"at": row["bar_end_at"], "type": "INITIAL_BUY", "direction": direction.value})
            posB = direction
            last_entry_at = bar_end
            daily_count += 1
        else:
            if posB is not None and posB != direction:
                # 2026-08-03 fix: reversal rejected by MAJOR filter still exits the old ETF.
                tradesB.append({"at": row["bar_end_at"], "type": "REVERSAL_EXIT_ONLY_SELL", "direction": posB.value})
                last_exit_direction, last_exit_at = posB, bar_end
                posB = None

    print(f"\n=== SCENARIO A (MAJOR FILTER OFF all day) - {len(tradesA)} order legs ===")
    for t in tradesA:
        print(t)

    print(f"\n=== SCENARIO B (MAJOR FILTER ON all day) - {len(tradesB)} order legs ===")
    for t in tradesB:
        print(t)

    print("\n=== SCENARIO B per-flag decisions ===")
    for d in decisionsB:
        print(d)

    # 3) Replay the PRE-FIX flag stream (what the live bot actually computed
    # before 10:19/11:14 today) through MAJOR FILTER ON, once with the OLD
    # dispatch rule (a MAJOR-rejected reversal does nothing at all) and once
    # with the NEW rule (a MAJOR-rejected reversal still exits the old ETF).
    print("\n=== REPLAY of PRE-FIX flag stream under MAJOR FILTER ON (old vs new dispatch) ===")
    posC_old, posC_new = None, None
    last_entry_at_c, daily_count_c = None, 0
    last_exit_dir_c, last_exit_at_c = None, None
    for row in old_overview:
        direction = Direction(row["direction"])
        bar_end = datetime.fromisoformat(row["bar_end_at"])
        idx = idx_for_bar_end(row["bar_end_at"])
        if idx is None:
            continue
        window = bars_3m_all.iloc[: idx + 1]
        decision = major_flag_filter.evaluate_major_flag(
            window, direction, posC_old, last_entry_at_c, daily_count_c, bar_end,
        )
        same_dir_exit_at = last_exit_at_c if last_exit_dir_c == direction else None
        decision = major_flag_filter.apply_major_trade_gates(
            decision, flag_direction=direction, position_direction=posC_old,
            last_entry_at=last_entry_at_c, last_same_direction_exit_at=same_dir_exit_at,
            daily_major_entry_count=daily_count_c, now=bar_end,
        )
        print(f"{row['bar_end_at']} flag={direction.value} pos_before={posC_old} "
              f"approved={decision.approved} decision={decision.decision} score={decision.score:.0f}/{decision.required_score:.0f}")
        if decision.approved:
            if posC_old is not None and posC_old != direction:
                print(f"  -> OLD dispatch: SELL {posC_old.value} + BUY {direction.value} (approved switch)")
                print(f"  -> NEW dispatch: SELL {posC_old.value} + BUY {direction.value} (approved switch, same)")
            else:
                print(f"  -> OLD/NEW dispatch: BUY {direction.value} (approved entry, same)")
            last_exit_dir_c, last_exit_at_c = posC_old, bar_end
            posC_old = direction
            posC_new = direction
            last_entry_at_c = bar_end
            daily_count_c += 1
        else:
            if posC_old is not None and posC_old != direction:
                print(f"  -> OLD dispatch (pre-11:14 fix): NOTHING happens - {posC_old.value} position stays held, "
                      f"{direction.value} entry skipped (the exact 매도/매수 안됨 bug)")
                print(f"  -> NEW dispatch (post-11:14 fix): SELL {posC_old.value} only (exits old ETF), "
                      f"{direction.value} entry still skipped")
                last_exit_dir_c, last_exit_at_c = posC_new, bar_end
                posC_new = None
                # posC_old intentionally left held (reproduces the pre-fix bug's stuck state)
            # else: plain rejected INITIAL entry, nothing to do either way.


if __name__ == "__main__":
    main()
