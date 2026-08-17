#!/usr/bin/env python
"""2026-08-17: FINAL, LOCKED "시간대별 최적거래 필터" (게이트 완화 변형)
parameter + result snapshot. This file is the single source of truth for
what strategy was confirmed. Per explicit user instruction, NO further
parameter changes may be made based on OOS results after this point -- any
future retune must start a new TRAIN/VAL/OOS split and a new confirmation
round, never silently edit these values.
"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "data" / "validation" / "tw_gate_relaxed_optimization"

final_report = json.loads((BASE / "FINAL_report.json").read_text(encoding="utf-8"))
breakdown = json.loads((BASE / "final_strategy_breakdown.json").read_text(encoding="utf-8"))

FINAL_STRATEGY = {
    "confirmed_at": "2026-08-17",
    "confirmed_by_instruction": "사용자 명시적 확정 지시 (2026-08-17) — 이후 OOS 결과로 파라미터 재조정 금지",
    "status": "NOT YET IMPLEMENTED IN PRODUCTION -- research/backtest only (see production_gap_notes)",
    "data_split": {
        "train_days": 34, "train_range": "2026-05-27 ~ 2026-07-14",
        "val_days": 11, "val_range": "2026-07-15 ~ 2026-07-30",
        "oos_days": 11, "oos_range": "2026-07-31 ~ 2026-08-14",
    },
    "entry_conditions": {
        "trading_window": "09:00-15:00 (time_window_filter.classify_window W1-W6)",
        "confirmation": "T+3 (confirmed MACD/Signal crossover bar, then re-check exactly one 3-min bar later)",
        "gap_must_still_hold": "gap_now > 0 in the flag's own direction at confirmation bar",
        "gap_expansion_required": True,
        "quality_score_base_threshold": 3,
        "quality_score_direction_bonus": {"UP_RED(레버리지 매수)": "+1 -> 실질 임계값 4", "DOWN_BLUE(인버스)": "+0 -> 실질 임계값 3"},
        "quality_score_components": [
            "1. T+3 확정 유지(구조적으로 항상 True)",
            "2. MACD-Signal gap이 flag bar 대비 확대",
            "3. 종가가 EMA10(W1-W3)/EMA20(W4-W6) 대비 방향에 맞게 위치",
            "4. EMA10 vs EMA20 추세정렬",
            "5. 확정봉 거래량 >= 직전 5개 완성봉 평균",
        ],
        "min_flag_interval_minutes": 9,
        "short_interval_fallback": "9분 미만이면 is_valid_reset()으로 유효 리셋 여부 재확인, 실패시 거부",
        "max_morning_entries": 3, "max_afternoon_entries": 2, "max_daily_entries": 5,
        "max_flag_seq_of_day": 4,
        "max_flag_seq_of_day_note": "당일 원시 MACD 크로스오버(양방향 합산) 5번째 이후는 진입 대상에서 제외 (TRAIN/VAL 양쪽에서 확인된 구조적 손실 구간)",
        "excluded_entry_seq_of_day": [4],
        "excluded_entry_seq_of_day_note": "당일 4번째 진입(오전 3회를 모두 소진한 뒤 첫 오후 진입)은 제외 -- TRAIN/VAL 양쪽에서 확인된 구조적 손실 구간. 5번째 진입(오후 두번째 슬롯)은 정상 유지",
        "no_pyramiding": True,
        "blocked_windows": "없음 (W4_NO_NEW_ENTRY 포함 전 구간 진입 허용 -- '게이트 전체 완화' 정신 유지)",
    },
    "exit_conditions": {
        "morning_ladder": {
            "tp1_pct": 2.5, "tp1_sell_ratio": 0.30,
            "tp2_pct": 5.0, "tp2_note": "고정값, 스윕 대상 아니었음",
            "stop_loss_pct": -1.5,
            "after_tp1_stop_pct": 0.3, "after_tp1_stop_note": "기본값 유지, 스윕 대상 아니었음",
            "trailing_trigger_pct": 3.5, "trailing_stop_pct": 2.0, "trailing_note": "기본값 유지",
        },
        "afternoon_ladder": {
            "tp_pct": 2.0, "stop_loss_pct": -0.8,
            "breakeven_trigger_pct": 1.5, "breakeven_stop_pct": 0.2, "breakeven_note": "기본값 유지",
            "profit_lock_trigger_pct": 2.0, "profit_lock_stop_pct": 1.0, "profit_lock_note": "기본값 유지",
        },
        "force_liquidate_at": "15:00",
    },
    "results_overall": {
        "TRAIN": final_report["final_train"],
        "VAL": final_report["final_val"],
        "OOS": final_report["final_oos"],
        "full_span_chained_compounded_pct": None,  # filled below
    },
    "results_by_direction": breakdown["direction_breakdown"],
    "results_by_session_and_entry_seq": breakdown["session_entryseq_breakdown"],
    "oos_all_23_trades": breakdown["oos_all_trades"],
    "oos_loss_pattern_summary": {
        "note": "OOS 23건 중 13건(56.5%) 손실이지만 승리거래 평균이 커서 전체 PF 1.23 플러스. 조건 수정 없이 관찰만 기록.",
        "by_session": "오후 진입 4건 전부 손실(전부 W5_EARLY_AFTERNOON_A_GRADE 창) vs 오전 19건 중 9건 손실(승률 52.6%) -- 오후 표본이 4건뿐이라 결론 보류, 주시 필요",
        "by_direction": "INV 16건 중 9패(43.75%->대략 균형), LONG 7건 중 4패(42.9%->유사) -- OOS에서는 TRAIN에서 보였던 LONG 구조적 약세가 뚜렷하지 않음(표본 작음)",
        "by_quality": "quality 3/4/5 손실 각각 4/5/4건으로 고르게 분포 -- OOS 표본에서는 quality 점수가 손실을 잘 구분하지 못함",
        "by_exit_reason": "정상 손절(STOP_LOSS) 9건, 반대신호 청산 3건, 손익분기손절 1건 -- 반복적 버그성 패턴 없음, 정상적 리스크관리 결과로 판단",
    },
    "production_gap_notes": "backtest와 production(worker.py)이 완전히 동일한 진입판단 함수를 쓰지 못함 -- 별도 안전성 검토 보고서 참고. quality_score 방향별 보너스, max_flag_seq_of_day, excluded_entry_seq_of_day는 현재 production time_window_filter.evaluate_time_window_entry에 없는 knob임.",
}

train_c = final_report["final_train"]["compounded_cumulative_return_pct"]
val_c = final_report["final_val"]["compounded_cumulative_return_pct"]
oos_c = final_report["final_oos"]["compounded_cumulative_return_pct"]
chained = ((1 + train_c / 100) * (1 + val_c / 100) * (1 + oos_c / 100) - 1) * 100
FINAL_STRATEGY["results_overall"]["full_span_chained_compounded_pct"] = round(chained, 3)

out_path = BASE / "FINAL_STRATEGY_CONFIRMED.json"
out_path.write_text(json.dumps(FINAL_STRATEGY, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print(f"Saved -> {out_path}")
print(f"Full-span chained compounded: {chained:.2f}%")
