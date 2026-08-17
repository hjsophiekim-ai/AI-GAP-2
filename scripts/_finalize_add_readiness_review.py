#!/usr/bin/env python
"""One-off: append the production-readiness review (parity check + 7-category
safety sweep + proposed file-change list) to FINAL_STRATEGY_CONFIRMED.json.
No production code touched -- this only documents what a future change
would look like."""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "data" / "validation" / "tw_gate_relaxed_optimization"
path = BASE / "FINAL_STRATEGY_CONFIRMED.json"
data = json.loads(path.read_text(encoding="utf-8"))

data["status"] = "CONFIRMED (backtest) -- NOT YET DEPLOYED to production; see production_readiness_review"

data["production_readiness_review"] = {
    "reviewed_at": "2026-08-17",
    "method": "Direct code read of app/trading/macd2/{worker.py,time_window_filter.py,time_window_position_manager.py,signal_engine.py,order_executor.py,ledger.py} -- no production files modified.",
    "entry_function_and_t3_parity": {
        "verdict": "PARTIAL PARITY -- T+3 confirmation mechanics are identical; the per-window quality gate is NOT identical",
        "t3_confirmation_identical": True,
        "t3_evidence": [
            "worker.py:1561 _judge_time_window_flag never grants order authority on the flag bar itself; only records state.time_window_pending_flag_direction/pending_flag_bar_ts (worker.py:1580-1591)",
            "worker.py:1730 _resolve_time_window_candidate calls time_window_filter.evaluate_time_window_entry(...) exactly one completed bar later (worker.py:1771-1776) -- the SAME primitives the backtest reuses (calculate_flag_quality_score/classify_window/is_valid_reset/_find_previous_opposite_flag/_gap_series/_prepare_bars in time_window_filter.py)",
            "evaluate_time_window_entry rejects if the bar alignment has drifted (worker downtime etc.): flag_rows[-1] != len(series)-2 -> TW_REJECT_NOT_CONFIRMED (time_window_filter.py:352-357)",
            "bars_3m passed in is produced by signal_engine.resample_completed_3m(one_minute_bars, now=...), which only includes 3m windows fully closed as of now (signal_engine.py:43-48) -- no forming bar ever reaches the decision function",
        ],
        "structural_difference_found": (
            "Production's evaluate_time_window_entry applies a DIFFERENT quality gate per window "
            "(time_window_filter.py:435-506): W1_MORNING_AGGRESSIVE has no quality-score check at all "
            "(no branch, required_score=0.0); W2_MORNING_SECOND only checks is_valid_reset(), no score "
            "check; W3/W4/W5 check quality_score >= config.QUALITY_SCORE_THRESHOLD (a single global "
            "value, currently 4); W6_LATE_AFTERNOON_MAIN checks only quality_detail['price_vs_ema'] "
            "plus a reset requirement on the 2nd afternoon entry, not a score threshold at all. The "
            "backtest's evaluate_relaxed_entry (scripts/tw_gate_relaxed_optimization.py) applies ONE "
            "uniform quality_score >= threshold+bonus check to every window with no exemptions. This "
            "means the confirmed strategy is not simply 'production + 3 new knobs' -- it also silently "
            "drops W1's exemption, W2's skip, and W6's component-only check. Deploying the 3 new knobs "
            "onto unchanged production logic would NOT reproduce the backtested behavior in W1/W2/W6."
        ),
        "open_decision_required": (
            "Before implementing, decide: (A) keep W1/W2/W6 production special-cases as-is and only add "
            "the uniform threshold+bonus to W3/W4/W5 (closer to today's live behavior, but then W1/W2/W6 "
            "were never actually backtested under this confirmed strategy), or (B) unify all windows to "
            "the flat quality_score>=threshold+bonus check the backtest used (matches what was actually "
            "validated, but changes W1/W2/W6 live behavior). Recommendation: (B), since it's the only "
            "option that matches what TRAIN/VAL/OOS actually measured -- but this is a strategy-semantics "
            "call, not a pure implementation detail, so flagging explicitly rather than deciding silently."
        ),
        "new_knob_minimal_change_sketch_pseudocode_only": [
            "(a) per-direction quality bonus: config.py add TW_QUALITY_SCORE_BONUS_UP_RED (default 1); "
            "time_window_filter.py's threshold branches use required_score = config.QUALITY_SCORE_THRESHOLD "
            "+ (bonus if direction==UP_RED else 0)",
            "(b) daily raw flag-rank cap: production ALREADY increments state.daily_confirmed_flag_count "
            "on every confirmed flag (worker.py:1540-1541, used by single_entry_filter) and already resets "
            "it on day rollover (worker.py:355) -- no new counter needed. _judge_time_window_flag should "
            "additionally stash state.time_window_pending_flag_seq = state.daily_confirmed_flag_count when "
            "recording the pending flag; _resolve_time_window_candidate passes flag_seq=... into "
            "evaluate_time_window_entry, which adds one reject branch if flag_seq > config.TW_MAX_FLAG_SEQ_OF_DAY",
            "(c) exclude a specific entry_seq: production only counts morning/afternoon entries separately "
            "(worker.py:1826-1831); add a new state.time_window_daily_entry_seq counter incremented at the "
            "same point, pass entry_seq=... into evaluate_time_window_entry, add one reject branch if "
            "entry_seq in config.TW_EXCLUDED_ENTRY_SEQ_OF_DAY",
        ],
    },
    "safety_sweep": [
        {"category": "look_ahead_bias", "verdict": "SAFE",
         "evidence": "signal_engine.py:43-48 resample_completed_3m only returns 3m windows fully closed as of `now`, forming bars never included; time_window_filter.py:352-357 rejects if bars_3m doesn't end exactly one bar after the flag bar."},
        {"category": "duplicate_entry", "verdict": "SAFE",
         "evidence": "order_executor.py:351-379 execute_signal blocks BLOCK_DUPLICATE_SIGNAL for a signal_id already in processed_signal_ids, and BLOCK_ALREADY_HOLDING for an already-held same-direction position."},
        {"category": "incomplete_3m_bar_used_as_completed", "verdict": "SAFE",
         "evidence": "Same mechanism as look_ahead_bias: signal_engine.py:43-48."},
        {"category": "day_rollover_state_reset", "verdict": "SAFE",
         "evidence": "worker.py:2448 calls _apply_day_rollover every tick; on a session_date change it resets time_window_morning_entry_count/afternoon_entry_count/pending_flag_direction/daily_confirmed_flag_count etc. exactly once (worker.py:310-324, 355, 365-368). The filter's enable/disable toggle and any already-held position are intentionally preserved across rollover."},
        {"category": "duplicate_order_submission", "verdict": "SAFE",
         "evidence": "Entry side reuses the duplicate_entry guard above. Exit side: _advance_stop_loss_bar silently ignores a second call for the same completed bar (worker.py:1594-1637 docstring) so the TW ladder decision fires at most once per bar; order_executor issues a single buy/sell_market call per decision; ledger.py:191-200's append_signal rejects a duplicate signal_id at the persistence layer too."},
        {"category": "partial_tp_quantity_handling", "verdict": "CAUTION -- narrow edge case found",
         "evidence": "worker.py:1678-1679 sets state.time_window_tp1_done = pm_decision.tp1_done BEFORE the partial-exit order is confirmed filled, while state.position's quantity is only updated later, gated on outcome.final_state == EXECUTED (worker.py:1701). If the partial-exit order fails, tp1_done stays True even though the full original quantity is still held -- the tighter post-TP1 stop (+0.3%/trailing) then applies to a position that was never actually reduced. This is a narrow failure-path edge case (order rejection/failure only) and biases toward an earlier, more conservative stop rather than a missed stop -- not a look-ahead or overstatement risk, but worth fixing before relying on it in a fast market with order failures."},
        {"category": "trading_cost_calculation", "verdict": "SAFE",
         "evidence": "Live decision math (worker._net_return_pct, worker.py:139-145), live ledger recording (order_executor.py's _record_leg calling cost_engine.compute_net_pnl, order_executor.py:245-247), and the backtest (scripts/tw_gate_relaxed_optimization.py's _net_pct calls worker._net_return_pct directly, worker.py:78) all route through the identical TradeCostEngine().compute_net_pnl -- no separate/simplified cost model in the backtest."},
    ],
    "existing_test_coverage_confirmed_passing": [
        "tests/macd2/test_time_window_filter.py (incl. TestNoLookAhead::test_evaluate_time_window_entry_never_reads_past_confirm_bar, TestEntryCaps::test_duplicate_position_same_direction_rejects)",
        "tests/macd2/test_time_window_position_manager.py",
        "tests/macd2/test_worker_time_window.py (incl. test_day_rollover_resets_time_window_session_counters, test_no_lookahead_run_once_only_uses_bars_up_to_now)",
        "tests/mu_macd/test_worker_time_window_filter.py",
        "tests/test_trading_cost_engine.py",
        "52 tests passed, 0 failed as of this review",
    ],
}

