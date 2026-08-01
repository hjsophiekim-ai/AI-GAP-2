"""generate_backtest_report.py — 과거 데이터 기반 예측 검증 리포트 생성.

두 예측 파이프라인의 실제 예측 로그를 실제 시세/실제 시장유형과 매칭해
정확도를 계산하고 3개 산출물을 만든다:

  reports/backtest_market_prediction.csv  — 국내장 시장유형(A~F) 예측 검증
  reports/backtest_hynix_prediction.csv   — SK하이닉스 다중 horizon 가격 예측 검증
  reports/backtest_summary.md             — 종합 요약 리포트

중요 — 테스트 오염 필터링:
  logs/market_prediction/*.jsonl 에는 pytest 유닛테스트(app/market/regime_router.py
  가 어떤 이유로도 파일 기록을 mocking 없이 그대로 수행하기 때문)가 남긴 합성
  데이터가 실제 운영 기록과 섞여 있다. 유닛테스트는 30초 이내에 A~F 여러 유형을
  연속으로 호출하므로("버스트"), 30초 이내 3건 이상 몰린 클러스터는 실거래
  데이터가 아니라고 간주하고 전부 제외한다(실운영에서 5분 자동재평가/수동 클릭이
  30초 안에 3번 이상 몰릴 일은 없다). 이 필터링은 결과를 과장하지 않기 위한
  최소한의 안전장치이며, 제외된 건수를 항상 리포트에 명시한다.

사용법:
    python scripts/generate_backtest_report.py
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.market import tick_history  # noqa: E402
from app.models import model_calibration  # noqa: E402

MARKET_LOG_DIR = ROOT / "logs" / "market_prediction"
HYNIX_LOG_DIR = ROOT / "logs" / "hynix_prediction"
REPORTS_DIR = ROOT / "reports"

BURST_WINDOW_SECONDS = 30
BURST_MIN_SIZE = 3

MARKET_HORIZON_MINUTES = {"30m": 30, "1h": 60, "3h": 180}
MARKET_HORIZON_TOLERANCE_MIN = {"30m": 10, "1h": 15, "3h": 30}
RISK_REGIMES = {"D", "E"}

HYNIX_HORIZON_MINUTES = {"30m": 30, "1h": 60, "3h": 180}
CONFIDENCE_BUCKETS = [(0, 50), (50, 70), (70, 85), (85, 101)]
QUALITY_BUCKETS = [(0, 45), (45, 65), (65, 85), (85, 101)]


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------

def _load_jsonl_dir(dir_path: Path) -> list[dict]:
    records = []
    if not dir_path.exists():
        return records
    for path in sorted(dir_path.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    rec["_date_str"] = path.stem
                    records.append(rec)
                except Exception:
                    continue
    return records


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _filter_test_bursts(records: list[dict], ts_field: str) -> tuple[list[dict], int]:
    """30초 이내 3건 이상 몰린 클러스터를 유닛테스트 오염으로 간주해 제외한다."""
    dated = [(r, _parse_ts(r.get(ts_field, ""))) for r in records]
    dated = [(r, ts) for r, ts in dated if ts is not None]
    dated.sort(key=lambda pair: pair[1])

    clusters: list[list[tuple[dict, datetime]]] = []
    for r, ts in dated:
        if clusters and (ts - clusters[-1][-1][1]).total_seconds() <= BURST_WINDOW_SECONDS:
            clusters[-1].append((r, ts))
        else:
            clusters.append([(r, ts)])

    kept: list[dict] = []
    excluded = 0
    for cluster in clusters:
        if len(cluster) >= BURST_MIN_SIZE:
            excluded += len(cluster)
            continue
        kept.extend(r for r, _ in cluster)
    return kept, excluded


def _nearest_within(records: list[dict], ts_field: str, target: datetime, tolerance_min: float) -> dict | None:
    best, best_diff = None, None
    for r in records:
        ts = _parse_ts(r.get(ts_field, ""))
        if ts is None:
            continue
        diff = abs((ts - target).total_seconds())
        if diff <= tolerance_min * 60 and (best_diff is None or diff < best_diff):
            best, best_diff = r, diff
    return best


# ---------------------------------------------------------------------------
# 1) 국내장(Market Regime) 예측 검증
# ---------------------------------------------------------------------------

def build_market_backtest() -> tuple[list[dict], dict]:
    raw = _load_jsonl_dir(MARKET_LOG_DIR)
    real, excluded_burst = _filter_test_bursts(raw, "timestamp")
    real.sort(key=lambda r: r["timestamp"])

    rows: list[dict] = []
    for rec in real:
        ts = _parse_ts(rec["timestamp"])
        row = {
            "date": rec["_date_str"],
            "predicted_at": rec["timestamp"],
            "initial_regime": rec.get("initial_regime"),
            "current_regime": rec.get("current_regime"),
            "alert_level": rec.get("alert_level"),
            "market_collapse_score": rec.get("market_collapse_score"),
            "semiconductor_collapse_score": rec.get("semiconductor_collapse_score"),
        }
        for horizon in ("30m", "1h", "3h"):
            predicted = rec.get(f"predicted_regime_{horizon}")
            target = ts + timedelta(minutes=MARKET_HORIZON_MINUTES[horizon])
            actual_rec = _nearest_within(real, "timestamp", target, MARKET_HORIZON_TOLERANCE_MIN[horizon])
            actual = actual_rec.get("current_regime") if actual_rec else None
            row[f"predicted_regime_{horizon}"] = predicted
            row[f"actual_regime_{horizon}"] = actual
            row[f"actual_at_{horizon}"] = actual_rec.get("timestamp") if actual_rec else None
            row[f"match_{horizon}"] = (predicted == actual) if (predicted is not None and actual is not None) else None

        is_de_now = rec.get("current_regime") in RISK_REGIMES
        alert_flagged = (rec.get("alert_level") or "NONE") != "NONE"
        row["is_de_regime_now"] = is_de_now
        row["alert_flagged_risk"] = alert_flagged
        row["de_alert_correct"] = (alert_flagged == is_de_now) if is_de_now else None  # recall 관점: D/E일 때 경보가 켜졌는가

        # C -> D 조기감지: 현재 C이고 30분 예측이 D인 경우, 실제로 30분 이내 D로 전환됐는지
        predicted_cd = (rec.get("current_regime") == "C" and rec.get("predicted_regime_30m") == "D")
        row["predicted_cd_transition"] = predicted_cd
        if predicted_cd:
            target = ts + timedelta(minutes=30)
            actual_rec = _nearest_within(real, "timestamp", target, MARKET_HORIZON_TOLERANCE_MIN["30m"])
            if actual_rec is None:
                row["cd_transition_confirmed"] = None  # 아직 실제값 없음(대기)
            else:
                row["cd_transition_confirmed"] = actual_rec.get("current_regime") == "D"
        else:
            row["cd_transition_confirmed"] = None

        rows.append(row)

    meta = {
        "raw_count": len(raw),
        "excluded_burst_count": excluded_burst,
        "real_count": len(real),
    }
    return rows, meta


def _accuracy(rows: list[dict], key: str) -> tuple[int, int, float | None]:
    matched = [r[key] for r in rows if r.get(key) is not None]
    if not matched:
        return 0, 0, None
    correct = sum(1 for m in matched if m)
    return correct, len(matched), round(correct / len(matched) * 100, 1)


def summarize_market(rows: list[dict]) -> dict:
    summary: dict = {"horizons": {}}
    for horizon in ("30m", "1h", "3h"):
        correct, total, pct = _accuracy(rows, f"match_{horizon}")
        summary["horizons"][horizon] = {"correct": correct, "total": total, "accuracy_pct": pct}

    de_rows = [r for r in rows if r.get("is_de_regime_now")]
    de_correct = sum(1 for r in de_rows if r.get("de_alert_correct"))
    summary["de_alert"] = {
        "de_regime_instances": len(de_rows),
        "alert_correct": de_correct,
        "recall_pct": round(de_correct / len(de_rows) * 100, 1) if de_rows else None,
        "note": "실제 관측 표본에 D/E가 아닌(경보 불필요) 경우가 없어 정밀도(precision)/오탐률은 계산 불가",
    }

    cd_rows = [r for r in rows if r.get("predicted_cd_transition")]
    cd_confirmed = [r for r in cd_rows if r.get("cd_transition_confirmed") is True]
    cd_pending = [r for r in cd_rows if r.get("cd_transition_confirmed") is None]
    summary["cd_early_detection"] = {
        "predicted_instances": len(cd_rows),
        "confirmed_success": len(cd_confirmed),
        "pending_no_actual_yet": len(cd_pending),
        "success_rate_pct": round(len(cd_confirmed) / (len(cd_rows) - len(cd_pending)) * 100, 1)
        if (len(cd_rows) - len(cd_pending)) > 0 else None,
    }
    return summary


# ---------------------------------------------------------------------------
# 2) SK하이닉스 다중 horizon 가격 예측 검증
# ---------------------------------------------------------------------------

def _find_actual_intraday(predicted_at: datetime, date_str: str, horizon: str, ticks: list[dict]) -> float | None:
    minutes = HYNIX_HORIZON_MINUTES[horizon]
    tick = tick_history.find_tick_near(ticks, minutes_ago=-minutes, now=predicted_at, tolerance_min=5.0)
    return tick.get("hynix_price") if tick else None


def _find_actual_close(date_str: str, ticks: list[dict]) -> float | None:
    valid = [t for t in ticks if t.get("hynix_price") is not None]
    return valid[-1]["hynix_price"] if valid else None


def _find_actual_next_open(rec: dict, all_records: list[dict]) -> float | None:
    try:
        rec_date = datetime.strptime(rec["_date_str"], "%Y%m%d").date()
    except ValueError:
        return None
    candidates = [
        r for r in all_records
        if r.get("current_price") is not None and _safe_date(r) is not None and _safe_date(r) > rec_date
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: (r["_date_str"], r.get("logged_at") or ""))
    return candidates[0]["current_price"]


def _safe_date(rec: dict):
    try:
        return datetime.strptime(rec["_date_str"], "%Y%m%d").date()
    except Exception:
        return None


def build_hynix_backtest() -> tuple[list[dict], dict]:
    raw = _load_jsonl_dir(HYNIX_LOG_DIR)
    real, excluded_burst = _filter_test_bursts(raw, "predicted_at")
    real.sort(key=lambda r: r["predicted_at"])

    tick_cache: dict[str, list[dict]] = {}

    def _ticks_for(date_str: str) -> list[dict]:
        if date_str not in tick_cache:
            tick_cache[date_str] = tick_history.load_ticks(date_str)
        return tick_cache[date_str]

    rows: list[dict] = []
    for rec in real:
        base = rec.get("current_price") or rec.get("base_price")
        if base is None:
            continue
        predicted_at = _parse_ts(rec["predicted_at"])
        date_str = rec["_date_str"]
        ticks = _ticks_for(date_str)

        row = {
            "date": date_str,
            "predicted_at": rec["predicted_at"],
            "base_price": base,
            "base_price_source": rec.get("base_price_source"),
            "data_quality_score": rec.get("data_quality_score"),
            "holiday_mode": rec.get("holiday_mode"),
        }

        for horizon, pred_field, conf_field in [
            ("30m", "predicted_price_30m", "confidence_30m"),
            ("1h", "predicted_price_1h", "confidence_1h"),
            ("3h", "predicted_price_3h", "confidence_3h"),
        ]:
            predicted = rec.get(pred_field)
            actual = _find_actual_intraday(predicted_at, date_str, horizon, ticks) if predicted_at else None
            row[f"predicted_price_{horizon}"] = predicted
            row[f"actual_price_{horizon}"] = actual
            row[f"confidence_{horizon}"] = rec.get(conf_field)
            _fill_error_cols(row, horizon, predicted, actual, base)

        predicted_close = rec.get("predicted_close_today")
        actual_close = _find_actual_close(date_str, ticks)
        row["predicted_close_today"] = predicted_close
        row["actual_close_today"] = actual_close
        row["confidence_close"] = rec.get("confidence_close")
        _fill_error_cols(row, "close", predicted_close, actual_close, base)

        predicted_open = rec.get("predicted_open_tomorrow")
        actual_open = _find_actual_next_open(rec, real)
        row["predicted_open_tomorrow"] = predicted_open
        row["actual_open_tomorrow"] = actual_open
        row["confidence_tomorrow_open"] = rec.get("confidence_tomorrow_open")
        _fill_error_cols(row, "tomorrow_open", predicted_open, actual_open, base)

        rows.append(row)

    meta = {"raw_count": len(raw), "excluded_burst_count": excluded_burst, "real_count": len(real)}
    return rows, meta


def _fill_error_cols(row: dict, horizon: str, predicted, actual, base) -> None:
    if predicted is None or actual is None:
        row[f"abs_error_{horizon}"] = None
        row[f"abs_pct_error_{horizon}"] = None
        row[f"direction_correct_{horizon}"] = None
        return
    abs_error = abs(actual - predicted)
    row[f"abs_error_{horizon}"] = round(abs_error, 1)
    row[f"abs_pct_error_{horizon}"] = round(abs_error / predicted * 100, 3) if predicted else None
    if base:
        row[f"direction_correct_{horizon}"] = bool((actual - base) * (predicted - base) >= 0)
    else:
        row[f"direction_correct_{horizon}"] = None


def summarize_hynix(rows: list[dict]) -> dict:
    horizons = ["30m", "1h", "3h", "close", "tomorrow_open"]
    summary: dict = {"horizons": {}}
    for horizon in horizons:
        samples = [r for r in rows if r.get(f"abs_error_{horizon}") is not None]
        if not samples:
            summary["horizons"][horizon] = {"count": 0, "mae": None, "mape_pct": None, "direction_accuracy_pct": None}
            continue
        mae = sum(r[f"abs_error_{horizon}"] for r in samples) / len(samples)
        pct_samples = [r[f"abs_pct_error_{horizon}"] for r in samples if r[f"abs_pct_error_{horizon}"] is not None]
        mape = sum(pct_samples) / len(pct_samples) if pct_samples else None
        dir_samples = [r[f"direction_correct_{horizon}"] for r in samples if r.get(f"direction_correct_{horizon}") is not None]
        dir_acc = sum(1 for d in dir_samples if d) / len(dir_samples) * 100 if dir_samples else None
        summary["horizons"][horizon] = {
            "count": len(samples),
            "mae": round(mae, 1),
            "mape_pct": round(mape, 3) if mape is not None else None,
            "direction_accuracy_pct": round(dir_acc, 1) if dir_acc is not None else None,
        }
    return summary


# ---------------------------------------------------------------------------
# 부가 분석 — 날짜별/신뢰도구간별/데이터품질구간별/최악 오차 케이스
# ---------------------------------------------------------------------------

def _bucket(value: float | None, buckets: list[tuple[int, int]]) -> str:
    if value is None:
        return "unknown"
    for lo, hi in buckets:
        if lo <= value < hi:
            return f"{lo}-{min(hi, 100)}"
    return "unknown"


def per_date_market(rows: list[dict]) -> dict:
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(r)
    out = {}
    for date, drows in by_date.items():
        out[date] = {h: _accuracy(drows, f"match_{h}")[2] for h in ("30m", "1h", "3h")}
        out[date]["count"] = len(drows)
    return out


def per_date_hynix(rows: list[dict]) -> dict:
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(r)
    out = {}
    for date, drows in by_date.items():
        entry = {"count": len(drows)}
        for h in ("30m", "1h", "3h", "close", "tomorrow_open"):
            samples = [r[f"abs_pct_error_{h}"] for r in drows if r.get(f"abs_pct_error_{h}") is not None]
            entry[f"mape_{h}"] = round(sum(samples) / len(samples), 3) if samples else None
        out[date] = entry
    return out


def confidence_bucket_hynix(rows: list[dict]) -> dict:
    result: dict[str, dict] = {}
    for horizon in ("30m", "1h", "3h", "close", "tomorrow_open"):
        buckets: dict[str, list] = defaultdict(list)
        for r in rows:
            err = r.get(f"abs_pct_error_{horizon}")
            conf = r.get(f"confidence_{horizon}")
            if err is None or conf is None:
                continue
            buckets[_bucket(conf, CONFIDENCE_BUCKETS)].append(err)
        result[horizon] = {b: {"count": len(v), "mape_pct": round(sum(v) / len(v), 3)} for b, v in buckets.items()}
    return result


def quality_bucket_hynix(rows: list[dict]) -> dict:
    buckets: dict[str, list] = defaultdict(list)
    for r in rows:
        q = r.get("data_quality_score")
        err = r.get("abs_pct_error_close")
        if q is None or err is None:
            continue
        buckets[_bucket(q, QUALITY_BUCKETS)].append(err)
    return {b: {"count": len(v), "mape_pct_close": round(sum(v) / len(v), 3)} for b, v in buckets.items()}


def worst_hynix_cases(rows: list[dict], top_n: int = 5) -> list[dict]:
    scored = []
    for r in rows:
        errs = [r.get(f"abs_pct_error_{h}") for h in ("30m", "1h", "3h", "close", "tomorrow_open")]
        errs = [e for e in errs if e is not None]
        if errs:
            scored.append((max(errs), r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:top_n]]


def worst_market_cases(rows: list[dict], top_n: int = 5) -> list[dict]:
    misses = []
    for r in rows:
        wrong_horizons = [h for h in ("30m", "1h", "3h") if r.get(f"match_{h}") is False]
        if wrong_horizons:
            misses.append((len(wrong_horizons), r, wrong_horizons))
    misses.sort(key=lambda x: x[0], reverse=True)
    return [{"row": r, "wrong_horizons": wh} for _, r, wh in misses[:top_n]]


# ---------------------------------------------------------------------------
# CSV / Markdown 출력
# ---------------------------------------------------------------------------

def write_csv(rows: list[dict], path: Path) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("no_data\n", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    for r in rows[1:]:
        for k in r.keys():
            if k not in fieldnames:
                fieldnames.append(k)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    market_rows, market_meta = build_market_backtest()
    hynix_rows, hynix_meta = build_hynix_backtest()

    write_csv(market_rows, REPORTS_DIR / "backtest_market_prediction.csv")
    write_csv(hynix_rows, REPORTS_DIR / "backtest_hynix_prediction.csv")

    hynix_bias = model_calibration.compute_and_save_hynix_bias(hynix_rows)
    market_bias = model_calibration.compute_and_save_market_bias(market_rows)

    market_summary = summarize_market(market_rows)
    hynix_summary = summarize_hynix(hynix_rows)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "market": {
            "meta": market_meta, "summary": market_summary,
            "per_date": per_date_market(market_rows),
            "worst_cases": worst_market_cases(market_rows),
        },
        "hynix": {
            "meta": hynix_meta, "summary": hynix_summary,
            "per_date": per_date_hynix(hynix_rows),
            "confidence_buckets": confidence_bucket_hynix(hynix_rows),
            "quality_buckets": quality_bucket_hynix(hynix_rows),
            "worst_cases": worst_hynix_cases(hynix_rows),
        },
    }
    (REPORTS_DIR / "_backtest_raw_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps({
        "market_rows": len(market_rows), "market_excluded_burst": market_meta["excluded_burst_count"],
        "hynix_rows": len(hynix_rows), "hynix_excluded_burst": hynix_meta["excluded_burst_count"],
        "hynix_bias": hynix_bias, "market_bias": market_bias,
    }, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
