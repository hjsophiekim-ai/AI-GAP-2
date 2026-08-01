"""evaluate_hynix_predictions.py — SK하이닉스 다중 horizon 가격 예측 백테스트.

logs/hynix_prediction/YYYYMMDD.jsonl에 쌓인 예측 로그와
data/state/market_ticks/ticks_YYYYMMDD.jsonl에 쌓인 실제 시세 tick(Market
Regime Router가 5분마다 재평가할 때 기록)을 비교해 MAE/MAPE/방향적중률/
신뢰도 구간별 정확도를 계산한다.

주의: 30분/1시간/3시간 horizon은 tick history가 있어야 실제값을 찾을 수
있다 — 즉 예측이 기록된 시각 전후로 Market Regime Router(시장판단
자동매매 페이지)가 재평가를 실행했어야 한다. tick이 없으면 해당 예측은
"대기 중(actual 없음)"으로 건너뛴다(가짜 값을 채우지 않는다).

오늘종가/내일시가는 각각 그날 마지막 tick / 다음 날 첫 로그의 current_price를
실제값으로 사용한다.

사용법:
    python scripts/evaluate_hynix_predictions.py [--days N]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.market import tick_history  # noqa: E402

LOG_DIR = ROOT / "logs" / "hynix_prediction"

HORIZON_MINUTES = {"30m": 30, "1h": 60, "3h": 180}
CONFIDENCE_BUCKETS = [(0, 50), (50, 70), (70, 85), (85, 101)]


def _load_logs(days: int) -> list[dict]:
    records = []
    if not LOG_DIR.exists():
        return records
    cutoff = datetime.now() - timedelta(days=days)
    for path in sorted(LOG_DIR.glob("*.jsonl")):
        try:
            file_date = datetime.strptime(path.stem, "%Y%m%d")
        except ValueError:
            continue
        if file_date < cutoff:
            continue
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
    records.sort(key=lambda r: r.get("logged_at") or "")
    return records


def _find_actual_intraday(rec: dict, horizon: str) -> float | None:
    """예측 시각 + horizon분 후 실제 hynix_price를 tick history에서 찾는다."""
    try:
        predicted_at = datetime.fromisoformat(rec["predicted_at"])
    except Exception:
        return None
    ticks = tick_history.load_ticks(rec["_date_str"])
    if not ticks:
        return None
    minutes = HORIZON_MINUTES[horizon]
    # find_tick_near(ticks, minutes_ago, now) -> target = now - minutes_ago
    # 미래 시각(target = predicted_at + minutes)을 찾으려면 minutes_ago에 음수를 넣는다.
    tick = tick_history.find_tick_near(ticks, minutes_ago=-minutes, now=predicted_at, tolerance_min=5.0)
    if not tick:
        return None
    return tick.get("hynix_price")


def _find_actual_close(rec: dict) -> float | None:
    ticks = tick_history.load_ticks(rec["_date_str"])
    if not ticks:
        return None
    valid = [t for t in ticks if t.get("hynix_price") is not None]
    if not valid:
        return None
    return valid[-1]["hynix_price"]


def _find_actual_next_open(rec: dict, all_records: list[dict]) -> float | None:
    try:
        rec_date = datetime.strptime(rec["_date_str"], "%Y%m%d").date()
    except ValueError:
        return None
    candidates = [
        r for r in all_records
        if r.get("current_price") is not None
        and _safe_date(r) is not None and _safe_date(r) > rec_date
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


def _confidence_bucket(conf: float) -> str:
    for lo, hi in CONFIDENCE_BUCKETS:
        if lo <= conf < hi:
            return f"{lo}-{hi if hi <= 100 else 100}"
    return "unknown"


def evaluate(days: int = 30) -> dict:
    records = _load_logs(days)
    horizons = ["30m", "1h", "3h", "close_today", "open_tomorrow"]
    per_horizon: dict[str, list[dict]] = {h: [] for h in horizons}

    for rec in records:
        base = rec.get("current_price") or rec.get("base_price")
        if base is None:
            continue

        for key, pred_field, conf_field in [
            ("30m", "predicted_price_30m", "confidence_30m"),
            ("1h", "predicted_price_1h", "confidence_1h"),
            ("3h", "predicted_price_3h", "confidence_3h"),
        ]:
            predicted = rec.get(pred_field)
            if predicted is None:
                continue
            actual = _find_actual_intraday(rec, key)
            if actual is None:
                continue
            per_horizon[key].append({
                "predicted": predicted, "actual": actual, "base": base,
                "confidence": rec.get(conf_field) or 0.0,
            })

        predicted_close = rec.get("predicted_close_today")
        if predicted_close is not None:
            actual_close = _find_actual_close(rec)
            if actual_close is not None:
                per_horizon["close_today"].append({
                    "predicted": predicted_close, "actual": actual_close, "base": base,
                    "confidence": rec.get("confidence_close") or 0.0,
                })

        predicted_open = rec.get("predicted_open_tomorrow")
        if predicted_open is not None:
            actual_open = _find_actual_next_open(rec, records)
            if actual_open is not None:
                per_horizon["open_tomorrow"].append({
                    "predicted": predicted_open, "actual": actual_open, "base": base,
                    "confidence": rec.get("confidence_tomorrow_open") or 0.0,
                })

    report: dict = {"evaluated_at": datetime.now().isoformat(timespec="seconds"), "days": days, "horizons": {}}
    for horizon, samples in per_horizon.items():
        if not samples:
            report["horizons"][horizon] = {"count": 0, "message": "실제값과 매칭된 예측 없음(대기 중)"}
            continue

        abs_errors = [abs(s["actual"] - s["predicted"]) for s in samples]
        pct_errors = [abs(s["actual"] - s["predicted"]) / s["predicted"] * 100 for s in samples if s["predicted"]]
        direction_hits = [
            (s["actual"] - s["base"]) * (s["predicted"] - s["base"]) >= 0
            for s in samples if s["base"]
        ]

        bucket_stats: dict[str, dict] = {}
        for s in samples:
            b = _confidence_bucket(s["confidence"])
            bucket_stats.setdefault(b, {"count": 0, "abs_pct_errors": [], "direction_hits": 0})
            bucket_stats[b]["count"] += 1
            if s["predicted"]:
                bucket_stats[b]["abs_pct_errors"].append(abs(s["actual"] - s["predicted"]) / s["predicted"] * 100)
            if s["base"] and (s["actual"] - s["base"]) * (s["predicted"] - s["base"]) >= 0:
                bucket_stats[b]["direction_hits"] += 1

        bucket_report = {}
        for b, stats in bucket_stats.items():
            n = stats["count"]
            mape = sum(stats["abs_pct_errors"]) / len(stats["abs_pct_errors"]) if stats["abs_pct_errors"] else None
            bucket_report[b] = {
                "count": n,
                "mape_pct": round(mape, 3) if mape is not None else None,
                "direction_accuracy_pct": round(stats["direction_hits"] / n * 100, 1) if n else None,
            }

        report["horizons"][horizon] = {
            "count": len(samples),
            "mae": round(sum(abs_errors) / len(abs_errors), 1),
            "mape_pct": round(sum(pct_errors) / len(pct_errors), 3) if pct_errors else None,
            "direction_accuracy_pct": round(sum(direction_hits) / len(direction_hits) * 100, 1) if direction_hits else None,
            "confidence_buckets": bucket_report,
        }

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="SK하이닉스 다중 horizon 가격 예측 백테스트")
    parser.add_argument("--days", type=int, default=30, help="최근 N일간 로그를 평가 대상으로 함(기본 30)")
    parser.add_argument("--out", type=str, default=None, help="결과를 저장할 JSON 파일 경로(생략 시 stdout만 출력)")
    args = parser.parse_args()

    report = evaluate(days=args.days)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"\n결과 저장: {args.out}")


if __name__ == "__main__":
    main()