data["production_deployment_plan"] = {
    "status": "PROPOSED ONLY -- no production file has been modified",
    "open_decision_before_implementation": data["production_readiness_review"]["entry_function_and_t3_parity"]["open_decision_required"],
    "files": [
        {
            "file": "app/trading/macd2/config.py",
            "changes": [
                "Add TW_QUALITY_SCORE_BONUS_UP_RED = _env_int('MACD2_TW_QUALITY_SCORE_BONUS_UP_RED', 1)",
                "Add TW_MAX_FLAG_SEQ_OF_DAY = _env_int('MACD2_TW_MAX_FLAG_SEQ_OF_DAY', 4) (0/None = disabled)",
                "Add TW_EXCLUDED_ENTRY_SEQ_OF_DAY (parsed int tuple), default (4,)",
                "Change MORNING_TP1_SELL_RATIO default 0.50 -> 0.30",
                "Change AFTERNOON_TP default 0.025 -> 0.020",
                "Change AFTERNOON_STOP_LOSS default -0.012 -> -0.008",
            ],
        },
        {
            "file": "app/trading/macd2/time_window_filter.py",
            "changes": [
                "In evaluate_time_window_entry's W3/W4/W5 threshold branches (~lines 445-470): required_score = config.QUALITY_SCORE_THRESHOLD + (config.TW_QUALITY_SCORE_BONUS_UP_RED if direction==Direction.UP_RED else 0)",
                "RESOLVE OPEN DECISION FIRST: whether W1's exemption / W2's skip / W6's component-only check stay as-is or get unified to the same flat threshold check (see open_decision_before_implementation)",
                "Add optional flag_seq / entry_seq parameters to evaluate_time_window_entry; add one reject branch each for TW_MAX_FLAG_SEQ_OF_DAY and TW_EXCLUDED_ENTRY_SEQ_OF_DAY",
            ],
        },
        {
            "file": "app/trading/macd2/worker.py",
            "changes": [
                "_judge_time_window_flag (~line 1561/1581): also store state.time_window_pending_flag_seq = state.daily_confirmed_flag_count (that counter already exists and already resets on day rollover)",
                "Add new state.time_window_daily_entry_seq counter, incremented alongside the existing morning/afternoon counters (~lines 1826-1831)",
                "_resolve_time_window_candidate (~line 1771): pass flag_seq=state.time_window_pending_flag_seq, entry_seq=state.time_window_daily_entry_seq+1 into evaluate_time_window_entry",
                "Day-rollover reset block (~lines 355-368): also reset time_window_pending_flag_seq and time_window_daily_entry_seq",
                "KNOWN CONSTRAINT (optional hardening, not required to deploy): in _advance_held_position_risk_management (~lines 1678-1701), consider gating state.time_window_tp1_done's assignment on outcome.final_state == EXECUTED the same way the quantity update already is, to close the narrow partial-exit-order-failure edge case found in the safety review",
            ],
        },
        {
            "file": "app/trading/macd2/models.py (RuntimeState) + state_store schema/version",
            "changes": [
                "Add the two new persisted fields: time_window_pending_flag_seq, time_window_daily_entry_seq (with a state-store version bump per existing convention)",
            ],
        },
        {
            "file": "tests/macd2/test_time_window_filter.py, tests/macd2/test_worker_time_window.py",
            "changes": [
                "Add coverage for the 3 new knobs once implemented (flag-seq cap rejection, entry-seq exclusion rejection, per-direction quality bonus) plus a regression test locking in whichever choice is made for the W1/W2/W6 open decision",
            ],
        },
    ],
}

path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("updated", path)
