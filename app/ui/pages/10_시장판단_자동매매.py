"""
10_시장판단_자동매매.py

오늘장 시장판단 모듈(Market Regime Router) 대시보드.

3단계로 분리되어 각각 독립적으로 실행할 수 있다:
  ① 시장판단      — 시장 유형(A~F) + 정책 선택만 수행
  ② 종목추천      — 선택된 정책의 후보 종목 조회 (후보 0개면 주도섹터 Top3로 폴백)
  ③ 매수매도      — 수동매수/자동매수, 자동손절익절, 수동 선택매도, 일괄매도를
                    각각 독립 버튼으로 제공
"""

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from datetime import datetime

import pandas as pd
import streamlit as st

try:
    from app.config import get_config, get_market_regime_config, get_trading_policy_config, real_order_triple_gate_ok
    from app.execution.auto_trader import AutoTrader
    from app.trading.broker_factory import create_broker
    from app.market.regime_router import should_reevaluate, REEVALUATION_INTERVAL_MINUTES
    _IMPORTS_OK = True
    _IMPORT_ERR = ""
except Exception as _ie:
    _IMPORTS_OK = False
    _IMPORT_ERR = str(_ie)

try:
    from streamlit_autorefresh import st_autorefresh
    _HAS_AUTOREFRESH = True
except ImportError:
    _HAS_AUTOREFRESH = False

st.title("오늘장 시장판단 · 자동매매 (Market Regime Router)")
st.caption("① 시장판단 → ② 종목추천(폴백: 주도섹터 Top3) → ③ 매수매도(자동/수동 각각 · 손절익절 · 일괄매도)")

if not _IMPORTS_OK:
    st.error(f"모듈 로드 오류: {_IMPORT_ERR}")
    st.stop()

cfg = get_config()
market_cfg = get_market_regime_config()
trading_cfg_all = get_trading_policy_config()
trading_mode_cfg = dict(trading_cfg_all.get("trading_mode", {}))
trading_mode_cfg["exit_rules"] = trading_cfg_all.get("exit_rules", {})
trading_mode_cfg["policy_gap_support_cfg"] = trading_cfg_all.get("policy_gap_support_cfg", {})

with st.expander("전략 설명 펼치기", expanded=False):
    st.markdown("""
| 유형 | 이름 | 정책 |
|---|---|---|
| A | 강세 주도장 | 주도섹터 Top3 (메인) |
| B | 급락 후 반도체 반등장 | 하이닉스/삼성전자/한미반도체 반등매수 |
| C | 지수 약세·테마 강세장 | GAP Top15 ∩ 주도섹터 (보조) |
| D | 갭상승 실패장 | 신규매수 금지 |
| E | 급락 지속장 | 인버스 또는 현금 |
| F | 보합/혼조장 | 매매 안 함 |

confidence_score < 60 이면 무조건 F(NO_TRADE)로 강등됩니다.
종목추천에서 선택 정책이 후보를 찾지 못하면 자동으로 주도섹터 Top3를 대신 보여줍니다.
""")

st.divider()

# ── 운영 모드 표시 ────────────────────────────────────────────────────────
c1, c2, c3 = st.columns(3)
c1.metric("주문 모드", trading_mode_cfg.get("order_mode", "PAPER"))
c2.metric("진입 모드(기본값)", trading_mode_cfg.get("entry_mode", "MANUAL_APPROVAL"))
c3.metric("청산 모드", trading_mode_cfg.get("exit_mode", "AUTO"))
st.caption("아래 ③ 매수매도 섹션에서는 기본 진입모드와 무관하게 수동매수/자동매수 버튼을 각각 직접 실행할 수 있습니다.")

if trading_mode_cfg.get("order_mode") == "REAL":
    st.warning("⚠️ REAL 주문 모드입니다. 3중 안전장치(enable_real_trading / real_trading_enabled / "
               "user_confirmed_real_risk)가 모두 통과해야 실제 주문이 나갑니다.")

password = ""
if trading_mode_cfg.get("order_mode") == "REAL":
    password = st.text_input("실전주문 확인 비밀번호 (real_order_confirm_text)", type="password")

st.divider()


# ── AutoTrader 준비 (세션 유지) ───────────────────────────────────────────

