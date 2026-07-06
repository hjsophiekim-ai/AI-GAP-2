#!/usr/bin/env python
"""CLI script: dry-run full flow.

Pipeline:
  collect -> features -> predict -> candidate50 -> top15 -> allocate -> buy -> portfolio

Usage:
    python scripts/run_dry_buy.py [--date YYYYMMDD] [--budget AMOUNT] [--no-save]

This uses DryRunBroker (no real orders placed). It overrides config mode to
'dry_run' for this session only.
"""

import sys
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
import os
os.chdir(PROJECT_ROOT)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from app.data.data_collector import DataCollector
from app.features.feature_builder import FeatureBuilder
from app.ml.predict_model import ModelPredictor
from app.strategy.candidate_generator import CandidateGenerator
from app.strategy.top15_selector import Top15Selector
from app.trading.budget_allocator import BudgetAllocator
from app.trading.dry_run_broker import DryRunBroker
from app.trading.order_manager import OrderManager
from app.trading.portfolio import Portfolio
from app.config import get_config
from app.utils.time_utils import today_str


def _fmt_trade_value(val: float) -> str:
    if val >= 1_000_000_000_000:
        return f"{val / 1_000_000_000_000:.1f}조"
    if val >= 1_000_000_000:
        return f"{val / 1_000_000_000:.1f}B"
    if val >= 100_000_000:
        return f"{val / 100_000_000:.0f}억"
    return f"{val:,.0f}"


def _print_separator(width: int = 90):
    print("=" * width)


def main():
    parser = argparse.ArgumentParser(description="Dry-run full buy pipeline (no real orders)")
    parser.add_argument(
        "--date",
        default=today_str(),
        help="Target date in YYYYMMDD format (default: today)",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=None,
        help="Total budget in KRW (default: config trading.total_budget)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Skip saving intermediate CSV files",
    )
    args = parser.parse_args()

    date_str = args.date
    save = not args.no_save

    cfg = get_config()
    budget = args.budget or float(cfg.trading.get("total_budget", 10_000_000))

    print(f"[dry_buy] 날짜    : {date_str}")
    print(f"[dry_buy] 예산    : {budget:,.0f}원")
    print(f"[dry_buy] 모드    : dry_run (모의 매매 — 실제 주문 없음)")
    _print_separator()

    # 1. Collect
    print("[dry_buy] 1/7 데이터 수집 ...")
    collector = DataCollector()
    stocks = collector.collect_gap_candidates(date_str=date_str)
    print(f"          수집 완료: {len(stocks)}개 종목")

    # 2. Features
    print("[dry_buy] 2/7 피처 생성 ...")
    builder = FeatureBuilder()
    features = builder.build_features(stocks)
    if save:
        builder.save_features(features, date_str=date_str)
    print(f"          피처 완료: {len(features)}개")

    # 3. Predict
    print("[dry_buy] 3/7 ML 예측 ...")
    predictor = ModelPredictor()
    predictions = predictor.predict(features)
    if save:
        predictor.save_predictions(predictions, date_str=date_str)
    print(f"          예측 완료: {len(predictions)}개")

    # 4. Candidate50
    print("[dry_buy] 4/7 후보 50개 생성 ...")
    generator = CandidateGenerator()
    candidates = generator.generate(stocks, predictions=predictions)
    if save:
        generator.save_candidates(candidates, date_str=date_str)
    print(f"          후보 완료: {len(candidates)}개")

    # 5. Top15
    print("[dry_buy] 5/7 Top-15 선정 ...")
    selector = Top15Selector()
    top15 = selector.select(candidates)
    if save:
        selector.save_top15(top15, date_str=date_str)
    print(f"          Top15 완료: {len(top15)}개")

    if not top15:
        print("[dry_buy] 선정된 종목이 없어 매수를 진행할 수 없습니다.")
        return

    # 6. Budget allocation
    print("[dry_buy] 6/7 예산 배분 ...")
    allocator = BudgetAllocator(cfg=cfg)
    buy_plans = allocator.allocate(top15, total_budget=budget)
    if save:
        allocator.save_buy_plan(buy_plans, date_str=date_str)
    print(f"          배분 완료: {len(buy_plans)}개 종목 매수 계획")

    # 7. Execute buy orders (dry run)
    print("[dry_buy] 7/7 모의 매수 실행 ...")
    broker = DryRunBroker()
    portfolio = Portfolio(initial_balance=budget)
    order_mgr = OrderManager(broker=broker, cfg=cfg)

    results = order_mgr.execute_buy_plans(buy_plans)
    if save:
        order_mgr.save_order_log(results, date_str=date_str)

    success_count = sum(1 for r in results if r.success)
    fail_count = len(results) - success_count
    print(f"          주문 완료: 성공 {success_count}건, 실패 {fail_count}건")

    # Update portfolio
    for result in results:
        portfolio.add_position(result)

    # Print summary
    summary = portfolio.get_summary()
    _print_separator()
    print(f"  AI-GAP Dry-Run 결과 ({date_str})")
    _print_separator()
    print(f"  초기 자본   : {summary['initial_balance']:>15,.0f} 원")
    print(f"  투자 금액   : {summary['invested']:>15,.0f} 원")
    print(f"  잔여 현금   : {summary['cash']:>15,.0f} 원")
    print(f"  평가 총액   : {summary['total_value']:>15,.0f} 원")
    pnl = summary['pnl']
    pnl_rate = summary['pnl_rate']
    sign = "+" if pnl >= 0 else ""
    print(f"  평가 손익   : {sign}{pnl:>14,.0f} 원  ({sign}{pnl_rate:.2f}%)")
    print(f"  보유 포지션 : {summary['n_positions']}개")
    _print_separator()

    # Positions table
    positions = summary.get("positions", [])
    if positions:
        rows = []
        for pos in positions:
            profit_rate = pos.get("profit_rate", 0.0)
            sign = "+" if profit_rate >= 0 else ""
            rows.append({
                "코드": pos["symbol"],
                "종목명": pos["name"],
                "수량(주)": pos["quantity"],
                "평균단가": f"{pos['avg_price']:,.0f}",
                "현재가": f"{pos['current_price']:,.0f}",
                "평가금액": f"{pos['market_value']:,.0f}",
                "수익률(%)": f"{sign}{profit_rate:.2f}",
            })
        df = pd.DataFrame(rows)
        print(df.to_string(index=False))
    else:
        print("  보유 포지션 없음")

    _print_separator()
    print("[dry_buy] 완료.")


if __name__ == "__main__":
    main()
