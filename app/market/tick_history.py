"""
tick_history.py

Market Regime Router가 매번(수동 재실행 또는 5분 자동 재평가) 호출될 때마다
핵심 지표를 얇게 추려 시계열로 남긴다. 이 시계열을 이용해 5분/15분 변화율
(선물, 환율, VWAP, 수급, breadth 등)을 계산한다.

독립 앱이 아니라 market_data_collector.py / regime_features.py 에서만
호출되는 작은 유틸리티 모듈이다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_TICK_DIR = _ROOT / "data" / "state" / "market_ticks"


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _tick_path(date_str: str = None) -> Path:
    return _TICK_DIR / f"ticks_{date_str or _today()}.jsonl"


def make_tick_summary(snapshot: dict) -> dict:
    """snapshot에서 델타 계산에 필요한 핵심 지표만 뽑아 얇은 dict로 만든다."""
    domestic = snapshot.get("domestic", {})
    overseas = snapshot.get("overseas", {})

    def _v(node, key="change_rate"):
        return (node or {}).get(key)

    hynix = domestic.get("hynix", {})
    samsung = domestic.get("samsung", {})
    hanmi = domestic.get("hanmi", {})

    sector_rates = domestic.get("sector_change_rates", {}) or {}
    top3_sectors = [s for s, _ in sorted(sector_rates.items(), key=lambda x: x[1], reverse=True)[:3]]
    tv_top50 = domestic.get("trading_value_top50", []) or []
    top3_tv_sum = sum(
        s.get("trading_value", 0) for s in tv_top50
        if s.get("sector") in top3_sectors
    )

    flow = domestic.get("investor_flow_market", {}) or {}

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "kospi_change_rate": _v(domestic.get("kospi")),
        "kosdaq_change_rate": _v(domestic.get("kosdaq")),
        "kospi200_futures_change_rate": _v(domestic.get("kospi200_futures")),
        "usdkrw_value": (overseas.get("usdkrw") or {}).get("value"),
        "usdkrw_change_rate": _v(overseas.get("usdkrw")),
        "nasdaq_futures_change_rate": _v(overseas.get("us_futures")),
        "nasdaq_change_rate": _v(overseas.get("nasdaq")),
        "advancers": domestic.get("advancers"),
        "decliners": domestic.get("decliners"),
        "hynix_price": hynix.get("current_price"),
        "hynix_vwap": hynix.get("vwap"),
        "samsung_price": samsung.get("current_price"),
        "samsung_vwap": samsung.get("vwap"),
        "hanmi_price": hanmi.get("current_price"),
        "hanmi_vwap": hanmi.get("vwap"),
        "foreign_net_buy_proxy": flow.get("foreign_net_buy_sum"),
        "institution_net_buy_proxy": flow.get("institution_net_buy_sum"),
        "leader_sectors": top3_sectors,
        "leader_sector_tv_sum": top3_tv_sum,
    }


def append_tick(snapshot: dict, date_str: str = None) -> dict:
    """이번 tick 요약을 시계열 파일에 append하고, 그 요약 dict를 반환한다."""
    summary = make_tick_summary(snapshot)
    try:
        _TICK_DIR.mkdir(parents=True, exist_ok=True)
        with open(_tick_path(date_str), "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("[TickHistory] tick 저장 실패: %s", exc)
    return summary


def load_ticks(date_str: str = None) -> list[dict]:
    """오늘 저장된 모든 tick을 시간순으로 로드한다. 실패 시 빈 리스트."""
    path = _tick_path(date_str)
    if not path.exists():
        return []
    ticks = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ticks.append(json.loads(line))
                except Exception:
                    continue
    except Exception as exc:
        logger.debug("[TickHistory] tick 로드 실패: %s", exc)
        return []
    return ticks


def find_tick_near(ticks: list[dict], minutes_ago: int, now: datetime = None, tolerance_min: float = 2.5) -> Optional[dict]:
    """minutes_ago분 전과 가장 가까운(허용오차 이내) tick을 찾는다."""
    if not ticks:
        return None
    now = now or datetime.now()
    target = now - timedelta(minutes=minutes_ago)
    best = None
    best_diff = None
    for t in ticks:
        try:
            ts = datetime.fromisoformat(t["timestamp"])
        except Exception:
            continue
        diff = abs((ts - target).total_seconds())
        if diff <= tolerance_min * 60 and (best_diff is None or diff < best_diff):
            best = t
            best_diff = diff
    return best


def _score_tick_path(date_str: str = None) -> Path:
    return _TICK_DIR / f"score_ticks_{date_str or _today()}.jsonl"


def append_score_tick(scores: dict, date_str: str = None) -> dict:
    """regime_router가 계산한 위험/회복 점수(market_collapse_score 등)를 별도
    시계열로 남긴다. append_tick()의 원본 snapshot 델타 계산과는 무관하게,
    "점수 자체의 5분/15분 변화"(관성 편향 완화용)를 계산하기 위한 전용 이력이다.
    """
    summary = {"timestamp": datetime.now().isoformat(timespec="seconds"), **scores}
    try:
        _TICK_DIR.mkdir(parents=True, exist_ok=True)
        with open(_score_tick_path(date_str), "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.warning("[TickHistory] score tick 저장 실패: %s", exc)
    return summary


def load_score_ticks(date_str: str = None) -> list[dict]:
    path = _score_tick_path(date_str)
    if not path.exists():
        return []
    ticks = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ticks.append(json.loads(line))
                except Exception:
                    continue
    except Exception as exc:
        logger.debug("[TickHistory] score tick 로드 실패: %s", exc)
        return []
    return ticks


def compute_delta(current_value: Optional[float], ticks: list[dict], field: str, minutes_ago: int, now: datetime = None) -> Optional[float]:
    """current_value - (minutes_ago분 전 field 값). 과거 tick이 없으면 None."""
    if current_value is None:
        return None
    past_tick = find_tick_near(ticks, minutes_ago, now=now)
    if not past_tick:
        return None
    past_value = past_tick.get(field)
    if past_value is None:
        return None
    try:
        return round(float(current_value) - float(past_value), 4)
    except (TypeError, ValueError):
        return None