def _get_broker():
    is_real = trading_mode_cfg.get("order_mode") == "REAL" and real_order_triple_gate_ok(cfg)
    broker_mode = "real" if is_real else cfg.mode
    return create_broker(
        cfg=cfg, mode=broker_mode, confirm_text=password,
        runtime_real_mode=is_real, runtime_enable_real_buy=is_real, runtime_enable_real_sell=True,
    )


def _ensure_trader() -> "AutoTrader":
    """세션에 AutoTrader가 없으면 생성한다. 있으면 감시 상태(포지션/리스크)를 유지한다."""
    existing = st.session_state.get("regime_trader")
    broker = _get_broker()
    trader = AutoTrader(broker=broker, cfg=cfg, market_cfg=market_cfg, trading_cfg=trading_mode_cfg)
    if existing is not None:
        trader.position_guard = existing.position_guard
        trader.manual_approval = existing.manual_approval
        trader.risk_manager = existing.risk_manager
        trader.price_watcher = existing.price_watcher
    st.session_state["regime_trader"] = trader
    return trader


trader: "AutoTrader" = st.session_state.get("regime_trader")

# ═══════════════════════════════════════════════════════════════════════
# ① 시장판단
# ═══════════════════════════════════════════════════════════════════════

st.subheader("① 시장판단 (실시간 재평가 + 30분/1시간/3시간/내일장 예측)")


