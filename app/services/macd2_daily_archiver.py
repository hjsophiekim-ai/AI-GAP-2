"""
macd2_daily_archiver.py — MACD2/TW2 리서치용 일별 아카이브. 완전히 독립된
read-only 부가기능이다.

절대 원칙(사용자 요구 2026-08-24): app/trading/macd2, app/trading/mu_macd,
app/trading/dynamic_exit_watcher 등 실거래 코드를 이 모듈은 절대 import해서
"호출"하지 않는다(주문 경로 없음) — 오직 순수 계산 함수(MACD 계산, TW/TW2
결정 함수, 3분봉 리샘플)만 재사용한다. scripts/run_tw2_today.py가 이미
검증한 것과 동일한 재사용 원칙: production evaluate_time_window_entry /
evaluate_tw2_extra_vetoes를 그대로 쓰되, 오케스트레이션은 이 모듈 것을 쓴다.

이 모듈의 어떤 함수도 예외를 밖으로 던지지 않는다(run_daily_archive는 섹션별로
try/except로 감싸 manifest에 실패를 기록만 하고 계속 진행한다) — 호출자가
어떤 스케줄러/스레드에서 부르든, 이 모듈의 실패가 그 스레드조차 죽이지 않게
하기 위함.

저장 위치: app.utils.data_paths.MACD2_DAILY_ARCHIVE_DIR (Render Persistent
Disk 마운트 경로 하위) / <date_ymd>/ — git repo에는 절대 커밋하지 않는다.
github_analysis_sync.py가 이 중 최근 60영업일분의 파생 데이터만 별도로 골라
repo의 data/analysis_60d/로 동기화한다(이 모듈은 그 동기화를 전혀 모른다).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.logger import logger  # noqa: E402
from app.trading.macd2 import config  # noqa: E402
from app.trading.macd2.signal_engine import resample_completed_3m  # noqa: E402
from app.utils.data_paths import MACD2_DAILY_ARCHIVE_DIR, MICRON_DIR  # noqa: E402
from app.services.minute_bar_archiver import fetch_full_day  # noqa: E402
import scripts.run_tw2_today as run_tw2  # noqa: E402 -- reuses PRODUCTION twf.* decisions

KST = config.KST

SYMBOLS = {
    "hynix": (config.WATCH_SYMBOL, config.NXT_MARKET_DIV_CODE),
    "long": (config.LONG_SYMBOL, "J"),
    "inverse": (config.INVERSE_SYMBOL, "J"),
}


def _out_dir(date_ymd: str, out_root: Optional[Path] = None) -> Path:
    root = out_root or MACD2_DAILY_ARCHIVE_DIR
    return Path(root) / date_ymd


def _normalize_datetime_kst(df: pd.DataFrame) -> pd.DataFrame:
    """2026-08-24 fix (found by tests/services/test_macd2_daily_archiver.py):
    a fresh KIS fetch tags ``datetime`` with config.KST's own tzinfo object,
    but a parquet round-trip reconstructs an offset-equal but DIFFERENT
    tzinfo object (pyarrow normalizes tz-aware timestamps through UTC and
    pandas rebuilds a generic fixed-offset tzinfo on read) -- pandas treats
    the two as incompatible dtypes, so pd.concat() silently falls back to
    ``object`` dtype instead of raising, and the LATER pd.to_datetime(...,
    errors="coerce") in signal_engine.resample_completed_3m then silently
    turns every row from one of the two sides into NaT and drops it (root
    cause of a real incident: every single one of "today"'s 1m rows
    vanishing from the flags/TW-candidate computation on every real run,
    with no error surfaced anywhere). Routing every frame through the same
    tz-normalize-via-UTC pipeline before any concat guarantees both sides
    carry the identical tzinfo object, so the dtypes always match."""
    if df is None or df.empty or "datetime" not in df.columns:
        return df
    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert(KST)
    return df


def _merge_1m(existing: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """Idempotent gap-fill merge: dedup by datetime, fresh wins on conflict
    (mirrors market_data.py's own merge_incremental_1m dedup pattern -- never
    reintroduces that module's logic, just the same dedup shape)."""
    existing = _normalize_datetime_kst(existing)
    fresh = _normalize_datetime_kst(fresh)
    if existing is None or existing.empty:
        return fresh
    if fresh is None or fresh.empty:
        return existing
    merged = pd.concat([existing, fresh], ignore_index=True)
    return merged.drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").reset_index(drop=True)


def _load_parquet_if_exists(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    try:
        return _normalize_datetime_kst(pd.read_parquet(path))
    except Exception as exc:  # corrupt/partial file -- treat as absent, never crash the run
        logger.warning("[macd2_daily_archiver] parquet read failed for %s (%r) -- treating as empty", path, exc)
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])


