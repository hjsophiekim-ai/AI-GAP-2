"""
auto_trader.py

전체 파이프라인 오케스트레이터 (1 tick 실행 단위):

  Market Regime Router -> Policy Selector -> 후보 종목 선정 -> Entry 조건 확인
  -> 매수/승인 -> Position Guard 자동매도

기존 구조와 동일하게 상시 실행되는 스케줄러는 없으며, Streamlit 페이지(또는
CLI 루프)가 run_once()를 주기적으로 호출하는 방식이다.

절대 원칙: position_guard.evaluate_and_execute()는 entry_mode(AUTO/
MANUAL_APPROVAL)와 무관하게 매 tick 반드시 호출된다 — 수동매수 보호.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from app.market.regime_router import MarketRegimeRouter
from app.market.policy_selector import select_policy
from app.market.market_data_collector import MarketDataCollector
from app.strategy.policy_base import get_policy_module
from app.execution.risk_manager import RiskManager
from app.execution.order_executor import OrderExecutor
from app.execution.position_guard import PositionGuard, GuardedPosition
from app.execution.price_watcher import PriceWatcher
from app.execution.manual_approval import ManualApprovalQueue

DEFAULT_ENTRY_START = "09:25"
DEFAULT_ENTRY_END = "09:45"
DEFAULT_OBSERVE_END = "09:20"

# 실행 직전(주문 바로 전) 2차 안전장치 — policy_selector의 1차 게이트(65/70/70)
# 보다 의도적으로 더 보수적인 값(75)을 쓴다. 두 단계 임계값이 다른 것은 실수가
# 아니라 "정책 선택 단계에서 한 번, 실제 주문 직전에 한 번 더" 이중 확인이다.
DEFAULT_PREDICTED_DOWN_1H_MANUAL_ONLY = 70.0
DEFAULT_MARKET_COLLAPSE_HARD_BLOCK = 75.0
DEFAULT_SEMICONDUCTOR_COLLAPSE_HARD_BLOCK = 75.0


def _now_hm() -> str:
    return datetime.now().strftime("%H:%M")


def _explain_recommendation(policy_name: str, diag: dict) -> str:
    """정책별 diag 딕셔너리를 사람이 읽을 수 있는 한글 사유로 변환한다."""
    diag = diag or {}

    if policy_name == "policy_gap_support":
        if diag.get("reason"):
            return f"GAP Top15 후보 생성 실패({diag.get('gap15_source', '-')} 소스): {diag['reason']}"
        leader_sectors = diag.get("leader_sectors")
        if leader_sectors is not None and not leader_sectors:
            return "주도섹터 정보를 찾지 못해 GAP∩주도섹터 교집합을 계산할 수 없었습니다."
        if diag.get("intersection_count", None) == 0:
            return (
                f"GAP Top15 {diag.get('gap15_count', 0)}개 중 주도섹터"
                f"({', '.join(leader_sectors or [])})와 겹치는 종목이 없었습니다."
            )
        return "GAP 보조정책 조건을 만족하는 종목이 없었습니다."

    if policy_name == "policy_semiconductor_rebound":
        return (
            f"평가 {diag.get('candidates_evaluated', 0)}종목 중 "
            f"09:20 저점 이탈 {diag.get('hard_excluded', 0)}개 제외, "
            f"미국 반도체 지표 반등 {diag.get('us_rebound_count', 0)}/3 — "
            "반등 스코어 기준(40점) 미달로 후보 없음."
        )

    if policy_name == "policy_inverse":
        return diag.get("reason", "E 유형이 아니거나 인버스 신규진입 시간이 지나 후보가 없습니다.")

    if policy_name == "policy_no_trade":
        return diag.get("reason", "현재 시장유형/리스크 상태는 신규매수 금지 정책입니다.")

    if policy_name == "policy_leader_top3":
        if diag.get("reason"):
            return diag["reason"]
        return (
            f"주도섹터 Top3 조건을 만족하는 종목이 없었습니다 "
            f"(평가 {diag.get('candidates_evaluated', 0)}개, 하드제외 {diag.get('hard_excluded', 0)}개)."
        )

    return "추천 종목을 찾지 못했습니다."


class AutoTrader:
    def __init__(
        self,
        broker,
        cfg=None,
        market_cfg: dict = None,
        trading_cfg: dict = None,
        kis_client=None,
        risk_manager: RiskManager = None,
        position_guard: PositionGuard = None,
        price_watcher: PriceWatcher = None,
        manual_approval: ManualApprovalQueue = None,
        order_executor: OrderExecutor = None,
    ):
        self.broker = broker
        self.cfg = cfg
        self.market_cfg = market_cfg or {}
        self.trading_cfg = trading_cfg or {}
        self.kis_client = kis_client

        self.order_executor = order_executor or OrderExecutor(broker, cfg=cfg)
        self.risk_manager = risk_manager or RiskManager(cfg=cfg)
        self.position_guard = position_guard or PositionGuard(
            self.order_executor, cfg=self.trading_cfg.get("exit_rules", {}), risk_manager=self.risk_manager
        )
        self.price_watcher = price_watcher or PriceWatcher(kis_client=kis_client)
        self.manual_approval = manual_approval or ManualApprovalQueue(
            self.order_executor, self.position_guard, cfg=self.trading_cfg
        )
        self.regime_router = MarketRegimeRouter(cfg=cfg, market_cfg=self.market_cfg)

    # ------------------------------------------------------------------
    def _entry_window_open(self, now_hm: str) -> tuple[bool, str]:
        start = self.market_cfg.get("entry_start_time", DEFAULT_ENTRY_START)
        end = self.market_cfg.get("entry_end_time", DEFAULT_ENTRY_END)
        observe_end = self.market_cfg.get("observe_end_time", DEFAULT_OBSERVE_END)
        if now_hm < observe_end:
            return False, f"관찰 구간({observe_end} 이전) — 신규매수 금지"
        if now_hm < start:
            return False, f"시장유형 확정 대기 중 ({start} 부터 매수 가능)"
        if now_hm >= end:
            return False, f"신규매수 가능시간 종료({end} 이후)"
        return True, ""

    # ------------------------------------------------------------------
    def run_once(self, snapshot: dict = None) -> dict:
        now_hm = _now_hm()
        summary: dict = {"now": now_hm}

        # 1. 시장유형 판단
        regime_result = self.regime_router.determine_regime(now_hm=now_hm, snapshot=snapshot)
        summary["regime_result"] = regime_result

        # 2. 정책 선택
        risk_state = self.risk_manager.get_state()
        policy_cfg = dict(self.trading_cfg)
        policy_cfg.setdefault("confidence_threshold", self.market_cfg.get("confidence_threshold", 60))
        policy_selection = select_policy(regime_result, risk_state=risk_state, now_hm=now_hm, policy_cfg=policy_cfg)
        summary["policy_selection"] = policy_selection

        # 3. 기존 포지션 동기화 (KIS 잔고에서 미감시 포지션 발견)
        self.position_guard.sync_from_broker(self.broker)

        # 4. 현재가 조회 (보유 포지션)
        open_positions = self.position_guard.get_open_positions()
        symbols = [p.symbol for p in open_positions]
        current_prices = self.price_watcher.get_prices(symbols) if symbols else {}

        # 5. 자동매도 평가 — entry_mode와 무관하게 항상 실행 (수동매수 보호 핵심)
        # alert_level=CRITICAL이면 뚜렷한 수익이 아닌 포지션까지 방어적으로 재평가한다.
        alert_level = regime_result.get("alert_level", "NONE")
        guard_actions = self.position_guard.evaluate_and_execute(
            current_prices, now_hm=now_hm, regime=regime_result.get("regime", ""),
            data_error_streak=self.price_watcher.consecutive_failures,
            alert_level=alert_level,
        )
        summary["guard_actions"] = guard_actions
        summary["alert_level"] = alert_level

        # 6. 신규매수 판단
        entry_open, entry_block_reason = self._entry_window_open(now_hm)
        can_open, position_block_reason = self.risk_manager.can_open_new_position(
            current_position_count=len(self.position_guard.get_open_positions()),
            max_positions=self.trading_cfg.get("max_positions", 3),
            max_daily_trades=self.trading_cfg.get("max_daily_trades", 3),
        )
        data_blocked = self.price_watcher.should_block_new_entries()

        allow_entry = (
            entry_open and can_open and not data_blocked
            and policy_selection.allow_new_entry and regime_result.get("is_confirmed", False)
        )
        summary["allow_new_entry"] = allow_entry
        summary["entry_block_reasons"] = [
            r for r in [
                None if entry_open else entry_block_reason,
                None if can_open else position_block_reason,
                "현재가 데이터 오류 누적 — 신규매수 금지" if data_blocked else None,
                None if regime_result.get("is_confirmed") else "시장유형 미확정(09:20 이전)",
            ] + list(policy_selection.block_reasons)
            if r
        ]
        summary["candidates"] = []
        summary["pending_approvals"] = []

        if allow_entry:
            policy_module = get_policy_module(policy_selection.policy_name)
            market_ctx = {
                "snapshot": regime_result.get("snapshot") or (snapshot or {}),
                "regime_result": regime_result,
                "kis_client": self.kis_client,
                "exit_cfg": self.trading_cfg.get("exit_rules", {}),
            }
            candidates, policy_diag = policy_module.generate_candidates(market_ctx, self.cfg)
            summary["policy_diag"] = policy_diag

            remaining_slots = self.trading_cfg.get("max_positions", 3) - len(self.position_guard.get_open_positions())
            candidates = candidates[:max(0, remaining_slots)]
            summary["candidates"] = candidates

            entry_mode = self.trading_cfg.get("entry_mode", "MANUAL_APPROVAL")
            if policy_selection.manual_approval_only and entry_mode == "AUTO":
                logger.info(
                    "[AutoTrader] Holiday Mode 등의 사유로 이번 tick은 자동매수를 강제로 "
                    "수동승인 모드로 전환합니다: %s", policy_selection.block_reasons,
                )
                entry_mode = "MANUAL_APPROVAL"
            summary["entry_mode_effective"] = entry_mode

            if entry_mode == "AUTO":
                buy_results = self.auto_buy_all(candidates, regime_result=regime_result)
                summary["buy_results"] = buy_results
            else:
                pending = self.manual_approval.propose(
                    candidates,
                    quantities={c.symbol: self._calc_quantity(c.entry_price) for c in candidates},
                )
                summary["pending_approvals"] = pending

        return summary

    # ------------------------------------------------------------------
    # 분리형 UI 액션 — ① 시장판단 / ② 종목추천 / ③ 매수·매도(자동/수동 각각)
    # ------------------------------------------------------------------

    def determine_market(self, snapshot: dict = None, now_hm: str = None) -> dict:
        """① 시장판단만 수행 (후보 생성/주문 없음)."""
        now_hm = now_hm or _now_hm()
        regime_result = self.regime_router.determine_regime(now_hm=now_hm, snapshot=snapshot)
        risk_state = self.risk_manager.get_state()
        policy_cfg = dict(self.trading_cfg)
        policy_cfg.setdefault("confidence_threshold", self.market_cfg.get("confidence_threshold", 60))
        policy_selection = select_policy(regime_result, risk_state=risk_state, now_hm=now_hm, policy_cfg=policy_cfg)
        return {"regime_result": regime_result, "policy_selection": policy_selection}

    def recommend_candidates(self, regime_result: dict, policy_selection=None) -> dict:
        """
        ② 종목추천.

        시장유형 고유 정책(A~F의 "자연" 정책, regime_result["policy_name"])을
        먼저 시도한다 — policy_selection.policy_name은 09:45 마감·리스크 한도
        등으로 이미 policy_no_trade로 강등되어 있을 수 있으므로 추천 화면에는
        쓰지 않는다(실제 매수 허용 여부는 ①의 allow_new_entry로 별도 표시됨).

        후보가 0개면 사유를 diag/reason_kr에 남기고 주도섹터 Top3로 폴백한다.
        """
        from app.market.regime_rules import REGIME_POLICY_MAP

        natural_policy_name = regime_result.get("policy_name") or REGIME_POLICY_MAP.get(
            regime_result.get("regime", "F"), "policy_no_trade"
        )

        market_ctx = {
            "snapshot": regime_result.get("snapshot") or {},
            "regime_result": regime_result,
            "kis_client": self.kis_client,
            "exit_cfg": self.trading_cfg.get("exit_rules", {}),
        }
        policy_module = get_policy_module(natural_policy_name)
        try:
            candidates, diag = policy_module.generate_candidates(market_ctx, self.cfg)
        except Exception as exc:
            logger.error("[AutoTrader] %s 후보 생성 예외: %s", natural_policy_name, exc)
            candidates, diag = [], {"policy": natural_policy_name, "reason": f"예외 발생: {exc}"}
        diag["attempted_policy"] = natural_policy_name
        diag["reason_kr"] = _explain_recommendation(natural_policy_name, diag)

        fallback_used = False
        if not candidates and natural_policy_name != "policy_leader_top3":
            from app.strategy import policy_leader_top3
            logger.info("[AutoTrader] %s 후보 0개(%s) → 주도섹터 Top3 폴백",
                        natural_policy_name, diag["reason_kr"])
            try:
                fb_candidates, fb_diag = policy_leader_top3.generate_candidates(market_ctx, self.cfg)
            except Exception as exc:
                logger.error("[AutoTrader] 주도섹터 Top3 폴백 예외: %s", exc)
                fb_candidates, fb_diag = [], {"reason": f"예외 발생: {exc}"}
            if fb_candidates:
                fallback_used = True
                for c in fb_candidates:
                    c.reason = f"[주도섹터Top3 폴백] {c.reason}"
                candidates = fb_candidates
                diag = {
                    "attempted_policy": natural_policy_name,
                    "reason_kr": diag["reason_kr"],
                    "original_policy_diag": diag,
                    "fallback_diag": fb_diag,
                    "fallback_reason_kr": _explain_recommendation("policy_leader_top3", fb_diag),
                }
            else:
                diag["fallback_reason_kr"] = _explain_recommendation("policy_leader_top3", fb_diag)
                diag["fallback_diag"] = fb_diag

        remaining_slots = self.trading_cfg.get("max_positions", 3) - len(self.position_guard.get_open_positions())
        candidates = candidates[:max(0, remaining_slots)]
        return {
            "candidates": candidates, "diag": diag, "fallback_used": fallback_used,
            "attempted_policy": natural_policy_name,
        }

    def _execution_safety_gate(self, regime_result: Optional[dict], candidate=None) -> tuple[bool, bool, str]:
        """
        실행 직전 2차(더 보수적인) 안전장치.

        Returns
        -------
        (blocked, manual_only, reason)
          blocked=True     : 이 후보(또는 전체)는 어떤 경로로도 매수 금지.
          manual_only=True : AUTO 경로만 금지(수동승인은 계속 가능).
        """
        if not regime_result:
            return False, False, ""

        market_collapse = regime_result.get("market_collapse_score")
        semi_collapse = regime_result.get("semiconductor_collapse_score")
        predicted_down_1h = ((regime_result.get("predictions") or {}).get("1h") or {}).get("probability_down")

        market_collapse_limit = self.trading_cfg.get("market_collapse_hard_block", DEFAULT_MARKET_COLLAPSE_HARD_BLOCK)
        if market_collapse is not None and market_collapse >= market_collapse_limit:
            return True, False, f"실행단계 안전장치: market_collapse_score {market_collapse:.0f} >= {market_collapse_limit:.0f} — 모든 신규매수 금지"

        is_semiconductor = bool(candidate) and (
            getattr(candidate, "sector", "") == "semiconductor"
            or getattr(candidate, "policy_name", "") == "policy_semiconductor_rebound"
        )
        semi_limit = self.trading_cfg.get("semiconductor_collapse_hard_block", DEFAULT_SEMICONDUCTOR_COLLAPSE_HARD_BLOCK)
        if is_semiconductor and semi_collapse is not None and semi_collapse >= semi_limit:
            return True, False, f"실행단계 안전장치: semiconductor_collapse_score {semi_collapse:.0f} >= {semi_limit:.0f} — 반도체 매수 금지"

        manual_only_limit = self.trading_cfg.get("predicted_down_1h_manual_only", DEFAULT_PREDICTED_DOWN_1H_MANUAL_ONLY)
        if predicted_down_1h is not None and predicted_down_1h >= manual_only_limit:
            return False, True, f"1시간 후 하락확률 {predicted_down_1h:.0f}% >= {manual_only_limit:.0f} — 신규 수동승인만 허용"

        return False, False, ""

    def buy_now(self, candidate, quantity: int = None, source: str = "manual", regime_result: Optional[dict] = None):
        """수동/자동 매수 공통 실행부. 즉시 1건 매수 + Position Guard 등록."""
        blocked, manual_only, gate_reason = self._execution_safety_gate(regime_result, candidate)
        if blocked:
            logger.warning("[AutoTrader] 매수 차단(%s): %s", candidate.symbol, gate_reason)
            return None
        if manual_only and source == "auto":
            logger.warning("[AutoTrader] 자동매수 차단, 수동승인 필요(%s): %s", candidate.symbol, gate_reason)
            return None

        qty = quantity if quantity is not None else self._calc_quantity(candidate.entry_price)
        if qty <= 0:
            return None
        result = self.order_executor.buy(
            symbol=candidate.symbol, name=candidate.name, quantity=qty, price=candidate.entry_price,
            reason=candidate.reason, source=source,
        )
        if result.success:
            self.position_guard.register_position(GuardedPosition(
                symbol=candidate.symbol, name=candidate.name, quantity=result.quantity,
                avg_price=result.price or candidate.entry_price, source=source,
                stop_loss_price=candidate.stop_loss_price,
                take_profit1_price=candidate.take_profit1_price,
                take_profit2_price=candidate.take_profit2_price,
            ))
        return result

    def auto_buy_all(self, candidates: list, regime_result: Optional[dict] = None) -> list:
        """③-자동매수: 추천 후보 전체를 리스크 한도 내에서 순차 자동매수."""
        blocked, _, gate_reason = self._execution_safety_gate(regime_result)
        if blocked:
            logger.warning("[AutoTrader] 자동매수 전체 중단: %s", gate_reason)
            return []

        results = []
        for c in candidates:
            can_open, reason = self.risk_manager.can_open_new_position(
                current_position_count=len(self.position_guard.get_open_positions()),
                max_positions=self.trading_cfg.get("max_positions", 3),
                max_daily_trades=self.trading_cfg.get("max_daily_trades", 3),
            )
            if not can_open:
                logger.info("[AutoTrader] 자동매수 중단: %s", reason)
                break
            result = self.buy_now(c, source="auto", regime_result=regime_result)
            if result is not None:
                results.append(result)
        return results

    def run_exit_check(self, regime: str = "", alert_level: str = "NONE") -> list:
        """③-자동손절익절: 보유 포지션 전체를 즉시 재평가하고 조건 충족 시 매도한다.

        alert_level=CRITICAL이면 뚜렷한 수익 구간이 아닌 포지션은 방어적으로 청산한다.
        """
        now_hm = _now_hm()
        self.position_guard.sync_from_broker(self.broker)
        open_positions = self.position_guard.get_open_positions()
        symbols = [p.symbol for p in open_positions]
        current_prices = self.price_watcher.get_prices(symbols) if symbols else {}
        return self.position_guard.evaluate_and_execute(
            current_prices, now_hm=now_hm, regime=regime,
            data_error_streak=self.price_watcher.consecutive_failures,
            alert_level=alert_level,
        )

    def manual_sell(self, symbol: str, quantity: int = None):
        """③-수동매도: 보유종목 중 선택한 1종목을 즉시 매도(전량 또는 지정 수량)."""
        position = next((p for p in self.position_guard.get_open_positions() if p.symbol == symbol), None)
        if position is None:
            return None
        price_info = self.price_watcher.get_price(symbol)
        price = price_info.get("price") or position.avg_price
        qty = min(quantity, position.quantity) if quantity else position.quantity

        result = self.order_executor.sell(
            symbol=symbol, name=position.name, quantity=qty, price=price,
            reason="manual_sell", source=f"manual_sell:{position.source}",
        )
        if result.success:
            if self.risk_manager is not None:
                pnl_amount = (price - position.avg_price) * qty
                self.risk_manager.record_trade_result(
                    symbol=symbol, pnl_amount=pnl_amount,
                    pnl_pct=position.profit_rate(price), was_stop_loss=False,
                )
            if qty >= position.quantity:
                position.status = "closed"
                position.quantity = 0
            else:
                position.quantity -= qty
        return result

    def sell_all(self) -> list:
        """③-일괄매도: 보유 포지션 전체를 즉시 매도."""
        results = []
        for position in list(self.position_guard.get_open_positions()):
            result = self.manual_sell(position.symbol)
            if result is not None:
                results.append(result)
        return results

    def _calc_quantity(self, price: float) -> int:
        if not price or price <= 0:
            return 0
        try:
            cash = self.broker.get_buyable_cash()
        except Exception:
            cash = 0
        max_positions = max(1, self.trading_cfg.get("max_positions", 3))
        budget_per_position = cash / max_positions if cash else 0
        return int(budget_per_position // price)
