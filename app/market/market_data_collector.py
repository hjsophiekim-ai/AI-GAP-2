"""
market_data_collector.py

오늘장 시장판단(Market Regime Router)에 필요한 국내/해외 데이터를 수집한다.

데이터 소스 우선순위: KIS API -> 네이버증권 -> 기존 데이터 소스 -> 캐시/전일 데이터.
개별 항목 수집 실패는 전체 파이프라인을 중단시키지 않으며, 각 항목마다
source/timestamp/success 를 meta.log 에 기록한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from app.market import naver_market_collector as nmc
from app.market import kis_market_collector as kmc
from app.market import us_market_data as umd
from app.market import tick_history
from app.utils.data_paths import CACHE_DIR as _CACHE_DIR

_ROOT = Path(__file__).resolve().parent.parent.parent

# 감시 대상 반도체 대형주
HYNIX = {"symbol": "000660", "name": "SK하이닉스"}
SAMSUNG = {"symbol": "005930", "name": "삼성전자"}
HANMI = {"symbol": "042700", "name": "한미반도체"}
DOMESTIC_SEMI_UNIVERSE = [HYNIX, SAMSUNG, HANMI]

# 해외 티커 (naver_global_stock_collector.fetch_naver_global_quote 로 조회 가능한 심볼)
_OVERSEAS_TICKERS = {
    "nasdaq": "^IXIC",
    "sp500": "^GSPC",
    "sox": "SOX",
    "micron": "MU",
    "nvidia": "NVDA",
    "amd": "AMD",
    "broadcom": "AVGO",
    "usdkrw": "USDKRW",
    "us_futures": "ES=F",  # 가능하면 수집 (S&P500 선물); 실패해도 무해
}
_OVERSEAS_OPTIONAL = {"us_futures"}

# us_realtime_bars / us_last_session 대상 (Alpaca/Polygon/Finnhub/yfinance/Naver 다중 소스)
_REALTIME_BAR_SYMBOLS = {
    "micron": "MU", "nvidia": "NVDA", "amd": "AMD", "broadcom": "AVGO", "qqq": "QQQ",
}
_LAST_SESSION_SYMBOLS = {
    "micron": "MU", "nvidia": "NVDA", "amd": "AMD", "broadcom": "AVGO",
    "sox": "^SOX", "nasdaq": "^IXIC",
}

# holiday_mode_inputs 부가 데이터 (전부 "가능하면 수집" — 실패해도 무해)
_HOLIDAY_INPUT_TICKERS = {
    "nq_futures": "NQ=F",
    "dxy": "DX-Y.NYB",
    "japan_tokyo_electron": "8035.T",
    "japan_advantest": "6857.T",
    "japan_disco": "6146.T",
    "japan_screen": "7735.T",
    "taiwan_tsmc": "TSM",
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class MarketDataCollector:
    """국내/해외 시장 데이터를 안전하게 수집한다. 실패해도 절대 예외를 던지지 않는다."""

    def __init__(self, cfg=None, kis_client=None):
        if cfg is None:
            try:
                from app.config import get_config
                cfg = get_config()
            except Exception:
                cfg = None
        self.cfg = cfg
        self._kis_client = kis_client
        self._log: list[dict] = []

    # ------------------------------------------------------------------
    def _kis(self):
        if self._kis_client is not None:
            return self._kis_client
        mode = "mock"
        try:
            mode = self.cfg.mode if self.cfg else "mock"
        except Exception:
            pass
        self._kis_client = kmc.get_kis_client(mode)
        return self._kis_client

    def _record(self, field: str, source: str, success: bool, error: Optional[str] = None) -> None:
        self._log.append({
            "field": field, "source": source, "success": success,
            "error": error, "timestamp": _now_iso(),
        })

    def _cache_path(self, key: str) -> Path:
        return _CACHE_DIR / f"market_regime_last_{key}.json"

    def _load_last_cache(self, key: str) -> Optional[dict]:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _save_cache(self, key: str, data: dict) -> None:
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(self._cache_path(key), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.debug("[MarketDataCollector] 캐시 저장 실패(%s): %s", key, exc)

    def _with_fallback(self, key: str, fetch_fn) -> dict:
        """fetch_fn() 이 실패하면 캐시 사용, 그것도 없으면 success=False 반환."""
        try:
            data = fetch_fn()
        except Exception as exc:
            logger.warning("[MarketDataCollector] %s 수집 예외: %s", key, exc)
            data = {"success": False, "error": str(exc)}

        if data.get("success"):
            self._save_cache(key, data)
            self._record(key, data.get("source", "unknown"), True)
            return data

        cached = self._load_last_cache(key)
        if cached:
            cached = dict(cached)
            cached["stale"] = True
            cached["source"] = f"{cached.get('source', 'cache')}_cache"
            self._record(key, cached.get("source", "cache"), False, "fallback_to_cache")
            return cached

        self._record(key, data.get("source", "unknown"), False, data.get("error"))
        return data

    # ------------------------------------------------------------------
    # Domestic
    # ------------------------------------------------------------------

    def _collect_domestic_semi_stock(self, meta: dict) -> dict:
        symbol, name = meta["symbol"], meta["name"]
        kis = self._kis()

        def _fetch():
            snap = kmc.fetch_stock_snapshot(symbol, name=name, kis_client=kis)
            if snap.get("success"):
                return snap
            from app.data.naver_stock_collector import fetch_naver_current_price
            naver_snap = fetch_naver_current_price(symbol)
            if naver_snap.get("status") == "success":
                return {
                    "symbol": symbol, "name": name,
                    "current_price": naver_snap["current_price"],
                    "open": None, "high": None, "low": None, "prev_close": None,
                    "change_rate": None, "volume": None, "trade_value": None,
                    "source": "naver", "timestamp": _now_iso(), "success": True, "error": None,
                }
            return {"success": False, "error": "kis_and_naver_failed", "source": "none"}

        result = self._with_fallback(f"stock_{symbol}", _fetch)

        # 최근 등락률 (전일/2일 누적) — 반등장 판단용, 실패해도 무시
        try:
            recent = kmc.fetch_recent_daily_returns(symbol, kis_client=kis, days=3)
            result["day1_return"] = recent.get("day1_return")
            result["day2_cum_return"] = recent.get("day2_cum_return")
        except Exception:
            result["day1_return"] = None
            result["day2_cum_return"] = None

        # VWAP = 누적거래대금 / 누적거래량 (당일 시가부터의 평균 체결가 근사).
        # KIS/Naver 모두 trade_value/volume을 이미 수집하므로 별도 API 호출 없이 계산 가능.
        try:
            tv = result.get("trade_value")
            vol = result.get("volume")
            result["vwap"] = round(tv / vol, 1) if tv and vol else None
        except (TypeError, ZeroDivisionError):
            result["vwap"] = None
        return result

    def _collect_market_investor_flow(self) -> dict:
        """외국인/기관 순매수 프록시.

        실제 KOSPI200 선물 외국인 순매수는 무료로 안정적인 실시간 소스가 없어,
        반도체 대형주(하이닉스+삼성전자) 개별종목 외국인/기관 순매수 합계를
        시장 수급 방향의 대리지표로 사용한다(한계는 최종 보고서에 명시).
        프로그램매매 순매수는 연동된 무료 소스가 없어 항상 success=False.
        """
        try:
            from app.data_sources.naver_investor_flow import fetch_naver_investor_flow
            hynix_flow = fetch_naver_investor_flow("000660")
            samsung_flow = fetch_naver_investor_flow("005930")
            ok = hynix_flow.get("status") == "success" or samsung_flow.get("status") == "success"
            foreign_sum = sum(
                v for v in (hynix_flow.get("foreign_net_buy"), samsung_flow.get("foreign_net_buy")) if v is not None
            ) if ok else None
            inst_sum = sum(
                v for v in (hynix_flow.get("institution_net_buy"), samsung_flow.get("institution_net_buy")) if v is not None
            ) if ok else None
            self._record("investor_flow_market", "naver_proxy", ok)
            return {
                "foreign_net_buy_sum": foreign_sum,
                "institution_net_buy_sum": inst_sum,
                "program_net_buy": None,
                "is_proxy": True,
                "proxy_basis": "hynix+samsung individual stock flow (not true index futures flow)",
                "success": ok,
                "source": "naver_proxy",
            }
        except Exception as exc:
            logger.debug("[MarketDataCollector] 시장 수급 프록시 수집 실패: %s", exc)
            self._record("investor_flow_market", "naver_proxy", False, str(exc))
            return {
                "foreign_net_buy_sum": None, "institution_net_buy_sum": None, "program_net_buy": None,
                "is_proxy": True, "success": False, "source": "none", "error": str(exc),
            }

    def _collect_news_shock(self) -> dict:
        try:
            from app.data_sources.hynix_news_momentum import compute_news_momentum_score
            result = compute_news_momentum_score("반도체")
            self._record("news_shock", result.get("source", "naver_news"), bool(result.get("success")))
            return result
        except Exception as exc:
            logger.debug("[MarketDataCollector] 뉴스 모멘텀 수집 실패(선택): %s", exc)
            self._record("news_shock", "none", False, str(exc))
            return {"score": None, "success": False, "source": "none", "error": str(exc)}

    def _collect_sector_theme_rates(self, tv_top50: list[dict]) -> tuple[dict, dict, list[dict]]:
        """거래대금 상위 종목 기준 업종별/서브테마별 평균 상승률 + 섹터가 부여된 목록."""
        try:
            from app.strategy.sector_mapper import SectorMapper
            from app.strategy.sector_strength_analyzer import SectorStrengthAnalyzer
            mapper = SectorMapper()
            classified = mapper.classify_stocks(tv_top50)
            analyzer = SectorStrengthAnalyzer()
            sector_analysis = analyzer.analyze(classified)
            sector_rates = {
                sec: info.get("sector_avg_change_rate", 0.0)
                for sec, info in sector_analysis.items()
            }
            theme_totals: dict[str, list[float]] = {}
            for s in classified:
                sub = s.get("subtheme", "")
                if sub:
                    theme_totals.setdefault(sub, []).append(s.get("change_rate", 0.0))
            theme_rates = {k: sum(v) / len(v) for k, v in theme_totals.items()}
            self._record("sector_theme_rates", "internal_analyzer", True)
            return sector_rates, theme_rates, classified
        except Exception as exc:
            logger.warning("[MarketDataCollector] 업종/테마 상승률 계산 실패: %s", exc)
            self._record("sector_theme_rates", "internal_analyzer", False, str(exc))
            return {}, {}, tv_top50

    def _collect_domestic(self) -> dict:
        domestic: dict = {}

        domestic["kospi"] = self._with_fallback("kospi", lambda: nmc.fetch_index_snapshot("KOSPI"))
        domestic["kosdaq"] = self._with_fallback("kosdaq", lambda: nmc.fetch_index_snapshot("KOSDAQ"))
        domestic["kospi200_futures"] = self._with_fallback(
            "kospi200_futures", nmc.fetch_kospi200_futures_proxy
        )

        adv = domestic["kospi"].get("advancers") or 0
        dec = domestic["kospi"].get("decliners") or 0
        adv += domestic["kosdaq"].get("advancers") or 0
        dec += domestic["kosdaq"].get("decliners") or 0
        domestic["advancers"] = adv
        domestic["decliners"] = dec

        try:
            from app.data.naver_nxt_turnover_collector import collect_nxt_turnover_stocks
            tv_top50 = collect_nxt_turnover_stocks(max_pages=5, max_stocks=50)
            self._record("trading_value_top50", "naver_nxt_turnover", bool(tv_top50))
        except Exception as exc:
            logger.warning("[MarketDataCollector] 거래대금 Top50 수집 실패: %s", exc)
            tv_top50 = []
            self._record("trading_value_top50", "naver_nxt_turnover", False, str(exc))
        domestic["trading_value_top50"] = tv_top50

        try:
            from app.data.naver_volume_spike_collector import collect_volume_spike_stocks
            cr_top50 = collect_volume_spike_stocks(max_pages=3, max_stocks=50)
            self._record("change_rate_top50", "naver_volume_spike", bool(cr_top50))
        except Exception as exc:
            logger.warning("[MarketDataCollector] 상승률 Top50 수집 실패: %s", exc)
            cr_top50 = []
            self._record("change_rate_top50", "naver_volume_spike", False, str(exc))
        domestic["change_rate_top50"] = cr_top50

        sector_rates, theme_rates, classified_top50 = self._collect_sector_theme_rates(tv_top50 or cr_top50)
        domestic["sector_change_rates"] = sector_rates
        domestic["theme_change_rates"] = theme_rates
        if classified_top50:
            # sector/subtheme 필드가 채워진 버전으로 교체 (기존 필드는 그대로 유지, 추가만 됨)
            domestic["trading_value_top50"] = classified_top50

        try:
            from app.data_sources.naver_investor_flow import fetch_naver_investor_flow
            flow = fetch_naver_investor_flow(HYNIX["symbol"])
            domestic["investor_flow"] = flow
            self._record("investor_flow", "naver", bool(flow))
        except Exception as exc:
            logger.debug("[MarketDataCollector] 수급 데이터 수집 실패(선택): %s", exc)
            domestic["investor_flow"] = {}
            self._record("investor_flow", "naver", False, str(exc))

        # 시장 전체(대리) 수급 — 외국인/기관/프로그램 순매수 프록시
        domestic["investor_flow_market"] = self._collect_market_investor_flow()

        # 뉴스 모멘텀 (반도체/미중/관세/금리/환율/실적 등 키워드 기반, 실패해도 무해)
        domestic["news_shock"] = self._collect_news_shock()

        domestic["hynix"] = self._collect_domestic_semi_stock(HYNIX)
        domestic["samsung"] = self._collect_domestic_semi_stock(SAMSUNG)
        domestic["hanmi"] = self._collect_domestic_semi_stock(HANMI)

        # 섹터별 거래대금 분포 (Top50 기준, sector 필드 포함된 classified 목록 사용)
        domestic["sector_tv_distribution"] = self._compute_sector_tv_distribution(domestic["trading_value_top50"])

        return domestic

    def _compute_sector_tv_distribution(self, tv_top50: list[dict]) -> dict:
        """섹터별 거래대금 합계 분포 (Top50 기준)."""
        dist: dict[str, float] = {}
        try:
            from app.strategy.sector_mapper import SectorMapper
            mapper = SectorMapper()
            for s in tv_top50:
                sec = s.get("sector") or mapper.get_sector(s.get("symbol", ""), s.get("name", ""))
                dist[sec] = dist.get(sec, 0.0) + (s.get("trading_value", 0) or 0)
        except Exception as exc:
            logger.debug("[MarketDataCollector] 섹터 거래대금 분포 계산 실패: %s", exc)
        return dist

    # ------------------------------------------------------------------
    # Overseas
    # ------------------------------------------------------------------

    def _collect_us_market_status(self) -> dict:
        try:
            status = umd.get_us_market_status()
            self._record("us_market_status", status.get("source", "unknown"), True)
            return status
        except Exception as exc:
            logger.warning("[MarketDataCollector] 미국장 상태 판단 실패: %s", exc)
            self._record("us_market_status", "none", False, str(exc))
            return {
                "is_us_market_open": False, "is_us_holiday": False, "is_us_early_close": False,
                "last_us_trading_day": None, "session": "unknown",
                "source": "none", "timestamp": _now_iso(), "confidence": 0.0,
            }

    def _collect_us_realtime_bars(self, market_open: bool) -> dict:
        bars: dict = {}
        for key, symbol in _REALTIME_BAR_SYMBOLS.items():
            data = self._with_fallback(
                f"realtime_bar_{key}",
                lambda s=symbol: umd.fetch_us_realtime_bar(s, market_open=market_open),
            )
            bars[key] = data
        return bars

    def _collect_us_last_session(self) -> dict:
        sessions: dict = {}
        for key, symbol in _LAST_SESSION_SYMBOLS.items():
            data = self._with_fallback(f"last_session_{key}", lambda s=symbol: umd.fetch_us_last_session(s))
            sessions[key] = data
        return sessions

    def _collect_holiday_mode_inputs(self) -> dict:
        inputs: dict = {}
        for key, ticker in _HOLIDAY_INPUT_TICKERS.items():
            data = self._with_fallback(f"holiday_input_{key}", lambda t=ticker: umd.fetch_optional_quote(t))
            data["optional"] = True
            inputs[key] = data
        return inputs

    def _collect_overseas(self) -> dict:
        overseas: dict = {}
        try:
            from app.data.naver_global_stock_collector import fetch_naver_global_quote
        except Exception as exc:
            logger.warning("[MarketDataCollector] 해외 수집 모듈 로드 실패: %s", exc)
            fetch_naver_global_quote = None

        us_status = self._collect_us_market_status()
        overseas["us_market_status"] = us_status
        market_open = bool(us_status.get("is_us_market_open"))

        realtime_bars = self._collect_us_realtime_bars(market_open)
        overseas["us_realtime_bars"] = realtime_bars
        overseas["us_last_session"] = self._collect_us_last_session()
        overseas["holiday_mode_inputs"] = self._collect_holiday_mode_inputs()

        for key, ticker in _OVERSEAS_TICKERS.items():
            # micron/nvidia/amd/broadcom은 us_realtime_bars(다중소스)를 우선 사용하고,
            # 실패 시에만 기존 네이버/야후 경로로 fallback한다.
            rt = realtime_bars.get(key)
            if rt and rt.get("success") and rt.get("latest_price"):
                overseas[key] = {
                    "value": rt["latest_price"], "change_rate": rt.get("latest_change_pct"),
                    "source": rt.get("source", "unknown"), "timestamp": rt.get("timestamp") or _now_iso(),
                    "success": True, "error": None,
                    "freshness_seconds": rt.get("freshness_seconds"), "is_stale": rt.get("is_stale", False),
                    "data_gap_reason": rt.get("data_gap_reason", "NORMAL"),
                }
                continue

            if fetch_naver_global_quote is None:
                overseas[key] = {"success": False, "error": "module_unavailable"}
                self._record(key, "none", False, "module_unavailable")
                continue

            def _fetch(t=ticker):
                q = fetch_naver_global_quote(t)
                return {
                    "value": q.get("price"),
                    "change_rate": q.get("return_pct"),
                    "source": q.get("source", "unknown"),
                    "timestamp": _now_iso(),
                    "success": q.get("status") == "success",
                    "error": q.get("error"),
                }

            data = self._with_fallback(key, _fetch)
            if key in _REALTIME_BAR_SYMBOLS and not market_open and not data.get("success"):
                # 휴장일에 실시간 소스가 전부 실패해도 last_session 데이터로 대체 가능하므로
                # 여기서는 오류로 취급하지 않는다 (regime_features가 last_session을 사용).
                data["data_gap_reason"] = "US_HOLIDAY" if us_status.get("is_us_holiday") else (
                    "WEEKEND" if us_status.get("is_us_weekend") else "EARLY_CLOSE_OR_CLOSED"
                )
            if key in _OVERSEAS_OPTIONAL and not data.get("success"):
                data["optional"] = True
            overseas[key] = data
        return overseas

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def collect(self, record_tick: bool = True) -> dict:
        self._log = []
        domestic = self._collect_domestic()
        overseas = self._collect_overseas()

        total = len(self._log)
        ok = sum(1 for e in self._log if e["success"])
        data_quality_ratio = (ok / total) if total else 0.0

        snapshot = {
            "domestic": domestic,
            "overseas": overseas,
            "meta": {
                "collected_at": _now_iso(),
                "log": self._log,
                "data_quality_ratio": round(data_quality_ratio, 3),
                "total_fields": total,
                "success_fields": ok,
            },
        }

        # 5분/15분 등 시계열 델타 계산을 위해 이번 tick을 기록하고, 과거 tick과
        # 비교한 델타를 snapshot에 첨부한다 (regime_features의 예측 점수들이 사용).
        if record_tick:
            try:
                ticks_before = tick_history.load_ticks()
                current_tick = tick_history.append_tick(snapshot)
                snapshot["deltas"] = self._compute_deltas(current_tick, ticks_before)
            except Exception as exc:
                logger.debug("[MarketDataCollector] tick history 처리 실패: %s", exc)
                snapshot["deltas"] = {}
        else:
            snapshot["deltas"] = {}

        logger.info(
            "[MarketDataCollector] 수집 완료: %d/%d 필드 성공 (품질 %.0f%%)",
            ok, total, data_quality_ratio * 100,
        )
        return snapshot

    @staticmethod
    def _compute_deltas(current_tick: dict, past_ticks: list[dict]) -> dict:
        fields = [
            "kospi_change_rate", "kosdaq_change_rate", "kospi200_futures_change_rate",
            "usdkrw_value", "nasdaq_futures_change_rate", "nasdaq_change_rate",
            "advancers", "decliners", "hynix_price", "samsung_price", "hanmi_price",
            "foreign_net_buy_proxy", "institution_net_buy_proxy", "leader_sector_tv_sum",
        ]
        deltas: dict = {}
        for horizon_label, minutes in (("5m", 5), ("15m", 15)):
            deltas[horizon_label] = {
                f: tick_history.compute_delta(current_tick.get(f), past_ticks, f, minutes)
                for f in fields
            }
        return deltas