def _fetch_symbol_1m(client, tag: str, symbol: str, market_div: str, date_ymd: str, out_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"tag": tag, "symbol": symbol, "status": None, "rows": 0, "path": None}
    try:
        fresh = fetch_full_day(client, symbol, date_ymd)
    except Exception as exc:
        result["status"] = "FETCH_ERROR"
        result["error"] = repr(exc)
        return result
    path = out_dir / f"{tag}_1m.parquet"
    if fresh.empty:
        existing = _load_parquet_if_exists(path)
        if existing.empty:
            result["status"] = "NO_DATA"
            return result
        result["status"] = "KEPT_EXISTING_NO_NEW_DATA"
        result["rows"] = len(existing)
        result["path"] = str(path)
        return result
    fresh = fresh[fresh["datetime"].dt.strftime("%Y%m%d") == date_ymd]
    # 2026-08-24 fix: only the fetch above used to be guarded -- a merge/save
    # failure (e.g. the tz-normalization bug this defends against, or any
    # future one) would otherwise escape run_daily_archive entirely,
    # violating this module's own "never raises" contract.
    try:
        existing = _load_parquet_if_exists(path)
        merged = _merge_1m(existing, fresh)
        out_dir.mkdir(parents=True, exist_ok=True)
        merged.to_parquet(path, index=False)
    except Exception as exc:
        result["status"] = "MERGE_OR_SAVE_ERROR"
        result["error"] = repr(exc)
        return result
    result["status"] = "SAVED"
    result["rows"] = len(merged)
    result["path"] = str(path)
    return result


def _save_mu_snapshot(date_ymd: str, out_dir: Path) -> dict[str, Any]:
    """Best-effort: MU 1-minute bars are already continuously collected
    elsewhere (app/data_sources/auto_market_collector.py-family, unrelated to
    this archiver) into MICRON_DIR/MU_1min.csv -- this just snapshots that
    day's slice. Never triggers a new MU fetch itself (this archiver has no
    business polling a live feed on its own once-daily schedule)."""
    result: dict[str, Any] = {"tag": "mu", "status": None, "rows": 0, "path": None}
    src = MICRON_DIR / "MU_1min.csv"
    try:
        if not src.exists():
            result["status"] = "SOURCE_NOT_AVAILABLE"
            return result
        raw = pd.read_csv(src)
        if "datetime" not in raw.columns:
            result["status"] = "SOURCE_MALFORMED"
            return result
        raw["datetime"] = pd.to_datetime(raw["datetime"], errors="coerce")
        raw = raw.dropna(subset=["datetime"])
        day_df = raw[raw["datetime"].dt.strftime("%Y%m%d") == date_ymd]
        if day_df.empty:
            result["status"] = "NO_DATA_FOR_DATE"
            return result
        path = out_dir / "mu_1m.parquet"
        existing = _load_parquet_if_exists(path)
        merged = _merge_1m(existing, day_df)
        out_dir.mkdir(parents=True, exist_ok=True)
        merged.to_parquet(path, index=False)
        result["status"] = "SAVED"
        result["rows"] = len(merged)
        result["path"] = str(path)
    except Exception as exc:
        result["status"] = "ERROR"
        result["error"] = repr(exc)
    return result