def _run_market_judgment(silent: bool = False) -> None:
    """시장판단 실행 + (있으면) 자동 손절익절 점검까지 함께 수행한다."""
    trader_local = _ensure_trader()
    try:
        step1 = trader_local.determine_market()
        st.session_state["mr_regime_result"] = step1["regime_result"]
        st.session_state["mr_policy_selection"] = step1["policy_selection"]
        st.session_state["mr_candidates"] = None  # 시장판단이 바뀌면 이전 추천은 무효화
        st.session_state["mr_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.session_state["mr_at_iso"] = datetime.now().isoformat(timespec="seconds")
        # 방어는 항상 자동 — 재판단 직후 보유 포지션을 즉시 재평가한다.
        alert_level = step1["regime_result"].get("alert_level", "NONE")
        defense_actions = trader_local.run_exit_check(
            regime=step1["regime_result"].get("regime", ""), alert_level=alert_level,
        )
        st.session_state["mr_last_defense_actions"] = defense_actions
        if not silent:
            st.success("시장판단 완료")
    except Exception as ex:
        if not silent:
            st.error(f"시장판단 오류: {ex}")


auto_col1, auto_col2 = st.columns([2, 1])
with auto_col1:
    auto_reeval = st.checkbox(
        f"🔄 {REEVALUATION_INTERVAL_MINUTES}분마다 자동 재평가 (09:25~11:10, 보유종목 방어 포함)",
        value=st.session_state.get("mr_auto_reeval", True), key="mr_auto_reeval",
    )
with auto_col2:
    if st.button("🔎 지금 즉시 재평가", type="primary", use_container_width=True):
        _run_market_judgment()

if auto_reeval and _HAS_AUTOREFRESH:
    st_autorefresh(interval=30_000, key="regime_autorefresh")
    now_hm_check = datetime.now().strftime("%H:%M")
    if "09:20" <= now_hm_check <= "11:10" and should_reevaluate(st.session_state.get("mr_at_iso")):
        _run_market_judgment(silent=True)
        st.rerun()
elif auto_reeval and not _HAS_AUTOREFRESH:
    st.caption("⚠️ streamlit-autorefresh 미설치 — '지금 즉시 재평가' 버튼을 수동으로 눌러주세요.")

regime_result = st.session_state.get("mr_regime_result")
policy_selection = st.session_state.get("mr_policy_selection")

if regime_result:
    st.caption(f"판단 시각: {st.session_state.get('mr_at', '')}")

    # ── 조기경보 배너 (최상단, 가장 중요) ──────────────────────────────────
    alert_level = regime_result.get("alert_level", "NONE")
    alert_reasons = regime_result.get("alert_reasons", [])
    action_recommendation = regime_result.get("action_recommendation", "")
    _alert_box = {"NONE": st.success, "WATCH": st.info, "WARNING": st.warning, "CRITICAL": st.error}
    _alert_box.get(alert_level, st.info)(
        f"**Market Alert: {alert_level}** — {action_recommendation}\n\n" + " · ".join(alert_reasons)
    )
    if alert_level == "CRITICAL":
        st.markdown(
            "<div style='background:#8b0000;color:#fff;padding:14px;border-radius:8px;"
            "text-align:center;font-weight:bold;font-size:1.1rem;margin-bottom:10px;'>"
            "🚨 CRITICAL — 신규매수 즉시 중단 · 자동매수 OFF · 보유종목 방어청산 검토 중 🚨</div>",
            unsafe_allow_html=True,
        )

    rc1, rc2, rc3, rc4 = st.columns(4)
    rc1.metric("유형", f"{regime_result['regime']}")
    rc2.metric("설명", regime_result["regime_label"])
    rc3.metric("confidence_score", f"{regime_result['confidence_score']:.1f}")
    rc4.metric("확정 여부", "✅ 확정" if regime_result["is_confirmed"] else "⏳ 관찰중")

    st.markdown(f"**선택된 정책:** `{policy_selection.policy_name}`")
    st.markdown(f"**판단 사유:** {', '.join(regime_result['reasons'])}")

    st.divider()

    # ── 최초 유형 vs 현재 유형 (장세 변화 조기 감지) ──────────────────────
    st.markdown("##### 실시간 장세 변화 감지")
    gc1, gc2, gc3 = st.columns(3)
    gc1.metric("최초 시장유형(09:20)", regime_result.get("initial_regime", "-"))
    gc2.metric("현재 시장유형", regime_result.get("current_regime", regime_result["regime"]))
    gc3.metric("유형 변화 위험도", f"{regime_result.get('regime_change_risk', 0):.0f}/100")
    if regime_result.get("initial_regime") and regime_result.get("initial_regime") != regime_result.get("current_regime"):
        st.warning(
            f"⚠️ 시장유형이 {regime_result['initial_regime']} → {regime_result['current_regime']}(으)로 전환되었습니다."
        )

    # ── 회복(recovery) 신호 + 위험점수 변화 추세 ───────────────────────────
    st.markdown("##### 회복(recovery) 신호 — \"위험 지속\" 관성 편향 완화")
    _recovery_score = regime_result.get("recovery_score")
    _score_deltas = regime_result.get("score_deltas", {}) or {}
    rc1, rc2, rc3, rc4 = st.columns(4)
    rc1.metric("회복 점수", f"{_recovery_score:.0f}/100" if _recovery_score is not None else "—")
    _mc5 = _score_deltas.get("market_collapse_score_delta_5m")
    _mc15 = _score_deltas.get("market_collapse_score_delta_15m")
    rc2.metric("시장붕괴점수 5분 변화", f"{_mc5:+.1f}" if _mc5 is not None else "—")
    rc3.metric("시장붕괴점수 15분 변화", f"{_mc15:+.1f}" if _mc15 is not None else "—")
    _momentum = _score_deltas.get("regime_transition_momentum")
    rc4.metric("전환 모멘텀", f"{_momentum:+.1f}" if _momentum is not None else "—",
               help="양수=완화(회복) 방향, 음수=악화 방향")

    _cur_regime = regime_result.get("current_regime", regime_result.get("regime"))
    if _cur_regime in ("D", "E") and _recovery_score is not None and _recovery_score >= 65 and _mc15 is not None and _mc15 <= -10:
        st.info(
            f"ℹ️ 현재 {_cur_regime}타입이나 recovery_score {_recovery_score:.0f}, "
            f"시장붕괴점수가 15분간 {_mc15:+.1f} 변화로 완화되는 중입니다 — "
            "1시간 이내 C 또는 SIDEWAYS로 완화될 가능성을 함께 참고하세요."
        )
    elif _cur_regime in ("D", "E") and _recovery_score is not None and _recovery_score < 40:
        st.caption("현재 회복 신호가 뚜렷하지 않아 위험 국면이 이어질 가능성에 무게를 두고 있습니다.")

    # ── §4/§10: 전체시장/반도체/주도테마는 서로 다른 질문이다 ───────────────
    st.warning(
        "⚠️ 전체시장·반도체 예측과 주도테마 예측은 다릅니다. "
        "일부 테마가 유지되어도 전체 시장이나 반도체가 회복된 것은 아닙니다."
    )

    _dir_icon = {
        "UP": "🟢 UP", "DOWN": "🔴 DOWN", "SIDEWAYS": "🟡 SIDEWAYS", "UNCERTAIN": "⚪ UNCERTAIN",
        "STRONG_UP": "🟢 강한 UP", "WEAK_UP": "🟢 약한 UP(잠정)",
        "STRONG_DOWN": "🔴 강한 DOWN", "WEAK_DOWN": "🔴 약한 DOWN(잠정)",
    }

    def _confidence_tier(v: float) -> str:
        if v >= 75:
            return "HIGH"
        if v >= 55:
            return "MEDIUM"
        if v >= 40:
            return "LOW"
        return "WATCH_ONLY"

    # ── 30분/1시간/3시간 전체 시장(overall_market) 예측 ──────────────────
    st.markdown("##### 전체 시장(overall_market) 방향 예측")
    predictions = regime_result.get("predictions", {}) or {}
    pcols = st.columns(3)
    for col, horizon, label in zip(pcols, ("30m", "1h", "3h"), ("30분 후", "1시간 후", "3시간 후")):
        pred = predictions.get(horizon, {})
        with col:
            st.markdown(f"**{label}**")
            st.markdown(f"### {_dir_icon.get(pred.get('direction'), pred.get('direction', '-'))}")
            st.caption(
                f"하락 {pred.get('probability_down', 0):.0f}% · 보합 {pred.get('probability_sideways', 0):.0f}% · "
                f"상승 {pred.get('probability_up', 0):.0f}% (1위-2위 차이 {pred.get('direction_margin', 0):.0f}pp)"
            )
            _conf = pred.get("confidence_score", 0) or 0
            st.caption(f"예상유형: {pred.get('expected_regime', '-')} · 신뢰도 {_conf:.0f} ({_confidence_tier(_conf)})")
            if pred.get("guard_rules_applied"):
                st.caption(f"가드: {', '.join(pred['guard_rules_applied'])}")
            for reason in pred.get("key_reasons", [])[:3]:
                st.caption(f"· {reason}")

    # ── 반도체(semiconductor) 예측 — 전체 시장과 분리된 축 ────────────────
    st.markdown("##### 반도체(semiconductor) 방향 예측 — 전체 시장과 별개 판단")
    semi_predictions = regime_result.get("semiconductor_prediction", {}) or {}
    scols = st.columns(3)
    for col, horizon, label in zip(scols, ("30m", "1h", "3h"), ("30분 후", "1시간 후", "3시간 후")):
        spred = semi_predictions.get(horizon, {})
        with col:
            st.markdown(f"**{label}**")
            st.markdown(f"### {_dir_icon.get(spred.get('direction'), spred.get('direction', '-'))}")
            st.caption(
                f"하락 {spred.get('probability_down', 0):.0f}% · 보합 {spred.get('probability_sideways', 0):.0f}% · "
                f"상승 {spred.get('probability_up', 0):.0f}%"
            )
            _sconf = spred.get("confidence_score", 0) or 0
            st.caption(
                f"반도체붕괴점수 {spred.get('semiconductor_collapse_score', 0):.0f} · "
                f"신뢰도 {_sconf:.0f} ({_confidence_tier(_sconf)})"
            )
            if spred.get("all_semi_stocks_below_vwap"):
                st.caption("🔴 하이닉스/삼성전자/한미반도체 모두 VWAP 이탈")
            for note in spred.get("guard_notes", [])[:2]:
                st.caption(f"· {note}")
    if any((semi_predictions.get(h) or {}).get("mu_data_status") in ("DELAYED", "MISSING") for h in ("30m", "1h", "3h")):
        st.caption("⚠️ MU(마이크론) 데이터가 지연/누락 상태라 반도체 예측 신뢰도가 상한 적용되었습니다.")

    # ── 주도테마(leading_theme) 유지 상태 — 방향 예측이 아님 ──────────────
    st.markdown("##### 주도테마(leading_theme) 유지 상태 — 방향/확률 예측 아님")
    theme_pred = regime_result.get("leading_theme_prediction", {}) or {}
    thc1, thc2, thc3 = st.columns(3)
    thc1.metric("현재 주도섹터", ", ".join(theme_pred.get("leading_sectors") or []) or "-")
    _theme_status = theme_pred.get("status", "-")
    thc2.metric(
        "주도테마 유지 여부",
        "🟢 유지(STABLE)" if theme_pred.get("leading_theme_maintained")
        else ("⚪ 판단불가(UNKNOWN)" if _theme_status == "UNKNOWN" else "🟡 회전/이탈"),
    )
    thc3.metric("theme_rotation_score", f"{theme_pred.get('theme_rotation_score', 50):.0f}/100")
    st.caption(theme_pred.get("disclaimer", ""))

    # ── 내일장 예측 ────────────────────────────────────────────────────────
    st.markdown("##### 내일장 예측")
    tomorrow = regime_result.get("tomorrow_prediction", {}) or {}
    _state_label = {
        "INTRADAY_PRELIMINARY": "장중(잠정)", "CLOSING_BASED": "장마감 기준",
        "US_SESSION_UPDATED": "미국장 반영(개장전)", "PREOPEN_FINAL": "개장직전 최종판단",
    }
    _gap_label = {"GAP_DOWN": "하락 출발 예상", "GAP_UP": "상승 출발 예상",
                  "FLAT_OR_UNCERTAIN": "보합/불확실(장중 잠정)", "FLAT": "보합 예상"}
    tc1, tc2, tc3, tc4 = st.columns(4)
    tc1.metric("내일 방향", _dir_icon.get(tomorrow.get("tomorrow_direction"), tomorrow.get("tomorrow_direction", "-")))
    tc2.metric("예상 시가갭", _gap_label.get(tomorrow.get("expected_open_gap"), tomorrow.get("expected_open_gap", "-")))
    tc3.metric("반도체 다음날 편향", tomorrow.get("semiconductor_next_day_bias", "-"))
    tc4.metric("위험수준", tomorrow.get("risk_level", "-"))
    st.caption(
        f"상태: {_state_label.get(tomorrow.get('state'), tomorrow.get('state', '-'))} · "
        f"주도예상섹터: {tomorrow.get('expected_leading_sector', '-')} · "
        f"신뢰도 {tomorrow.get('confidence_score', 0):.0f} · "
        f"하락 {tomorrow.get('probability_down', 0):.0f}% / 보합 {tomorrow.get('probability_sideways', 0):.0f}% / "
        f"상승 {tomorrow.get('probability_up', 0):.0f}%"
    )
    if tomorrow.get("disclaimer"):
        st.info(f"ℹ️ {tomorrow['disclaimer']}")
    for reason in tomorrow.get("key_reasons", [])[:5]:
        st.caption(f"· {reason}")

    # ── 붕괴 점수 + 실시간 지표 ───────────────────────────────────────────
    st.markdown("##### 시장/반도체 붕괴 점수 및 실시간 지표")
    scores = regime_result.get("scores", {}) or {}
    snapshot = regime_result.get("snapshot", {}) or {}
    domestic = snapshot.get("domestic", {})
    deltas = snapshot.get("deltas", {}) or {}

    cc1, cc2, cc3, cc4 = st.columns(4)
    cc1.metric("시장붕괴점수", f"{regime_result.get('market_collapse_score', 0):.0f}/100")
    cc2.metric("반도체붕괴점수", f"{regime_result.get('semiconductor_collapse_score', 0):.0f}/100")
    cc3.metric("회복 확률(추정)", f"{regime_result.get('recovery_probability', 0):.0f}%")
    cc4.metric("추세 지속 확률(추정)", f"{regime_result.get('trend_continuation_probability', 0):.0f}%")

    def _vwap_status(stock: dict) -> str:
        price, vwap = stock.get("current_price"), stock.get("vwap")
        if not price or not vwap:
            return "데이터 없음"
        return "🟢 VWAP 위" if price >= vwap else "🔴 VWAP 아래(이탈)"

    dc1, dc2, dc3 = st.columns(3)
    foreign_5m = (deltas.get("5m", {}) or {}).get("foreign_net_buy_proxy")
    kospi200_5m = (deltas.get("5m", {}) or {}).get("kospi200_futures_change_rate")
    fx_5m = (deltas.get("5m", {}) or {}).get("usdkrw_value")
    dc1.metric("외국인 수급(프록시) 5분 변화", f"{foreign_5m:+,.0f}" if foreign_5m is not None else "데이터 부족")
    dc2.metric("KOSPI200 선물 5분 변화", f"{kospi200_5m:+.2f}%p" if kospi200_5m is not None else "데이터 부족")
    dc3.metric("환율 5분 변화", f"{fx_5m:+.2f}원" if fx_5m is not None else "데이터 부족")

    vc1, vc2, vc3 = st.columns(3)
    vc1.metric("하이닉스 VWAP 상태", _vwap_status(domestic.get("hynix", {})))
    vc2.metric("삼성전자 VWAP 상태", _vwap_status(domestic.get("samsung", {})))
    vc3.metric("한미반도체 VWAP 상태", _vwap_status(domestic.get("hanmi", {})))

    bc1, bc2, bc3 = st.columns(3)
    _adv, _dec = domestic.get("advancers", 0), domestic.get("decliners", 0)
    bc1.metric("상승/하락 종목수", f"{_adv} / {_dec}")
    if not _adv and not _dec:
        bc1.caption("⚠️ 0/0 — breadth 데이터 없음(data_quality_score 상한 70 적용됨)")
    _theme_pred_for_bc = regime_result.get("leading_theme_prediction", {}) or {}
    if _theme_pred_for_bc.get("status") == "UNKNOWN":
        bc2.metric("주도섹터 유지/붕괴", "⚪ 판단불가(UNKNOWN)")
    else:
        bc2.metric("주도섹터 유지/붕괴", "🟢 유지" if _theme_pred_for_bc.get("leading_theme_maintained") else "🟡 회전/이탈")
    leader_sectors_now = domestic.get("sector_change_rates", {})
    top_sector = max(leader_sectors_now, key=leader_sectors_now.get) if leader_sectors_now else "-"
    bc3.metric("현재 주도섹터 1위", top_sector)

    st.caption("⚠️ 외국인 수급은 하이닉스+삼성전자 개별종목 순매수 합계를 시장 수급의 대리지표로 사용합니다(실제 지수선물 수급 아님).")

    st.markdown(
        f"**신규매수 상태:** {'✅ 허용' if policy_selection.allow_new_entry else '❌ 금지'}"
        f"{' (WATCH_ONLY/수동승인만)' if policy_selection.watch_only else ''}"
        f"{' (반도체 매수 금지)' if policy_selection.semiconductor_blocked else ''}"
    )
    defense_actions = st.session_state.get("mr_last_defense_actions") or []
    st.markdown(f"**자동매도 방어모드:** {'🛡️ 이번 tick ' + str(len(defense_actions)) + '건 방어 실행' if defense_actions else '대기 중(조건 미충족)'}")

    st.markdown("**미국장 상태 · 데이터 품질**")
    us_status = regime_result.get("us_market_status", {}) or {}

    def _us_status_label(s: dict) -> str:
        if not s:
            return "UNKNOWN"
        if s.get("is_us_market_open"):
            return "OPEN"
        if s.get("is_us_holiday"):
            return "HOLIDAY"
        if s.get("is_us_weekend"):
            return "WEEKEND"
        if s.get("is_us_early_close"):
            return "EARLY_CLOSE"
        return "CLOSED"

    uc1, uc2, uc3, uc4, uc5 = st.columns(5)
    uc1.metric("미국장 상태", _us_status_label(us_status))
    uc2.metric("마지막 미국 거래일", us_status.get("last_us_trading_day", "-"))
    uc3.metric("MU 데이터 상태", regime_result.get("mu_data_status", "-"))
    uc4.metric("MU 데이터 소스", regime_result.get("mu_data_source", "-"))
    uc5.metric("Holiday Mode", "🟠 ON" if regime_result.get("holiday_mode") else "⚪ OFF")

    qc1, qc2, qc3 = st.columns(3)
    qc1.metric("data_quality_score", f"{regime_result.get('data_quality_score', 0):.1f}")
    qc2.metric("data_freshness_score", f"{regime_result.get('data_freshness_score', 0):.1f}")
    qc3.metric("data_gap_reason", regime_result.get("data_gap_reason", "-"))

    with st.expander("점수 상세 (6종 + Holiday 보정)", expanded=False):
        st.json(regime_result["scores"])

    if policy_selection.block_reasons:
        st.warning("신규매수 제한 사유: " + " | ".join(policy_selection.block_reasons))
    if policy_selection.manual_approval_only:
        st.info("휴장모드 등의 사유로 자동매수 대신 수동매수를 권장합니다(아래 ③에서 수동매수는 계속 가능).")
else:
    st.caption("아직 시장판단이 실행되지 않았습니다.")

st.divider()

# ═══════════════════════════════════════════════════════════════════════
# ② 종목추천
# ═══════════════════════════════════════════════════════════════════════

st.subheader("② 종목추천")

if st.button("🎯 종목 추천 받기", use_container_width=True):
    if not regime_result or not policy_selection:
        st.warning("먼저 ① 시장판단을 실행하세요.")
    else:
        try:
            trader = _ensure_trader()
            with st.spinner("추천 종목 조회 중..."):
                step2 = trader.recommend_candidates(regime_result, policy_selection)
            st.session_state["mr_candidates"] = step2["candidates"]
            st.session_state["mr_candidates_diag"] = step2["diag"]
            st.session_state["mr_fallback_used"] = step2["fallback_used"]
            st.session_state["mr_attempted_policy"] = step2["attempted_policy"]
            if not step2["candidates"]:
                st.warning("주도섹터 Top3 폴백에서도 추천 종목을 찾지 못했습니다. 아래 사유를 확인하세요.")
            elif step2["fallback_used"]:
                st.info(f"`{step2['attempted_policy']}` 후보가 없어 주도섹터 Top3(대장주)로 대체했습니다. 아래 사유를 확인하세요.")
            else:
                st.success(f"`{step2['attempted_policy']}` 정책으로 {len(step2['candidates'])}개 종목 추천 완료")
        except Exception as ex:
            st.error(f"종목추천 오류: {ex}")

candidates = st.session_state.get("mr_candidates") or []
fallback_used = st.session_state.get("mr_fallback_used", False)
candidates_diag = st.session_state.get("mr_candidates_diag") or {}
attempted_policy = st.session_state.get("mr_attempted_policy")

if attempted_policy:
    reason_kr = candidates_diag.get("reason_kr")
    if fallback_used:
        st.markdown(
            f"**시도한 정책:** `{attempted_policy}` → 0개 ({reason_kr}) "
            f"→ **주도섹터 Top3(대장주)로 대체**"
        )
    elif not candidates:
        st.markdown(f"**시도한 정책:** `{attempted_policy}` → 추천 실패")
        st.markdown(f"**실패 사유:** {reason_kr}")
        fb_reason = candidates_diag.get("fallback_reason_kr")
        if fb_reason:
            st.markdown(f"**주도섹터 Top3 폴백도 실패한 사유:** {fb_reason}")
    with st.expander("상세 진단 정보(diag) 보기", expanded=False):
        st.json(candidates_diag)

if candidates:
    rows = [{
        "종목코드": c.symbol, "종목명": c.name,
        "매수예정가": f"{c.entry_price:,.0f}", "손절가": f"{c.stop_loss_price:,.0f}",
        "1차익절가": f"{c.take_profit1_price:,.0f}", "2차익절가": f"{c.take_profit2_price:,.0f}",
        "사유": c.reason,
    } for c in candidates]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
else:
    st.caption("추천 종목이 없습니다. 위 버튼으로 추천을 실행하세요.")

st.divider()

# ═══════════════════════════════════════════════════════════════════════
# ③ 매수매도
# ═══════════════════════════════════════════════════════════════════════

st.subheader("③ 매수매도")

# ── 3-a. 매수: 수동매수(종목별) / 자동매수(일괄) 각각 독립 ─────────────────
st.markdown("#### 매수")

if not candidates:
    st.caption("추천 종목이 있어야 매수할 수 있습니다. ②에서 먼저 추천을 받으세요.")
else:
    for c in candidates:
        with st.container(border=True):
            bc1, bc2, bc3 = st.columns([3, 2, 2])
            bc1.markdown(f"**{c.name}** ({c.symbol}) — {c.reason}")
            default_qty = trader._calc_quantity(c.entry_price) if trader else 0
            qty = bc2.number_input(
                "수량", min_value=0, value=max(default_qty, 0), step=1, key=f"qty_{c.symbol}",
            )
            if bc3.button("🖐️ 수동매수", key=f"manual_buy_{c.symbol}", use_container_width=True):
                trader = trader or _ensure_trader()
                result = trader.buy_now(c, quantity=int(qty), source="manual", regime_result=regime_result)
                if result and result.success:
                    st.success(f"{c.symbol} 수동매수 체결 완료 ({result.quantity}주) — 자동매도 감시 등록됨")
                elif result is None:
                    st.error("매수 차단됨: 실행단계 안전장치(market_collapse/semiconductor_collapse) 확인")
                else:
                    st.error(f"매수 실패: {result.message}")
                st.rerun()

    if st.button("⚡ 전체 자동매수 실행", type="primary", use_container_width=True):
        trader = trader or _ensure_trader()
        results = trader.auto_buy_all(candidates, regime_result=regime_result)
        ok = sum(1 for r in results if r.success)
        st.success(f"자동매수 실행 완료: 성공 {ok}/{len(results)}건") if results else st.warning("자동매수 가능한 후보가 없습니다(리스크 한도 또는 실행단계 안전장치 확인).")
        st.rerun()

st.divider()

# ── 3-b. 자동 손절익절 ─────────────────────────────────────────────────
st.markdown("#### 자동 손절익절")
exit_rules = trading_mode_cfg.get("exit_rules", {})
st.caption(
    f"조건: +{exit_rules.get('take_profit1_pct', 2.0)}% 50%익절 · "
    f"+{exit_rules.get('take_profit2_pct', 3.0)}% 전량익절 · "
    f"{exit_rules.get('stop_loss_pct', -1.2)}% 전량손절 · "
    f"{exit_rules.get('force_exit_time', '11:10')} 전량 시간청산 "
    "— 수동매수 포지션도 동일하게 적용됩니다."
)
if st.button("🛡️ 자동 손절익절 점검 실행", use_container_width=True):
    trader = trader or _ensure_trader()
    regime_for_check = regime_result.get("regime", "") if regime_result else ""
    alert_for_check = regime_result.get("alert_level", "NONE") if regime_result else "NONE"
    actions = trader.run_exit_check(regime=regime_for_check, alert_level=alert_for_check)
    if actions:
        st.success(f"{len(actions)}건 매도 실행됨")
        st.dataframe(pd.DataFrame(actions), use_container_width=True, hide_index=True)
    else:
        st.info("현재 손절/익절/시간청산 조건에 해당하는 포지션이 없습니다.")
    st.rerun()

st.divider()

# ── 3-c. 수동 선택종목 매도 / 3-d. 일괄매도 ────────────────────────────
sc1, sc2 = st.columns(2)

with sc1:
    st.markdown("#### 수동 선택종목 매도")
    open_positions = trader.position_guard.get_open_positions() if trader else []
    if open_positions:
        options = {f"{p.name}({p.symbol}) {p.quantity}주": p.symbol for p in open_positions}
        picked_label = st.selectbox("매도할 종목 선택", list(options.keys()), key="manual_sell_pick")
        picked_symbol = options[picked_label]
        picked_position = next(p for p in open_positions if p.symbol == picked_symbol)
        sell_qty = st.number_input(
            "매도 수량", min_value=1, max_value=picked_position.quantity,
            value=picked_position.quantity, step=1, key="manual_sell_qty",
        )
        if st.button("📤 선택 종목 매도", use_container_width=True):
            result = trader.manual_sell(picked_symbol, quantity=int(sell_qty))
            if result and result.success:
                st.success(f"{picked_symbol} {result.quantity}주 매도 완료")
            else:
                st.error(f"매도 실패: {result.message if result else '알 수 없는 오류'}")
            st.rerun()
    else:
        st.caption("보유 포지션이 없습니다.")

with sc2:
    st.markdown("#### 일괄매도")
    open_positions = trader.position_guard.get_open_positions() if trader else []
    st.caption(f"현재 보유 {len(open_positions)}종목 전체를 즉시 매도합니다.")
    if st.button("🧹 전체 일괄매도", use_container_width=True, disabled=not open_positions):
        results = trader.sell_all()
        ok = sum(1 for r in results if r.success)
        st.success(f"일괄매도 완료: 성공 {ok}/{len(results)}건")
        st.rerun()

st.divider()

st.markdown("#### 보유 포지션 · 자동매도 감시 상태")
guard_status = trader.position_guard.get_status() if trader else []
if guard_status:
    st.dataframe(pd.DataFrame(guard_status), use_container_width=True, hide_index=True)
else:
    st.caption("보유 포지션 없음")
