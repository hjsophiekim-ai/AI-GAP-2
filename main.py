#!/usr/bin/env python
"""Main entry point for the AI-GAP trading system.

Usage:
    python main.py --script collect
    python main.py --script train
    python main.py --script top15
    python main.py --script buy --mode dry_run --budget 10000000
    python main.py --script app --mode mock
    python main.py --script buy --mode mock --budget 5000000
    python main.py --script buy --mode real --budget 10000000
"""

import argparse
import sys
import os
from pathlib import Path

# Ensure project root is in sys.path regardless of how main.py is invoked.
PROJECT_ROOT = Path(__file__).parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Script dispatch helpers
# ---------------------------------------------------------------------------

def _run_collect(args):
    """Run data collection pipeline."""
    from scripts.run_collect_data import main as _main
    # Inject CLI overrides via sys.argv substitution so argparse inside works.
    _argv = ["run_collect_data"]
    if args.date:
        _argv += ["--date", args.date]
    _patch_argv(_argv, _main)


def _run_train(args):
    """Run model training pipeline."""
    from scripts.run_train_model import main as _main
    _argv = ["run_train_model"]
    _patch_argv(_argv, _main)


def _run_top15(args):
    """Run full pipeline through top-15 selection."""
    from scripts.run_select_top15 import main as _main
    _argv = ["run_select_top15"]
    if args.date:
        _argv += ["--date", args.date]
    _patch_argv(_argv, _main)


def _run_buy(args):
    """Run buy pipeline according to mode."""
    mode = args.mode or _get_config_mode()

    if mode == "dry_run":
        from scripts.run_dry_buy import main as _main
        _argv = ["run_dry_buy"]
        if args.date:
            _argv += ["--date", args.date]
        if args.budget:
            _argv += ["--budget", str(args.budget)]
        _patch_argv(_argv, _main)

    elif mode in ("mock", "real"):
        # For mock/real modes, apply mode override to config then run buy script.
        _set_config_mode_override(mode)
        _run_mock_real_buy(args, mode)

    else:
        print(f"[main] 알 수 없는 mode: {mode}")
        sys.exit(1)


def _run_mock_real_buy(args, mode: str):
    """Run the full buy pipeline using Mock or Real broker."""
    # Import pipeline components directly (same as dry_buy but with broker from factory).
    from app.data.data_collector import DataCollector
    from app.features.feature_builder import FeatureBuilder
    from app.ml.predict_model import ModelPredictor
    from app.strategy.candidate_generator import CandidateGenerator
    from app.strategy.top15_selector import Top15Selector
    from app.trading.budget_allocator import BudgetAllocator
    from app.trading.broker_factory import create_broker
    from app.trading.order_manager import OrderManager
    from app.trading.portfolio import Portfolio
    from app.config import get_config
    from app.utils.time_utils import today_str
    import pandas as pd

    cfg = get_config()
    date_str = args.date or today_str()
    budget = args.budget or float(cfg.trading.get("total_budget", 10_000_000))

    print(f"[main] 날짜: {date_str}  예산: {budget:,.0f}원  모드: {mode}")

    broker = create_broker(cfg=cfg, mode=mode)

    collector = DataCollector()
    stocks = collector.collect_gap_candidates(date_str=date_str)

    builder = FeatureBuilder()
    features = builder.build_features(stocks)
    builder.save_features(features, date_str=date_str)

    predictor = ModelPredictor()
    predictions = predictor.predict(features)

    generator = CandidateGenerator()
    candidates = generator.generate(stocks, predictions=predictions)
    generator.save_candidates(candidates, date_str=date_str)

    selector = Top15Selector()
    top15 = selector.select(candidates)
    selector.save_top15(top15, date_str=date_str)

    if not top15:
        print("[main] 선정 종목 없음 — 매수 중단.")
        return

    allocator = BudgetAllocator(cfg=cfg)
    buy_plans = allocator.allocate(top15, total_budget=budget)
    allocator.save_buy_plan(buy_plans, date_str=date_str)

    portfolio = Portfolio(initial_balance=budget)
    order_mgr = OrderManager(broker=broker, cfg=cfg)
    results = order_mgr.execute_buy_plans(buy_plans)
    order_mgr.save_order_log(results, date_str=date_str)

    for result in results:
        portfolio.add_position(result)

    summary = portfolio.get_summary()
    success_count = sum(1 for r in results if r.success)
    print(f"[main] 매수 결과: 성공 {success_count}/{len(results)}건")
    print(f"[main] 투자 금액: {summary['invested']:,.0f}원  잔여: {summary['cash']:,.0f}원")

    positions = summary.get("positions", [])
    if positions:
        rows = [
            {
                "코드": p["symbol"],
                "종목명": p["name"],
                "수량": p["quantity"],
                "평균단가": f"{p['avg_price']:,.0f}",
                "평가금액": f"{p['market_value']:,.0f}",
            }
            for p in positions
        ]
        df = pd.DataFrame(rows)
        print(df.to_string(index=False))