def _compute_flags_and_tw(client, date_ymd: str, frames: dict[str, pd.DataFrame], out_dir: Path) -> dict[str, Any]:
    """Flags/TW-candidate/execution/backtest-summary derivation -- reuses
    scripts.run_tw2_today's helpers (which themselves only call the real
    production time_window_filter.evaluate_time_window_entry /
    evaluate_tw2_extra_vetoes; no filter logic is reimplemented here)."""
    result: dict[str, Any] = {"status": None}
    try:
        prior_date, prior_hynix_1m = run_tw2.find_prior_trading_day(client, date_ymd)
        prior_hynix_1m = _normalize_datetime_kst(prior_hynix_1m)
        hynix_today = _normalize_datetime_kst(frames["hynix"])
        hynix_1m_warm = (
            pd.concat([prior_hynix_1m, hynix_today], ignore_index=True)
            .drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").reset_index(drop=True)
            if prior_date is not None else hynix_today
        )
        now_after_close = datetime.strptime(date_ymd, "%Y%m%d").replace(hour=20, minute=5, tzinfo=KST)
        hynix_bars_3m = resample_completed_3m(hynix_1m_warm, now=now_after_close)
        long_bars_3m = resample_completed_3m(frames["long"], now=now_after_close)
        inverse_bars_3m = resample_completed_3m(frames["inverse"], now=now_after_close)
        etf_close_3m = {
            config.LONG_SYMBOL: run_tw2.etf_close_lookup(long_bars_3m),
            config.INVERSE_SYMBOL: run_tw2.etf_close_lookup(inverse_bars_3m),
        }

        current_day_mask = hynix_bars_3m["datetime"].dt.strftime("%Y%m%d") == date_ymd
        if not current_day_mask.any():
            result["status"] = "NO_COMPLETED_BARS_TODAY"
            return result
        start_idx = int(current_day_mask.to_numpy().nonzero()[0][0])

        flags = run_tw2.detect_confirmed_flags(hynix_bars_3m, date_ymd)
        flags_rows = [
            {
                "bar_start": pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]).isoformat(),
                "confirm_at": (pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]) + timedelta(minutes=3)).isoformat(),
                "direction": direction.value,
            }
            for idx, direction in flags
        ]
        pd.DataFrame(flags_rows).to_csv(out_dir / "flags.csv", index=False)

        trades, rejected = run_tw2.simulate_tw2(
            date_ymd, hynix_bars_3m, flags, etf_close_3m,
            {config.LONG_SYMBOL: frames["long"], config.INVERSE_SYMBOL: frames["inverse"]}, start_idx,
        )

        candidate_rows = [
            {"flag_bar_at": r["flag_bar_at"], "direction": r["direction"], "status": "REJECTED", "reason": r["reason"]}
            for r in rejected
        ] + [
            {
                "flag_bar_at": t["entry_time"].isoformat(), "direction": t["direction"], "status": "ENTERED",
                "reason": None, "quality_score": t.get("quality_score"),
            }
            for t in trades
        ]
        pd.DataFrame(candidate_rows).to_csv(out_dir / "tw_candidates.csv", index=False)

        exec_rows = [
            {
                "direction": t["direction"], "entry_time": t["entry_time"].isoformat(), "entry_symbol": t["entry_symbol"],
                "entry_price": t["entry_price"], "exit_time": t["exit_time"].isoformat() if t.get("exit_time") else None,
                "exit_price": t.get("exit_price"), "exit_reason": t.get("exit_reason"),
                "net_return_pct": t.get("net_return_pct"), "quality_score": t.get("quality_score"),
            }
            for t in trades
        ]
        pd.DataFrame(exec_rows).to_csv(out_dir / "executions.csv", index=False)

        returns = [t.get("net_return_pct") or 0.0 for t in trades]
        compounded = 1.0
        peak = 1.0
        mdd = 0.0
        for r in returns:
            compounded *= (1.0 + r / 100.0)
            peak = max(peak, compounded)
            mdd = max(mdd, (peak - compounded) / peak * 100.0)
        wins = sum(1 for r in returns if r > 0)
        summary_row = {
            "date": date_ymd, "flags_count": len(flags), "entries_count": len(trades),
            "rejected_count": len(rejected), "win_rate_pct": (wins / len(returns) * 100.0) if returns else None,
            "total_return_simple_pct": sum(returns) if returns else 0.0,
            "compounded_return_pct": (compounded - 1.0) * 100.0,
            "mdd_pct": mdd, "avg_return_per_trade_pct": (sum(returns) / len(returns)) if returns else None,
        }
        pd.DataFrame([summary_row]).to_csv(out_dir / "backtest_summary.csv", index=False)
        result["status"] = "SAVED"
        result["flags"] = len(flags)
        result["entries"] = len(trades)
        result["rejected"] = len(rejected)
    except Exception as exc:
        result["status"] = "ERROR"
        result["error"] = repr(exc)
    return result


def run_daily_archive(client, date_ymd: str, *, out_root: Optional[Path] = None) -> dict[str, Any]:
    """Best-effort, section-isolated daily archive for one KST trading date.
    Never raises. Returns a manifest dict; the same dict is also written to
    <out_dir>/_manifest.json."""
    out_dir = _out_dir(date_ymd, out_root)
    manifest: dict[str, Any] = {"date": date_ymd, "run_at": datetime.now(KST).isoformat(), "sections": {}}

    frames: dict[str, pd.DataFrame] = {}
    for tag, (symbol, market_div) in SYMBOLS.items():
        r = _fetch_symbol_1m(client, tag, symbol, market_div, date_ymd, out_dir)
        manifest["sections"][f"{tag}_1m"] = r
        frames[tag] = _load_parquet_if_exists(out_dir / f"{tag}_1m.parquet")

    manifest["sections"]["mu_1m"] = _save_mu_snapshot(date_ymd, out_dir)
    manifest["sections"]["mu_macd_flags"] = {"status": "NOT_IMPLEMENTED", "note": "MU_MACD has no standalone pure flag function yet; deferred"}

    if frames.get("hynix", pd.DataFrame()).empty or frames.get("long", pd.DataFrame()).empty or frames.get("inverse", pd.DataFrame()).empty:
        manifest["sections"]["flags_and_tw"] = {"status": "SKIPPED_MISSING_1M_DATA"}
    else:
        manifest["sections"]["flags_and_tw"] = _compute_flags_and_tw(client, date_ymd, frames, out_dir)

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as exc:
        logger.warning("[macd2_daily_archiver] manifest write failed: %r", exc)

    return manifest