def _run_regime(args):
    """Run Market Regime Router once and print the A~F decision."""
    from app.market.regime_router import determine_regime
    from app.config import get_config, get_market_regime_config

    result = determine_regime(cfg=get_config(), market_cfg=get_market_regime_config())
    print(f"[regime] 유형: {result['regime']} ({result['regime_label']})")
    print(f"[regime] confidence_score: {result['confidence_score']}")
    print(f"[regime] 정책: {result['policy_name']}")
    print(f"[regime] 확정여부: {result['is_confirmed']} (확정시각 {result['confirmed_at']})")
    print(f"[regime] 사유: {', '.join(result['reasons'])}")
    print(f"[regime] 데이터 품질: {result['data_quality_ratio']:.0%}")
    for k, v in result["scores"].items():
        print(f"[regime]   {k}: {v}")


def _run_app(args):
    """Launch the Streamlit UI."""
    from scripts.run_app import main as _main  # noqa: F401 — just import to trigger
    import subprocess
    port = 8501
    target = str(PROJECT_ROOT / "app" / "ui" / "streamlit_app.py")
    cmd = [sys.executable, "-m", "streamlit", "run", target, f"--server.port={port}"]
    print(f"[main] Streamlit 실행: {' '.join(cmd)}")
    subprocess.run(cmd)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _patch_argv(new_argv: list, fn):
    """Replace sys.argv temporarily and call fn()."""
    old_argv = sys.argv[:]
    sys.argv = new_argv
    try:
        fn()
    finally:
        sys.argv = old_argv


def _get_config_mode() -> str:
    try:
        from app.config import get_config
        return get_config().mode
    except Exception:
        return "dry_run"


def _set_config_mode_override(mode: str):
    """Patch the in-memory config singleton to use the given mode."""
    try:
        from app.config import get_config
        cfg = get_config()
        cfg._raw["mode"] = mode
    except Exception as exc:
        print(f"[main] config mode 설정 실패: {exc}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="AI-GAP 자동매매 시스템 메인 엔트리포인트",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --script collect
  python main.py --script train
  python main.py --script top15
  python main.py --script buy --mode dry_run --budget 10000000
  python main.py --script buy --mode mock
  python main.py --script buy --mode real --budget 5000000
  python main.py --script app
        """,
    )
    parser.add_argument(
        "--script",
        choices=["collect", "train", "top15", "buy", "app", "regime"],
        required=True,
        help=(
            "실행할 스크립트: "
            "collect=데이터수집, train=모델학습, top15=Top15선정, "
            "buy=매수실행, app=Streamlit UI, regime=오늘장 시장유형(A~F) 판단"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["dry_run", "mock", "real"],
        default=None,
        help="거래 모드 (기본값: config.yaml의 mode 설정값)",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=None,
        help="총 예산 (KRW). 기본값: config.yaml의 trading.total_budget",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="대상 날짜 (YYYYMMDD). 기본값: 오늘",
    )
    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_DISPATCH = {
    "collect": _run_collect,
    "train": _run_train,
    "top15": _run_top15,
    "buy": _run_buy,
    "app": _run_app,
    "regime": _run_regime,
}

if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    handler = _DISPATCH.get(args.script)
    if handler is None:
        print(f"[main] 알 수 없는 스크립트: {args.script}")
        sys.exit(1)

    handler(args)
