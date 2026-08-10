"""
11_MACD_자동매매2.py — ReadOnly UI for MACD2 (독립 신규 모듈)

MACD2는 app/trading/macd2/* 로 완전히 독립되어 있으며, 다른 자동매매
엔진 코드를 호출하지 않는다. UI는 command 기록(시작/중지)과 service.get_snapshot()
읽기만 수행한다 — MACD 계산·network 호출·Worker 생성/reload를 UI에서
직접 하지 않는다(docs/MACD2_LOGIC.md §16).

패널별로 격리되어 있어, 통계·원장 패널 하나가 오류가 나도 나머지 화면은
계속 렌더링된다(docs §16).
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from datetime import datetime, time as dtime

import pandas as pd
import streamlit as st

from app.ui.auth_gate import require_login

require_login()

from app.config import get_config, get_kis_account_config, mask_account  # noqa: E402
from app.trading.macd2 import config as macd2_config  # noqa: E402
from app.trading.macd2 import ledger  # noqa: E402
from app.trading.macd2.models import RuntimeStatus  # noqa: E402
from app.trading.macd2.service import get_service  # noqa: E402
from app.utils.runtime_info import read_runtime_info  # noqa: E402

# Display-only threshold, not an order-blocking rule (that remains
# macd2_config.FORCE_LIQUIDATE_AT/NEW_ENTRY_CUTOFF, untouched) — used only to
# tell "장 마감 후 대기" apart from "장전 대기" when bootstrap has no today
# bars yet (docs §21 2026-07-24 bootstrap-diagnostics UI addition).
_MARKET_CLOSE_HINT = dtime(15, 30)


def _worker_status(state, worker_stats: dict) -> str:
    """STOPPED (정상, auto_trade_on=False) / STARTING (스레드 있음, 아직 tick
    없음) / RUNNING (<=10s) / DELAYED (10~15s) / STALLED (>15s) / DEAD
    (auto_trade_on=True인데 Worker 스레드/객체 자체가 없음 — 프로세스 재시작
    후 복구되지 않은 상태)."""
    if not state.auto_trade_on:
        return "STOPPED"
    if not worker_stats:
        return "DEAD"
    age = worker_stats.get("last_tick_age_sec")
    if age is None:
        return "STARTING"
    if age <= 10:
        return "RUNNING"
    if age <= 15:
        return "DELAYED"
    return "STALLED"


def _quote_status(quotes: dict) -> str:
    for symbol in (macd2_config.WATCH_SYMBOL, macd2_config.LONG_SYMBOL, macd2_config.INVERSE_SYMBOL):
        snap = quotes.get(symbol)
        if snap is None or snap.error or not snap.price:
            return "PARTIAL_ERROR"
        if snap.age_sec is not None and snap.age_sec > macd2_config.QUOTE_MAX_AGE_SEC:
            return "PARTIAL_STALE"
    return "READY"


def _broker_order_result_display(state) -> str:
    result = str(state.last_broker_order_result or "").strip()
    if not result:
        return "-"
    legacy_cash_reject = "주문가능금액" in result and state.last_order_nrcvb_buy_qty is None and state.last_order_final_qty is None
    if legacy_cash_reject:
        return "LEGACY_ORDER_REJECTED_PRE_FIX"
    return result


def _bootstrap_status(state, bootstrap_last_result: dict | None) -> str:
    if state.warmup_ready:
        return "OK"
    reason = str((bootstrap_last_result or {}).get("reason") or state.order_block_reason or "")
    if "NO_1M_BARS" in reason or "TODAY_ONLY_WARMING_UP" in reason:
        now_t = datetime.now(macd2_config.KST).time()
        if now_t < macd2_config.SESSION_OPEN:
            return "PREMARKET_WAIT"
        if now_t >= _MARKET_CLOSE_HINT:
            return "MARKET_CLOSED_WAIT"
    return "FAILED" if bootstrap_last_result is not None or state.order_block_reason else "PENDING"


def _format_signal_time(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    try:
        parsed = pd.Timestamp(text)
        if not pd.isna(parsed):
            return parsed.strftime("%H:%M:%S")
    except Exception:
        pass
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 14:
        return f"{digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
    if len(digits) >= 6:
        return f"{digits[-6:-4]}:{digits[-4:-2]}:{digits[-2:]}"
    return text


def _signal_display_time(row: dict) -> str:
    return _format_signal_time(
        row.get("forming_bar_start")
        or row.get("signal_bar_at")
        or row.get("completed_bar_at")
    )


def _order_requested_at(row: dict) -> str:
    return _format_signal_time(row.get("order_requested_at") or row.get("order_requested_at_trace"))


def _not_ordered_reason(row: dict, requested_at: str) -> str:
    if requested_at != "-":
        return "-"
    for key in ("block_reason", "final_result", "order_result"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "NO_ORDER_REQUEST"


def _as_float(value: object) -> float | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except Exception:
        return None


def _as_bool(value: object) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _fmt_num(value: object, digits: int = 2) -> str:
    num = _as_float(value)
    return "-" if num is None else f"{num:.{digits}f}"


def _trade_entered_status(row: dict, requested_at: str) -> str:
    result = str(row.get("order_result") or "").strip().upper()
    if result == "EXECUTED":
        return "YES"
    if requested_at != "-":
        return "ORDER_REQUESTED"
    return "NO"


def _v6_unmet_summary(row: dict) -> tuple[str, str]:
    enabled = _as_bool(row.get("major_filter_enabled"))
    decision = str(row.get("major_decision") or "").strip()
    approved = _as_bool(row.get("major_approved"))
    if enabled is False:
        return "OFF", "filter OFF"
    if not decision and approved is None:
        return "-", "-"
    if approved is True or decision == macd2_config.MAJOR_APPROVED:
        return "PASS", "충족"

    reason = str(row.get("major_block_reason") or row.get("block_reason") or decision or "").strip()
    score = _fmt_num(row.get("major_score"), 0)
    required = _fmt_num(row.get("major_required_score"), 0)
    price = _fmt_num(row.get("price_impulse_atr"))
    hist = _fmt_num(row.get("hist_impulse_atr"))
    volume = _fmt_num(row.get("volume_ratio"))
    trend = str(row.get("ema20_or_vwap_ok") or "-")
    metrics = f"score {score}/{required}, price {price}ATR, hist {hist}, vol {volume}, trend {trend}"

    if decision == macd2_config.MAJOR_SCORE_BELOW_THRESHOLD:
        return "FAIL", f"score 미달 ({score} < {required})"
    if decision == macd2_config.MAJOR_PRICE_CONFIRMATION_FAILED:
        return "FAIL", f"price impulse 미달 ({price}ATR < {macd2_config.MAJOR_PRICE_IMPULSE_ATR_MIN:.2f})"
    if decision == macd2_config.MAJOR_SIDEWAYS_BLOCK:
        return "FAIL", f"횡보 차단 ({metrics})"
    if decision == macd2_config.MAJOR_DAILY_ENTRY_LIMIT:
        return "FAIL", "일일 진입 한도"
    if decision == macd2_config.MAJOR_SAME_DIRECTION_COOLDOWN:
        return "FAIL", "동일방향 재진입 쿨다운"
    if decision == macd2_config.MAJOR_MIN_HOLD_BLOCK:
        return "FAIL", "최소 보유시간 미충족"
    if decision == macd2_config.SAME_DIRECTION_POSITION_HELD:
        return "FAIL", "이미 같은 방향 보유"
    if decision == macd2_config.MAJOR_STRONG_PROFILE_FAILED:
        return "FAIL", f"{reason or 'V6 profile 미일치'} ({metrics})"
    return "FAIL", f"{reason or decision or 'V6 조건 미충족'} ({metrics})"


try:
    from streamlit_autorefresh import st_autorefresh

    st_autorefresh(interval=5000, key="macd2_refresh")
except Exception:
    pass

st.set_page_config(page_title="MACD 자동매매2", layout="wide")
st.title("MACD 자동매매2")
st.caption(
    "완전 독립 신규 모듈 · MACD v1/Enhanced와 상태·원장 미공유 · "
    "Read-only UI — command 기록과 snapshot 표시만 수행"
)

cfg = get_config()
service = get_service()
snapshot = service.get_snapshot()
state = snapshot["state"]

st.metric("UI mode", state.ui_mode.value)

# ── Controls (commands only) ────────────────────────────────────────────
st.subheader("계좌 / 제어")
c1, c2, c3, c4 = st.columns([1.2, 1.2, 1, 1])
with c1:
    mode = st.radio("계좌 모드", ["mock", "real"], index=0 if state.mode != "real" else 1, horizontal=True, key="macd2_mode")
with c2:
    budget = st.number_input(
        "투자예산 (원)", min_value=100_000, max_value=500_000_000,
        value=int(state.budget or macd2_config.DEFAULT_BUDGET), step=100_000, key="macd2_budget",
    )
with c3:
    try:
        acct = get_kis_account_config(mode)
        masked = acct.get("masked_account") or mask_account(acct.get("account_no", ""))
    except Exception:
        masked = None
    st.metric("계좌", masked or "(미설정)")
with c4:
    # auto_trade_on is a persisted flag and can survive a Streamlit process
    # restart with no Worker actually re-started in this process — never show
    # order-ready from the flag alone (docs: Worker 부재+auto_trade_on=True는
    # STALLED로 표시).
    _worker_alive = bool(service.supervisor_status().get("worker_alive"))
    if state.auto_trade_on and _worker_alive:
        st.metric("주문 가능", "YES")
    elif state.auto_trade_on:
        st.metric("주문 가능", "STALLED")
    else:
        st.metric("주문 가능", "NO")

real_kwargs = {}
if mode == "real":
    st.error("REAL(실전) 모드 — 확인 문구 입력 후에만 시작 가능")
    expected = str(cfg.real_confirm_text() or "LIVE")
    confirm_in = st.text_input("REAL 확인 문구", type="password", key="macd2_real_confirm")
    real_toggle = st.checkbox("REAL 주문 활성화", key="macd2_real_toggle")
    real_kwargs = {
        "confirm_text": confirm_in, "runtime_real_mode": bool(real_toggle),
        "runtime_enable_real_buy": bool(real_toggle), "runtime_enable_real_sell": bool(real_toggle),
    }
else:
    st.info("MOCK 모드 (기본값) — KIS 모의투자 계좌")

# 2026-08-05 fix: state.json이 오늘 도중 유실된(재배포/디스크 문제 등) 것으로
# 보이는 재시작을 감지했을 때 표시 — 강한 플래그/추세전환장/퀵Profit/Profit
# Lock 토글이 그 순간 기본값으로 조용히 되돌아갔을 수 있으니 반드시 아래
# 토글들의 현재 ON/OFF를 다시 확인해야 한다(잃어버린 토글 값은 시세 데이터로
# 복원할 방법이 없어 코드로 자동 교정할 수 없다 — 원인 자체(재배포 시 데이터
# 유실)는 Render Persistent Disk/AI_GAP_DATA_DIR 설정을 직접 점검해야 한다).
if getattr(state, "possible_toggle_reset_at", None):
    st.warning(
        f"⚠️ 오늘 {state.possible_toggle_reset_at} 무렵 자동매매 상태가 초기화된 것으로 보입니다 "
        "(재배포·재시작 등으로 state 파일이 유실됐을 가능성). 이 시점에 강한 플래그 거래/추세전환장 거래/"
        "퀵 Profit 익절/Profit Lock 토글이 기본값으로 조용히 되돌아갔을 수 있으니, 아래 토글들이 "
        "원하시는 설정대로 켜져 있는지 지금 꼭 다시 확인해 주세요."
    )

# Optional Hybrid MAJOR_FLAG filter toggle (command only — never places orders).
_filter_cols = st.columns([1.4, 1.6])
with _filter_cols[0]:
    _major_on = st.checkbox(
        "강한 플래그만 거래",
        value=bool(getattr(state, "major_filter_enabled", False)),
        key="macd2_major_filter_toggle",
        help="OFF=기존 confirmed 신호 전부 / ON=MAJOR_FLAG 승인 신호만 주문권한",
    )
with _filter_cols[1]:
    if bool(_major_on) != bool(getattr(state, "major_filter_enabled", False)):
        res = service.set_major_filter_enabled(bool(_major_on), changed_by="ui")
        if res.get("ok"):
            st.caption(
                f"Major filter → {'ON' if _major_on else 'OFF'} "
                f"(다음 confirmed 플래그부터 · `{res.get('major_filter_enabled_at')}`)"
            )
            st.rerun()
    else:
        st.caption(
            f"Major filter={'ON' if state.major_filter_enabled else 'OFF'} · "
            f"version=`{getattr(state, 'major_filter_version', None) or macd2_config.MAJOR_FILTER_VERSION}`"
        )

# Optional 추세전환장(sideways/whipsaw) filter toggle (command only — never
# places orders). When ON, this gate takes PRIORITY over "강한 플래그만 거래"
# above (the two are never both active for the same signal). ENTRY GATING
# ONLY — the take-profit exit below is a completely separate toggle now.
_sideways_cols = st.columns([1.4, 1.6])
with _sideways_cols[0]:
    _sideways_on = st.checkbox(
        "추세전환장 거래",
        value=bool(getattr(state, "sideways_filter_enabled", False)),
        key="macd2_sideways_filter_toggle",
        help="OFF=기존 로직 그대로 / ON=약한 점수(score 낮음)+비돌파 신호만 주문권한(강한 플래그 필터보다 우선)",
    )
with _sideways_cols[1]:
    if bool(_sideways_on) != bool(getattr(state, "sideways_filter_enabled", False)):
        res = service.set_sideways_filter_enabled(bool(_sideways_on), changed_by="ui")
        if res.get("ok"):
            st.caption(
                f"추세전환장 필터 → {'ON' if _sideways_on else 'OFF'} "
                f"(다음 confirmed 플래그부터 · `{res.get('sideways_filter_enabled_at')}`)"
            )
            st.rerun()
    else:
        st.caption(
            f"추세전환장 필터={'ON' if state.sideways_filter_enabled else 'OFF'} · "
            f"version=`{getattr(state, 'sideways_filter_version', None) or macd2_config.SIDEWAYS_FILTER_VERSION}`"
        )

# Optional Trend Persistence filter toggle (command only — never places
# orders). When ON, this gate is LOWEST priority — both "강한 플래그만 거래"
# and "추세전환장 거래" above take priority over it (the three are never more
# than one active for the same signal). ENTRY GATING ONLY.
_tp_cols = st.columns([1.4, 1.6])
with _tp_cols[0]:
    _tp_on = st.checkbox(
        "Trend Persistence 거래",
        value=bool(getattr(state, "trend_persistence_filter_enabled", False)),
        key="macd2_trend_persistence_filter_toggle",
        help=(
            "OFF=기존 로직 그대로 / ON=VWAP 체류시간+EMA5/10/20 정렬+최근 3봉 HH/HL(또는 LH/LL) 구조로 "
            f"산출한 점수가 {macd2_config.TREND_PERSISTENCE_SCORE_MIN:.0f}점 이상인 confirmed 신호만 주문권한 "
            "(강한 플래그 필터·추세전환장 필터보다 우선순위 낮음 — 둘 중 하나라도 ON이면 이 필터는 적용되지 않음)"
        ),
    )
with _tp_cols[1]:
    if bool(_tp_on) != bool(getattr(state, "trend_persistence_filter_enabled", False)):
        res = service.set_trend_persistence_filter_enabled(bool(_tp_on), changed_by="ui")
        if res.get("ok"):
            st.caption(
                f"Trend Persistence 필터 → {'ON' if _tp_on else 'OFF'} "
                f"(다음 confirmed 플래그부터 · `{res.get('trend_persistence_filter_enabled_at')}`)"
            )
            st.rerun()
    else:
        st.caption(
            f"Trend Persistence 필터={'ON' if state.trend_persistence_filter_enabled else 'OFF'} · "
            f"version=`{getattr(state, 'trend_persistence_filter_version', None) or macd2_config.TREND_PERSISTENCE_FILTER_VERSION}`"
        )

# Optional Daily Single-Entry filter toggle (command only — never places
# orders). When ON, this gate is LOWEST priority of the four — "강한 플래그만
# 거래"/"추세전환장 거래"/"Trend Persistence 거래" above all take priority over
# it. ENTRY GATING ONLY: v3 (2026-08-10) scores EVERY confirmed flag of the
# day (MAJOR score + seq bonus + gap/EMA10/15m-slope bonuses - overheat
# penalty); approves only while under config.SINGLE_ENTRY_MAX_DAILY_
# ENTRIES fills AND score>=config.SINGLE_ENTRY_SCORE_MIN — the 4th+ flag is
# no longer auto-blocked, and a weak 1st-3rd flag is no longer auto-approved.
_se_cols = st.columns([1.4, 1.6])
with _se_cols[0]:
    _se_on = st.checkbox(
        "2% 3회진입",
        value=bool(getattr(state, "single_entry_filter_enabled", False)),
        key="macd2_single_entry_filter_toggle",
        help=(
            "OFF=기존 로직 그대로 / ON=그날의 모든 confirmed 플래그를 계속 평가 — "
            f"MAJOR 점수 + 순번가산(1번째+{macd2_config.SINGLE_ENTRY_SEQ_BONUS_1:.0f}/2번째+{macd2_config.SINGLE_ENTRY_SEQ_BONUS_2:.0f}/3번째+{macd2_config.SINGLE_ENTRY_SEQ_BONUS_3:.0f}, 4번째부터는 가산 없음, 자동차단도 없음) "
            "+ MACD gap확장/EMA10기울기/최근15분 기울기 방향일치 가점 "
            f"- 과열감점(price_impulse_atr>={macd2_config.SINGLE_ENTRY_OVERHEAT_THRESHOLD:.1f}) 합산 점수가 "
            f"{macd2_config.SINGLE_ENTRY_SCORE_MIN:.0f}점 이상이면 진입, 오늘 신규진입이 이미 "
            f"{macd2_config.SINGLE_ENTRY_MAX_DAILY_ENTRIES}회면 그때부터는 차단(반대신호 청산은 그대로 적용). "
            "near-zero BLUE(|MACD|<3000)는 진단값만 기록하고 점수에는 미반영. "
            "(강한 플래그·추세전환장·Trend Persistence 필터보다 우선순위 낮음 — 셋 중 하나라도 ON이면 이 필터는 적용되지 않음). "
            "2026-08-10 실제 worker.run_once() 재현 검증(25거래일, 000660): 기존 순번캡(v2, 75건) 대비 "
            "거래 73건(2.92/일), 승률 54.8%(v2 53.3%), 퀵Profit청산률 53.4%(v2 50.7%), Net 2,658,576(v2 1,737,014) — "
            "MDD만 12.7% 높음(1,445,338 vs 1,282,201). 퀵 Profit 익절(2% 자동 익절)과 함께 켜는 것을 권장."
        ),
    )
with _se_cols[1]:
    if bool(_se_on) != bool(getattr(state, "single_entry_filter_enabled", False)):
        res = service.set_single_entry_filter_enabled(bool(_se_on), changed_by="ui")
        if res.get("ok"):
            st.caption(
                f"2% 3회진입 → {'ON' if _se_on else 'OFF'} "
                f"(다음 confirmed 플래그부터 · `{res.get('single_entry_filter_enabled_at')}`)"
            )
            st.rerun()
    else:
        st.caption(
            f"2% 3회진입={'ON' if state.single_entry_filter_enabled else 'OFF'} · "
            f"version=`{getattr(state, 'single_entry_filter_version', None) or macd2_config.SINGLE_ENTRY_FILTER_VERSION}`"
        )

# Profit Lock — MACD convergence early exit (2026-08-05 spec; replaces the
# old net-return-giveback Profit Lock entirely). EXIT LOGIC ONLY — never
# places/changes an entry, never touches Stop Loss/forced liquidation/
# opposite-flag switching. Default OFF (2026-08-05: all filters default OFF).
# Mutually exclusive with "퀵 Profit 익절" below — never both ON at once
# (service.py refuses the second toggle).
_pl_cols = st.columns([1.4, 1.6])
with _pl_cols[0]:
    _pl_on = st.checkbox(
        "Profit Lock",
        value=bool(getattr(state, "profit_lock_enabled", False)),
        key="macd2_profit_lock_toggle",
        help=(
            "추세전환장 모드 ON/OFF와 무관하게 항상 수동 제어 가능. "
            "OFF(기본값)=Profit Lock 매도 완전 비활성화(기존 손절·반대플래그청산·강제청산·퀵Profit만 적용) / "
            f"ON=보유 방향 MACD-Signal 간격 수렴을 감지해 조기 청산 — "
            f"수익률 +{macd2_config.PROFIT_LOCK_MIN_NET_RETURN_PCT}% 이상, 완성 3분봉 "
            f"{macd2_config.PROFIT_LOCK_MIN_BARS_SINCE_ENTRY}개 이상 경과, 간격 "
            f"{macd2_config.PROFIT_LOCK_MIN_CONSECUTIVE_CONTRACTIONS}봉 연속 축소, 간격비율 "
            f"{macd2_config.PROFIT_LOCK_MAX_GAP_RATIO*100:.0f}% 이하, 최고수익 대비 "
            f"{macd2_config.PROFIT_LOCK_MIN_DRAWDOWN_PP}%p 이상 반납 — 5개 조건 모두 충족 시 전량 매도. "
            "퀵 Profit 익절과 동시 ON 불가."
        ),
    )
with _pl_cols[1]:
    if bool(_pl_on) != bool(getattr(state, "profit_lock_enabled", False)):
        res = service.set_profit_lock_enabled(bool(_pl_on), changed_by="ui")
        if res.get("ok"):
            st.caption(
                f"Profit Lock → {'ON' if _pl_on else 'OFF'} "
                f"(다음 완성 3분봉부터 · `{res.get('profit_lock_enabled_at')}`)"
            )
            st.rerun()
        else:
            st.error("Profit Lock은 퀵 Profit 익절과 동시에 켤 수 없습니다 — 퀵 Profit 익절을 먼저 꺼주세요.")
    else:
        st.caption(f"Profit Lock={'ON' if getattr(state, 'profit_lock_enabled', False) else 'OFF'}")

# Optional Quick-Profit take-profit filter toggle (command only — never
# places orders). EXIT LOGIC ONLY — completely independent of both
# "강한 플래그만 거래" and "추세전환장 거래" above; applies underneath whichever
# of those (or neither) is active. ON: a held position exits in full the
# moment net return reaches +2.0% (2026-08-05: raised from 1.5%, and judged
# directly off each tick's live quote — no "1분 고점 기억" delay any more, so
# turning this ON while already holding a qualifying position sells on the
# very next tick). OFF: existing 손절(-1.5%)/반대플래그 청산/장마감 강제청산
# 규칙만 적용 (지금까지와 동일). 2026-08-05: 상호배타 — Profit Lock과 동시에
# ON 불가 (service.py가 두 번째 토글 시도를 거부).
_qp_cols = st.columns([1.4, 1.6])
with _qp_cols[0]:
    _qp_on = st.checkbox(
        "퀵 Profit 익절",
        value=bool(getattr(state, "quick_profit_enabled", False)),
        key="macd2_quick_profit_toggle",
        help=(
            "추세전환장 모드 ON/OFF와 무관하게 항상 수동 제어 가능. "
            f"OFF=기존 손절·반대플래그청산·강제청산만 적용 / "
            f"ON=보유 포지션 순수익률이 +{macd2_config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT}%에 도달하는 즉시(1분봉 확정 전 실시간 시세 기준) 전량 익절 "
            "(일반거래/강한 플래그 거래/추세전환장 어떤 진입 모드에서도 동일하게 적용, 수동매수 포함, 진입 로직은 전혀 안 바뀜). "
            "Profit Lock과 동시 ON 불가."
        ),
    )
with _qp_cols[1]:
    if bool(_qp_on) != bool(getattr(state, "quick_profit_enabled", False)):
        res = service.set_quick_profit_enabled(bool(_qp_on), changed_by="ui")
        if res.get("ok"):
            st.caption(
                f"퀵 Profit 익절 → {'ON' if _qp_on else 'OFF'} "
                f"(다음 tick부터 · `{res.get('quick_profit_enabled_at')}`)"
            )
            st.rerun()
        else:
            st.error("퀵 Profit 익절은 Profit Lock과 동시에 켤 수 없습니다 — Profit Lock을 먼저 꺼주세요.")
    else:
        st.caption(f"퀵 Profit 익절={'ON' if state.quick_profit_enabled else 'OFF'}")

b1, b2, b3, b4 = st.columns(4)
with b1:
    if st.button("자동매매 시작", type="primary", use_container_width=True):
        res = service.start(mode=mode, budget=float(budget), real_kwargs=real_kwargs if mode == "real" else None)
        if res.get("ok"):
            st.success("MACD2 자동매매 시작")
            st.rerun()
        else:
            st.error(res.get("message") or "시작 실패")
with b2:
    if st.button("자동매매 중지", use_container_width=True):
        service.stop("user_stop")
        st.warning("중지됨")
        st.rerun()
with b3:
    if st.button("자동매매 중지 및 일괄매도", use_container_width=True):
        res = service.stop_and_liquidate_all("user_stop_liquidate_all")
        sold = [r for r in res.get("results", []) if r.get("symbol")]
        if res.get("ok"):
            if sold:
                detail = ", ".join(f"{r['symbol']} {r['quantity']}주" for r in sold)
                st.warning(f"자동매매 중지 + 일괄매도 완료: {detail}")
            else:
                st.warning("자동매매 중지됨 (보유 포지션 없음)")
        else:
            failed = ", ".join(f"{r.get('symbol') or '?'}:{r.get('block_reason')}" for r in sold if not r.get("ok"))
            st.error(f"일괄매도 일부/전체 실패 — {failed or res.get('message')}")
        st.rerun()
with b4:
    if st.button("Bootstrap 재시도", use_container_width=True):
        res = service.retry_bootstrap()
        if res.get("ok"):
            st.success(res.get("message") or "Bootstrap 재시도 성공")
        else:
            st.error(res.get("message") or "Bootstrap 재시도 실패")
        st.rerun()

# 수동 진입 버튼 (2026-08-04) — MACD 신호/필터를 전혀 거치지 않고 지정한
# 방향의 ETF를 예산 내 즉시 매수(프리마켓 등 시스템이 못 본 신호를 사람이
# 판단해서 넣는 용도). 이미 포지션 보유 중이면 거부만 하고 아무 것도 안 함.
# 체결 후에는 기존 손절/퀵프로핏/반대플래그청산 로직이 그대로 관리.
st.caption("수동 진입 (MACD 신호·필터 무시, 예산 내 즉시 전량매수 — 이미 보유 중이면 거부)")
m1, m2 = st.columns(2)
with m1:
    if st.button("현재시점 레버리지(레드) 전량매수", use_container_width=True):
        res = service.manual_entry("UP_RED")
        if res.get("ok"):
            st.success(f"레버리지 매수 체결: {res.get('symbol')} {res.get('quantity')}주 @ {res.get('price')}")
        else:
            st.error(f"레버리지 매수 실패: {res.get('message') or res.get('block_reason')}")
        st.rerun()
with m2:
    if st.button("현재시점 인버스(블루) 전량매수", use_container_width=True):
        res = service.manual_entry("DOWN_BLUE")
        if res.get("ok"):
            st.success(f"인버스 매수 체결: {res.get('symbol')} {res.get('quantity')}주 @ {res.get('price')}")
        else:
            st.error(f"인버스 매수 실패: {res.get('message') or res.get('block_reason')}")
        st.rerun()

# 09:03 예약 매수 버튼 (2026-08-06) — 개장 직후 데이터 부족으로 이른 시간대
# MACD 플래그를 놓치기 쉬운 문제 대응. 지금 눌러두면 오늘 09:03(창 3분)에
# worker.run_once가 자동으로 지정 방향 ETF를 예산 내 전량매수한다(하루 1회).
# 체결 후에는 기존 손절/반대플래그청산/프로핏락/퀵프로핏 로직이 그대로
# 감시하며(수동매수와 동일한 경로), 체결·신호 원장에도 동일하게 기록된다.
_sched_dir = getattr(state, "scheduled_entry_armed_direction", None)
_sched_done = getattr(state, "scheduled_entry_executed_at", None)
if _sched_done:
    _protect_note = (
        f" · 반대 플래그 보호 중(~{macd2_config.SCHEDULED_ENTRY_PROTECTION_UNTIL.strftime('%H:%M')}까지 반대신호청산 무시)"
        if getattr(state, "scheduled_entry_protected", False) else ""
    )
    st.caption(f"09:03 예약 매수 — 오늘 처리 완료: `{state.scheduled_entry_last_result or '-'}`{_protect_note}")
else:
    _sched_label = "레버리지(레드)" if (_sched_dir and _sched_dir.value == "UP_RED") else (
        "인버스(블루)" if (_sched_dir and _sched_dir.value == "DOWN_BLUE") else "없음"
    )
    st.caption(f"09:03 예약 매수 (개장 직후 이른 플래그 대응, 하루 1회) — 현재 예약: {_sched_label}")
sch1, sch2 = st.columns(2)
with sch1:
    _armed_up = bool(_sched_dir and _sched_dir.value == "UP_RED")
    if st.button(
        ("[예약중] " if _armed_up else "") + "09시03분 레버리지(레드) 전량매수 예약",
        use_container_width=True, disabled=bool(_sched_done),
    ):
        res = service.arm_scheduled_entry("UP_RED")
        if res.get("ok"):
            st.success("09:03 레버리지 전량매수 예약됨" if res.get("armed") else "예약 해제됨")
        else:
            st.error(res.get("message") or "예약 실패")
        st.rerun()
with sch2:
    _armed_down = bool(_sched_dir and _sched_dir.value == "DOWN_BLUE")
    if st.button(
        ("[예약중] " if _armed_down else "") + "09시03분 인버스(블루) 전량매수 예약",
        use_container_width=True, disabled=bool(_sched_done),
    ):
        res = service.arm_scheduled_entry("DOWN_BLUE")
        if res.get("ok"):
            st.success("09:03 인버스 전량매수 예약됨" if res.get("armed") else "예약 해제됨")
        else:
            st.error(res.get("message") or "예약 실패")
        st.rerun()

# 수동 전량매도 버튼 (2026-08-04) — "자동매매 중지 및 일괄매도"와 달리
# 자동매매는 계속 유지한 채 현재 보유 포지션만 지금 즉시 시장가로 전량
# 매도한다. 체결/신호 원장에 모두 기록되며, 이후 확정 신호부터는 다시
# 기존 로직이 정상적으로 감시/매매한다.
st.caption("수동 전량매도 (자동매매는 계속 유지, 현재 보유 포지션만 지금 즉시 매도)")
if st.button("현재 보유 포지션 수동 전량매도", use_container_width=True):
    res = service.manual_exit()
    if res.get("ok"):
        st.success(f"수동 매도 체결: {res.get('symbol')} {res.get('quantity')}주 @ {res.get('price')}")
    else:
        st.error(f"수동 매도 실패: {res.get('message') or res.get('block_reason')}")
    st.rerun()

# Re-read after potential command
snapshot = service.get_snapshot()
state = snapshot["state"]
worker_stats = snapshot["worker"] or {}
quotes = snapshot["quotes"] or {}

# ── Status ────────────────────────────────────────────────────────────────
try:
    st.subheader("상태")
    bootstrap_last_result = snapshot.get("bootstrap_last_result")
    quote_status = snapshot.get("quote_status") or _quote_status(quotes)
    bootstrap_status = _bootstrap_status(state, bootstrap_last_result)
    macd_status = "READY" if state.warmup_ready else "NOT_READY"
    order_status = "BLOCKED" if state.order_block_reason or not state.auto_trade_on else "READY"

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("전략 상태", state.ui_mode.value)
    s2.metric("Worker 상태", _worker_status(state, worker_stats))
    s3.metric("quote_status", quote_status)
    s4.metric("bootstrap_status", bootstrap_status)

    st.caption(f"macd_status=`{macd_status}` · order_status=`{order_status}`")
    st.write(f"block/error reason: `{state.order_block_reason or '-'}`")

    q1, q2, q3 = st.columns(3)
    for col, symbol, label in (
        (q1, macd2_config.WATCH_SYMBOL, "SK하이닉스 000660"),
        (q2, macd2_config.LONG_SYMBOL, "KODEX 0193T0"),
        (q3, macd2_config.INVERSE_SYMBOL, "SOL 0197X0"),
    ):
        snap = quotes.get(symbol)
        if snap is None:
            col.metric(label, "-")
        else:
            col.metric(label, f"{snap.price:,.0f}" if snap.price else "-", delta=f"age {snap.age_sec:.1f}s" if snap.age_sec is not None else None)

    p1, p2 = st.columns(2)
    if state.position:
        p1.metric("보유 종목", f"{state.position.symbol} · {state.position.quantity}주 · 평단 {state.position.avg_price:,.0f}")
    else:
        p1.metric("보유 종목", "flat")
    p2.metric(
        "Profit Lock", "ON" if getattr(state, "profit_lock_enabled", False) else "OFF",
        delta=f"peak {getattr(state, 'profit_lock_peak_return_pct', 0.0):.2f}%",
    )

    st.markdown("**Major filter (강한 플래그)**")
    mf1, mf2, mf3, mf4 = st.columns(4)
    mf1.metric("Major filter", "ON" if getattr(state, "major_filter_enabled", False) else "OFF")
    mf2.metric("filter version", getattr(state, "major_filter_version", None) or macd2_config.MAJOR_FILTER_VERSION)
    mf3.metric(
        "오늘 MAJOR 승인 진입",
        f"{int(getattr(state, 'daily_major_entry_count', 0) or 0)} / {macd2_config.MAJOR_MAX_DAILY_ENTRIES}",
    )
    mf4.metric("마지막 MAJOR 승인 시각", _format_signal_time(getattr(state, "last_major_entry_at", None)) if getattr(state, "last_major_entry_at", None) else "-")
    st.caption(
        f"enabled_at=`{getattr(state, 'major_filter_enabled_at', None) or '-'}` · "
        f"by=`{getattr(state, 'major_filter_enabled_by', None) or '-'}`"
    )
    st.info(
        "강한 플래그 V6 기준: confirmed 3분봉 플래그 확정 시점에만 판정. "
        "score, price impulse, MACD hist, volume, EMA/VWAP, 장중 시간대 profile 조건을 사용. "
        "opening/morning, midday trend/reversal, late rebound/capitulation profile만 진입 승인. "
        "그 외 플래그는 MAJOR_STRONG_PROFILE_FAILED로 차단."
    )

    st.markdown("**추세전환장 필터 (횡보/휩쏘 대응)**")
    sf1, sf2, sf3, sf4 = st.columns(4)
    sf1.metric("추세전환장 필터", "ON" if getattr(state, "sideways_filter_enabled", False) else "OFF")
    sf2.metric("filter version", getattr(state, "sideways_filter_version", None) or macd2_config.SIDEWAYS_FILTER_VERSION)
    sf3.metric("오늘 추세전환장 승인 진입", f"{int(getattr(state, 'daily_sideways_entry_count', 0) or 0)}")
    sf4.metric(
        "마지막 추세전환장 승인 시각",
        _format_signal_time(getattr(state, "last_sideways_entry_at", None)) if getattr(state, "last_sideways_entry_at", None) else "-",
    )
    _sw_score = getattr(state, "last_sideways_score", None)
    _sw_req = getattr(state, "last_sideways_required_score", None)
    st.caption(
        f"enabled_at=`{getattr(state, 'sideways_filter_enabled_at', None) or '-'}` · "
        f"by=`{getattr(state, 'sideways_filter_enabled_by', None) or '-'}` · "
        f"최근 판정 score=`{f'{_sw_score:.0f}/{_sw_req:.0f}' if _sw_score is not None and _sw_req is not None else '-'}` "
        f"decision=`{getattr(state, 'last_sideways_decision', None) or '-'}`"
    )
    st.info(
        f"추세전환장 기준 v5(ON일 때 강한 플래그 필터보다 우선 적용, 진입권한만 결정): "
        f"09:00~11:00은 오늘의 PRIMARY_TREND와 반대 방향(눌림목)만 제외하고 나머진 진입, "
        f"11:00부터 장 마감까지는 score < {macd2_config.SIDEWAYS_ENTRY_SCORE_MAX:.0f} (약한 플래그만) "
        f"AND 4봉 돌파(breakout) 없음 — 둘 다 충족해야 진입. "
        "2026-08-03~08-07 실거래 5일 tick-by-tick 재현으로 4개 후보 중 최고 성과(순수익 13.87%, 승률 67%) 기준."
    )

    st.markdown("**Trend Persistence 필터 (VWAP/EMA/구조 점수, 기본 OFF)**")
    tp1, tp2, tp3, tp4 = st.columns(4)
    tp1.metric("Trend Persistence 필터", "ON" if getattr(state, "trend_persistence_filter_enabled", False) else "OFF")
    tp2.metric("filter version", getattr(state, "trend_persistence_filter_version", None) or macd2_config.TREND_PERSISTENCE_FILTER_VERSION)
    tp3.metric("오늘 Trend Persistence 승인 진입", f"{int(getattr(state, 'daily_trend_persistence_entry_count', 0) or 0)}")
    tp4.metric(
        "마지막 승인 시각",
        _format_signal_time(getattr(state, "last_trend_persistence_entry_at", None)) if getattr(state, "last_trend_persistence_entry_at", None) else "-",
    )
    _tp_score = getattr(state, "last_trend_persistence_score", None)
    _tp_req = getattr(state, "last_trend_persistence_required_score", None)
    st.caption(
        f"enabled_at=`{getattr(state, 'trend_persistence_filter_enabled_at', None) or '-'}` · "
        f"by=`{getattr(state, 'trend_persistence_filter_enabled_by', None) or '-'}` · "
        f"최근 판정 score=`{f'{_tp_score:.0f}/{_tp_req:.0f}' if _tp_score is not None and _tp_req is not None else '-'}` "
        f"decision=`{getattr(state, 'last_trend_persistence_decision', None) or '-'}`"
    )
    st.info(
        f"Trend Persistence 기준 v1(ON일 때 강한 플래그 필터·추세전환장 필터보다 우선순위 낮음, 진입권한만 결정): "
        f"VWAP 체류시간 + EMA5/10/20 정렬 + 최근 3개 완성 3분봉 HH/HL(또는 LH/LL) 구조로 산출한 "
        f"0-100 점수가 {macd2_config.TREND_PERSISTENCE_SCORE_MIN:.0f}점 이상이면 진입 승인. "
        "2026-07-20~08-07 15거래일 리플레이 백테스트로 50/55/60/65/70 스윕 검증 — "
        "70이 순수익/승률/MDD/profit factor 전 지표에서 최고 성과."
    )

    st.markdown(f"**2% 3회진입 (최대 {macd2_config.SINGLE_ENTRY_MAX_DAILY_ENTRIES}회/일, 기본 OFF)**")
    se1, se2, se3, se4 = st.columns(4)
    se1.metric("2% 3회진입", "ON" if getattr(state, "single_entry_filter_enabled", False) else "OFF")
    se2.metric("filter version", getattr(state, "single_entry_filter_version", None) or macd2_config.SINGLE_ENTRY_FILTER_VERSION)
    se3.metric("오늘 신규진입", f"{int(getattr(state, 'daily_single_entry_count', 0) or 0)} / {macd2_config.SINGLE_ENTRY_MAX_DAILY_ENTRIES}")
    se4.metric(
        "마지막 승인 시각",
        _format_signal_time(getattr(state, "last_single_entry_at", None)) if getattr(state, "last_single_entry_at", None) else "-",
    )
    se5, se6, se7 = st.columns(3)
    se5.metric("오늘 확인한 플래그 수", int(getattr(state, "daily_confirmed_flag_count", 0) or 0))
    _se_score = getattr(state, "last_single_entry_score", None)
    se6.metric("마지막 점수", f"{_se_score:.1f} / {macd2_config.SINGLE_ENTRY_SCORE_MIN:.0f}" if _se_score is not None else "-")
    _se_seq = getattr(state, "last_single_entry_flag_seq", None)
    se7.metric("마지막 플래그 순번", int(_se_seq) if _se_seq is not None else "-")
    st.caption(
        f"enabled_at=`{getattr(state, 'single_entry_filter_enabled_at', None) or '-'}` · "
        f"by=`{getattr(state, 'single_entry_filter_enabled_by', None) or '-'}` · "
        f"decision=`{getattr(state, 'last_single_entry_decision', None) or '-'}` · "
        f"near_zero_blue=`{getattr(state, 'last_single_entry_near_zero_blue', None)}`"
    )
    st.info(
        f"2% 3회진입 기준 v3(ON일 때 강한 플래그·추세전환장·Trend Persistence 필터보다 우선순위 낮음, 진입권한만 결정): "
        "그날의 모든 confirmed 플래그를 계속 평가 — 4번째 이후도 자동차단하지 않고, 1~3번째도 점수 미달이면 진입하지 않음. "
        f"점수 = MAJOR 0-100점 + 순번가산(1번째+{macd2_config.SINGLE_ENTRY_SEQ_BONUS_1:.0f}/2번째+{macd2_config.SINGLE_ENTRY_SEQ_BONUS_2:.0f}/3번째+{macd2_config.SINGLE_ENTRY_SEQ_BONUS_3:.0f}/4번째부터+0) "
        "+ gap확장·EMA10기울기·최근15분기울기 방향일치 가점 각 "
        f"+{macd2_config.SINGLE_ENTRY_GAP_EXPANSION_BONUS:.0f}/+{macd2_config.SINGLE_ENTRY_EMA10_SLOPE_BONUS:.0f}/+{macd2_config.SINGLE_ENTRY_PRICE_SLOPE_15M_BONUS:.0f} "
        f"- 과열감점 -{macd2_config.SINGLE_ENTRY_OVERHEAT_PENALTY:.0f}(price_impulse_atr>={macd2_config.SINGLE_ENTRY_OVERHEAT_THRESHOLD:.1f}), "
        f"합산 {macd2_config.SINGLE_ENTRY_SCORE_MIN:.0f}점 이상이면 진입 승인, 신규진입이 이미 {macd2_config.SINGLE_ENTRY_MAX_DAILY_ENTRIES}회면 차단 — "
        "이후엔 반대 신호가 나와도 청산만(재진입 없음), Stop Loss/Profit Lock/퀵 Profit 익절/15:00 강제청산은 그대로 적용. "
        "near-zero BLUE(|MACD|<3000)는 진단값만 기록, 점수 미반영. "
        "2026-07-03~08-07 25거래일, 실제 worker.run_once()로 두 방식 모두 재현(000660, 동일 비용/청산 규칙): "
        "구 순번캡 방식(v2) 거래 75건(3.00/일)·승률 53.3%·퀵Profit청산률 50.7%·Net 1,737,014·PF 1.25·MDD 1,282,201 대비, "
        "이 v3 방식(threshold=42)은 거래 73건(2.92/일)·승률 54.8%·퀵Profit청산률 53.4%·Net 2,658,576(+53%)·PF 1.43·MDD 1,445,338 "
        "(MDD만 12.7% 높고 승률/청산률/Net/PF는 전부 개선). 단, v3에서 승인된 4번째 이후 플래그(73건 중 7건)는 이 표본에서 "
        "1~3번째보다 성과가 낮았음(승률 28.6% vs 57.6%) — 사용자 요청에 따라 그대로 두었으나 데이터가 더 쌓이면 재검증 필요. "
        "퀵 Profit 익절(기본 +2.0% 익절)을 함께 켜면 2% 도달 시점에 실제로 확정 가능."
    )

    st.markdown("**Profit Lock — MACD 수렴 조기청산 (청산 로직 전용, 기본 OFF)**")
    pl1, pl2, pl3, pl4 = st.columns(4)
    pl1.metric("Profit Lock", "ON" if getattr(state, "profit_lock_enabled", False) else "OFF")
    pl2.metric("진입 후 경과 완성봉", f"{int(getattr(state, 'profit_lock_bars_since_entry', 0) or 0)} / {macd2_config.PROFIT_LOCK_MIN_BARS_SINCE_ENTRY}")
    pl3.metric("연속 축소 봉수", f"{int(getattr(state, 'profit_lock_contraction_count', 0) or 0)} / {macd2_config.PROFIT_LOCK_MIN_CONSECUTIVE_CONTRACTIONS}")
    _pl_gap_ratio = getattr(state, "profit_lock_gap_ratio", None)
    pl4.metric("간격비율", f"{_pl_gap_ratio*100:.1f}%" if _pl_gap_ratio is not None else "-")
    pl5, pl6, pl7 = st.columns(3)
    pl5.metric("현재 support_gap", f"{getattr(state, 'profit_lock_current_support_gap', None):.4f}" if getattr(state, "profit_lock_current_support_gap", None) is not None else "-")
    pl6.metric("최고수익률", f"{float(getattr(state, 'profit_lock_peak_return_pct', 0.0) or 0.0):.2f}%")
    pl7.metric("최고수익 반납", f"{float(getattr(state, 'profit_lock_drawdown_pct', 0.0) or 0.0):.2f}%p")
    st.caption(
        f"enabled_at=`{getattr(state, 'profit_lock_enabled_at', None) or '-'}` · "
        f"by=`{getattr(state, 'profit_lock_enabled_by', None) or '-'}`"
    )
    st.info(
        f"진입권한(일반거래/강한 플래그/추세전환장)과 무관하게 독립 적용되는 청산 전용 필터, 퀵 Profit 익절과 동시 ON 불가. "
        f"ON이면(기본값 OFF) 보유 방향 기준 MACD-Signal 간격(0193T0 보유: MACD-Signal / 0197X0 보유: Signal-MACD)이 완성 3분봉마다 "
        f"수렴하는지 판정해서, ①실제 ETF 수익률 +{macd2_config.PROFIT_LOCK_MIN_NET_RETURN_PCT}% 이상 ②진입 후 완성 3분봉 "
        f"{macd2_config.PROFIT_LOCK_MIN_BARS_SINCE_ENTRY}개 이상 경과 ③간격 {macd2_config.PROFIT_LOCK_MIN_CONSECUTIVE_CONTRACTIONS}개 "
        f"완성봉 연속 축소 ④간격이 보유 중 최대 간격의 {macd2_config.PROFIT_LOCK_MAX_GAP_RATIO*100:.0f}% 이하 ⑤최고수익률에서 "
        f"{macd2_config.PROFIT_LOCK_MIN_DRAWDOWN_PP}%p 이상 반납 — 5개 조건을 모두 만족하면 전량 매도"
        f"({macd2_config.EXIT_PROFIT_LOCK_MACD_CONVERGENCE}). 진행 중인(미완성) 3분봉으로는 절대 판정하지 않음. "
        "support_gap이 0 이하가 되면 반대 플래그 청산이 우선 적용되어 이 필터는 관여하지 않음. "
        "기존 -1.5% 손절/반대 플래그 청산/15:00 강제청산 규칙은 그대로, 이보다 우선 적용됨. OFF면 이 규칙 자체가 없던 것과 동일."
    )

    st.markdown("**퀵 Profit 익절 필터 (청산 로직 전용)**")
    qp1, qp2 = st.columns(2)
    qp1.metric("퀵 Profit 익절", "ON" if getattr(state, "quick_profit_enabled", False) else "OFF")
    qp2.metric("익절 문턱", f"+{macd2_config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT}%")
    st.caption(
        f"enabled_at=`{getattr(state, 'quick_profit_enabled_at', None) or '-'}` · "
        f"by=`{getattr(state, 'quick_profit_enabled_by', None) or '-'}`"
    )
    st.info(
        f"진입권한(일반거래/강한 플래그/추세전환장)과 무관하게 독립 적용되는 청산 전용 필터, Profit Lock과 동시 ON 불가. "
        f"ON이면 보유 포지션 순수익률이 +{macd2_config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT}%에 도달하는 즉시 전량 익절"
        f"({macd2_config.EXIT_QUICK_PROFIT_TAKE_PROFIT}) — 기존 -1.5% 손절/반대 플래그 청산/15:00 강제청산/Profit Lock 규칙은 그대로 적용되고, "
        "이 필터는 그 위에 얹혀서만 동작함. OFF면 이 규칙 자체가 없던 것과 동일."
    )

    st.markdown("**현재 confirmed 신호 / MAJOR 판정**")
    ms1, ms2, ms3, ms4 = st.columns(4)
    ms1.metric("원본 flag", state.latest_primary_flag.value if state.latest_primary_flag else "-")
    _score = getattr(state, "last_major_score", None)
    _req = getattr(state, "last_major_required_score", None)
    ms2.metric(
        "major score / required",
        f"{_score:.0f} / {_req:.0f}" if _score is not None and _req is not None else "-",
    )
    _approved = getattr(state, "last_major_approved", None)
    if _approved is True:
        _maj_status = "APPROVED"
    elif _approved is False:
        _maj_status = "FILTERED_OUT"
    else:
        _maj_status = "-"
    ms3.metric("MAJOR 결과", _maj_status)
    ms4.metric("block reason", getattr(state, "last_major_block_reason", None) or "-")
    st.caption(
        f"decision=`{getattr(state, 'last_major_decision', None) or '-'}` · "
        f"signal_id=`{getattr(state, 'last_major_signal_id', None) or state.latest_primary_signal_id or '-'}` · "
        f"components=`{getattr(state, 'last_major_component_scores', None) or '-'}`"
    )

    st.markdown("**Primary (완성봉 MACD color flag — 유일한 주문권한)**")
    st.caption("아래 diff/MACD는 진행 중(미완성) 3분봉의 shadow 진단값이며 주문에 사용되지 않는다. 실제 주문권한은 latest_primary_flag(완성봉)에만 있다.")
    pc1, pc2, pc3, pc4 = st.columns(4)
    pc1.metric("MACD (진행봉 shadow)", f"{state.provisional_macd:.6f}" if state.provisional_macd is not None else "-")
    pc2.metric("Signal (진행봉 shadow)", f"{state.provisional_signal:.6f}" if state.provisional_signal is not None else "-")
    prev_diff = state.primary_current_diff
    curr_diff = state.provisional_diff
    pc3.metric("previous diff (완성봉)", f"{prev_diff:.6f}" if prev_diff is not None else "-")
    pc4.metric("current diff (진행봉 shadow)", f"{curr_diff:.6f}" if curr_diff is not None else "-")
    fc1, fc2, fc3, fc4 = st.columns(4)
    fc1.metric(
        "candidate flag (진행봉, 주문권한 없음)",
        f"CANDIDATE_{state.candidate_flag.value}" if state.candidate_flag else "-",
    )
    fc2.metric("candidate confirmed (여전히 shadow)", state.provisional_flag.value if state.provisional_flag else "-")
    fc3.metric("last confirmed onset (완성봉, 주문권한)", state.latest_primary_flag.value if state.latest_primary_flag else "-")
    fc4.metric("broker order result", _broker_order_result_display(state))
    st.caption(
        f"current signal_id=`{state.provisional_signal_id or '-'}` · "
        f"last primary onset signal_id=`{state.latest_primary_signal_id or '-'}` · "
        f"candidate since=`{state.candidate_first_seen_at or '-'}` (first diff `{state.candidate_first_diff if state.candidate_first_diff is not None else '-'}`) · "
        f"last confirmed at=`{state.candidate_confirmed_at or '-'}` (confirmed diff `{state.candidate_confirmed_diff if state.candidate_confirmed_diff is not None else '-'}`)"
    )
    st.caption(
        f"broker order: id=`{state.last_broker_order_id or '-'}` · "
        f"symbol=`{state.last_broker_order_symbol or '-'}` · side=`{state.last_broker_order_side or '-'}` · "
        f"at=`{state.last_broker_order_at or '-'}`"
    )
    if _broker_order_result_display(state) == "LEGACY_ORDER_REJECTED_PRE_FIX":
        st.caption(
            "legacy broker message from a pre-fix order is hidden from the current status; "
            "new attempts use KIS nrcvb_buy_qty/final_qty diagnostics below."
        )
    st.caption(
        f"order failure detail: stage=`{state.last_order_failure_stage or '-'}` · "
        f"filled_qty=`{state.last_order_filled_qty if state.last_order_filled_qty is not None else '-'}` · "
        f"fill_poll_result=`{state.last_order_fill_poll_result or '-'}` · "
        f"balance_qty=`{state.last_order_balance_qty if state.last_order_balance_qty is not None else '-'}`"
    )
    if state.last_duplicate_signal_id:
        st.warning(f"signal ledger 중복 기록 거부됨 — signal_id=`{state.last_duplicate_signal_id}` (이미 기록된 signal_id)")
    st.markdown("**주문 sizing (직전 진입/스위치 시도)**")
    if state.last_order_orderable_cash is not None:
        oc1, oc2, oc3, oc4 = st.columns(4)
        oc1.metric("orderable_cash", f"{state.last_order_orderable_cash:,.0f}")
        oc2.metric("nrcvb_buy_amt", f"{state.last_order_nrcvb_buy_amt:,.0f}" if state.last_order_nrcvb_buy_amt is not None else "-")
        oc3.metric("nrcvb_buy_qty", state.last_order_nrcvb_buy_qty if state.last_order_nrcvb_buy_qty is not None else "-")
        oc4.metric("final_qty", state.last_order_final_qty if state.last_order_final_qty is not None else "-")
        oc5, oc6, oc7, oc8 = st.columns(4)
        oc5.metric("budget_qty", state.last_order_budget_qty if state.last_order_budget_qty is not None else "-")
        oc6.metric("psbl_qty_calc_unpr", f"{state.last_order_psbl_qty_calc_unpr:,.0f}" if state.last_order_psbl_qty_calc_unpr is not None else "-")
        oc7.metric("order_price", f"{state.last_order_order_price:,.2f}" if state.last_order_order_price is not None else "-")
        oc8.metric("expected_amount", f"{state.last_order_expected_amount:,.0f}" if state.last_order_expected_amount is not None else "-")
        oc9, oc10, oc11, oc12 = st.columns(4)
        oc9.metric("ask1", f"{state.last_order_ask1:,.2f}" if state.last_order_ask1 is not None else "-")
        oc10.metric("order_type", state.last_order_order_type or "-")
        oc11.metric("usable_cash", f"{state.last_order_usable_cash:,.0f}" if state.last_order_usable_cash is not None else "-")
        oc12.metric("limit_buyable_qty", state.last_order_limit_buyable_qty if state.last_order_limit_buyable_qty is not None else "-")
        st.caption(
            f"KIS sizing response: rt_cd=`{state.last_order_sizing_rt_cd or '-'}` · "
            f"msg_cd=`{state.last_order_sizing_msg_cd or '-'}` · msg1=`{state.last_order_sizing_msg1 or '-'}`"
        )
    else:
        st.caption("orderable_cash=`-` · nrcvb_buy_qty=`-` · budget_qty=`-` · final_qty=`-` · expected_amount=`-`")

    st.markdown("**QUOTE_STALE 재조회 진단 (2026-07-27)**")
    st.caption(
        f"signal_id=`{state.last_quote_stale_signal_id or '-'}` · "
        f"quote_ages(감지 시점)=`{state.last_quote_stale_quote_ages or '-'}` · "
        f"재조회 횟수=`{state.last_quote_stale_retry_count if state.last_quote_stale_retry_count is not None else '-'}` · "
        f"결과=`{state.last_quote_stale_result or '-'}`"
    )
    if state.last_quote_stale_result == macd2_config.MISSED_SIGNAL_QUOTE_STALE:
        st.warning(
            f"signal_id=`{state.last_quote_stale_signal_id}`은 15초 내 fresh quote를 확보하지 못해 "
            f"주문하지 않고 `{macd2_config.MISSED_SIGNAL_QUOTE_STALE}`로 종료되었습니다."
        )

    st.markdown("**1분봉 history 진단 (KIS 당일 1분봉 = 단일 원본)**")
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("당일 1분봉 수", state.today_1m_bar_count if state.today_1m_bar_count is not None else "-")
    h2.metric("history 최신시각", _format_signal_time(state.history_newest_at) if state.history_newest_at else "-")
    h3.metric("마지막 완성 3분봉", _format_signal_time(state.last_completed_3m_bar_at) if state.last_completed_3m_bar_at else "-")
    h4.metric("quote-history 불일치", state.quote_history_mismatch_reason or "OK")
    if state.quote_history_mismatch_reason:
        st.error(f"quote/1분봉 history 단위·시각 불일치로 신규 진입이 차단됩니다 — 원인: `{state.quote_history_mismatch_reason}`")

    st.markdown("**Provisional forming-bar crossover**")
    pr1, pr2, pr3, pr4 = st.columns(4)
    pr1.metric("MACD", f"{state.provisional_macd:.6f}" if state.provisional_macd is not None else "-")
    pr2.metric("Signal", f"{state.provisional_signal:.6f}" if state.provisional_signal is not None else "-")
    pr3.metric("diff", f"{state.provisional_diff:.6f}" if state.provisional_diff is not None else "-")
    pr4.metric("flag", state.provisional_flag.value if state.provisional_flag else "-")
    st.caption(
        f"forming=`{state.provisional_bar_start or '-'}` -> `{state.provisional_bar_end or '-'}` · "
        f"signal_id=`{state.provisional_signal_id or '-'}` · "
        f"detected_at=`{state.provisional_detected_at or '-'}` · "
        f"order_requested_at=`{state.provisional_order_requested_at or '-'}` · "
        f"input_now=`{getattr(state, 'provisional_input_now', None) or '-'}` · "
        f"quote=`{getattr(state, 'provisional_quote_price', None) or '-'}` · "
        f"last_1m=`{getattr(state, 'provisional_last_1m_at', None) or '-'}`/"
        f"`{getattr(state, 'provisional_last_1m_close', None) or '-'}` · "
        f"scale=`{getattr(state, 'provisional_price_scale_note', None) or '-'}`"
    )

    st.markdown("**Signed-B shadow**")
    st.caption(
        f"hist_last3=`{state.signed_b_shadow_hist_last3 or '-'}` · "
        f"signed-B=`{state.signed_b_shadow_direction.value if state.signed_b_shadow_direction else '-'}` · "
        "order_authority=`NONE`"
    )
except Exception as exc:
    st.error(f"상태 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")

# ── 운영 진단 (Worker heartbeat + bootstrap/quote diagnostics, isolated) ──
st.subheader("운영 진단")
try:
    sup = service.supervisor_status()
    bootstrap_diag = snapshot.get("bootstrap_diag") or {}
    kis_pages = bootstrap_diag.get("kis_pages") or []
    prior_day_cache = bootstrap_diag.get("prior_day_cache") or {}
    worker_code_sha = (read_runtime_info() or {}).get("git_sha")

    d1, d2, d3 = st.columns(3)
    d1.metric("worker_status", _worker_status(state, worker_stats))
    d2.metric("quote_updater_status", "READY" if sup.get("quote_updater_alive") else "STOPPED")
    d3.metric("active_worker_count", sup.get("active_worker_count", 0))

    st.markdown("**Worker**")
    w1, w2, w3, w4 = st.columns(4)
    w1.metric("worker_instance_id", worker_stats.get("instance_id") or "-")
    w2.metric("worker_started_at", worker_stats.get("started_at") or "-")
    w3.metric("worker_code_sha", (worker_code_sha or "-")[:12])
    w4.metric("tick_seq_total", worker_stats.get("tick_n", 0))

    w5, w6, w7, w8 = st.columns(4)
    w5.metric("recent_tick_sample_count", worker_stats.get("recent_tick_sample_count", 0))
    w6.metric("last_tick_at", worker_stats.get("last_tick_at") or "-")
    last_tick_age = worker_stats.get("last_tick_age_sec")
    w7.metric("last_tick_age_sec", f"{last_tick_age:.1f}" if last_tick_age is not None else "-")
    w8.metric("next_tick_at", worker_stats.get("next_tick_at") or "-")

    w9, w10, w11 = st.columns(3)
    mean_iv, p95_iv, max_iv = (
        worker_stats.get("mean_interval_sec"), worker_stats.get("p95_interval_sec"), worker_stats.get("max_interval_sec"),
    )
    w9.metric("tick mean(s)", f"{mean_iv:.2f}" if mean_iv is not None else "-")
    w10.metric("tick p95(s)", f"{p95_iv:.2f}" if p95_iv is not None else "-")
    w11.metric("tick max(s)", f"{max_iv:.2f}" if max_iv is not None else "-")
    if worker_stats.get("last_exception"):
        st.error(f"Worker last_exception: `{worker_stats['last_exception']}`")

    st.markdown("**Quote**")
    for symbol, label in (
        (macd2_config.WATCH_SYMBOL, "SK하이닉스 000660"),
        (macd2_config.LONG_SYMBOL, "KODEX 0193T0"),
        (macd2_config.INVERSE_SYMBOL, "SOL 0197X0"),
    ):
        snap = quotes.get(symbol)
        if snap is None:
            st.write(f"- `{symbol}` ({label}): price=- fetched_at=- age=- error=-")
        else:
            fetched_at = snap.fetched_at.isoformat() if snap.fetched_at else "-"
            age = f"{snap.age_sec:.1f}s" if snap.age_sec is not None else "-"
            st.write(
                f"- `{symbol}` ({label}): price={snap.price:,.0f} fetched_at={fetched_at} "
                f"age={age} error=`{snap.error or '-'}`"
            )

    st.markdown("**Bootstrap**")
    bs1, bs2, bs3 = st.columns(3)
    bs1.metric("bootstrap_last_attempt_at", snapshot.get("bootstrap_last_attempt_at") or "-")
    bs2.metric("bootstrap_retry_count", snapshot.get("bootstrap_attempts", 0))
    bs3.metric("requested trading date", bootstrap_diag.get("requested_trading_date") or "-")

    bs4, bs5, bs6 = st.columns(3)
    bs4.metric("received_1m_bars", (bootstrap_last_result or {}).get("received_1m_bars", "-"))
    bs5.metric("completed_3m_bars", (bootstrap_last_result or {}).get("completed_3m_count", "-"))
    bs6.metric("warmup_ready", "YES" if state.warmup_ready else "NO")

    st.caption(
        f"merged oldest/newest: `{bootstrap_diag.get('merged_oldest') or '-'}` ~ "
        f"`{bootstrap_diag.get('merged_newest') or '-'}` · "
        f"prior_day_cache: date=`{prior_day_cache.get('prior_trading_date') or '-'}` "
        f"count={prior_day_cache.get('received_count', '-')} error=`{prior_day_cache.get('error') or '-'}`"
    )

    if kis_pages:
        with st.expander(f"KIS 분봉 요청 상세 ({len(kis_pages)}건)"):
            st.dataframe(pd.DataFrame(kis_pages), use_container_width=True)

    st.caption(f"정확한 block_reason: `{state.order_block_reason or '-'}`")
except Exception as exc:
    st.error(f"운영 진단 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")

# ── 데이터 저장 경로(Persistent Disk) 진단 — Render 등에서 컨테이너 로컬(휘발성)
# 경로에 계속 쓰고 있으면 재배포/재시작마다 state/원장이 사라지고, session_
# started_at이 매번 "방금 재시작한 시각"으로 리셋되어 오늘 이미 실시간으로
# 발생했던 플래그가 전부 HISTORICAL_REPLAY_ONLY로 잘못 표시된다(2026-08-04
# 실측). AI_GAP_DATA_DIR이 실제로 적용됐는지, 그 경로가 진짜로 쓰기 가능한지,
# 그리고 지금 이 세션이 언제부터 이어져 온 것인지를 항상 보이는 곳에 표시한다.
st.subheader("데이터 저장 경로 / 세션 연속성 진단")
try:
    import os as _os

    from app.trading.macd2 import state_store as _macd2_state_store
    from app.utils.data_paths import DATA_ROOT, DATA_ROOT_ENV_VAR, check_writable, file_info

    _data_writable_status = check_writable()
    _dp_cols = st.columns(3)
    _dp_cols[0].metric(
        f"Data root ({'env' if _os.environ.get(DATA_ROOT_ENV_VAR) else 'default'})",
        str(DATA_ROOT),
    )
    _dp_cols[1].metric(
        "Persistent 쓰기 가능",
        "🟢 YES" if _data_writable_status.get("writable") else "🔴 NO",
    )
    _dp_cols[2].metric(f"{DATA_ROOT_ENV_VAR}", _os.environ.get(DATA_ROOT_ENV_VAR) or "(미설정 — 기본값 사용)")
    if not _data_writable_status.get("writable"):
        st.error(
            f"🔴 데이터 루트({DATA_ROOT})에 쓰기 실패 — state/원장이 저장되지 않습니다: "
            f"{_data_writable_status.get('error')}"
        )

    sc1, sc2, sc3 = st.columns(3)
    sc1.metric("session_started_at (오늘 이어져 온 시각)", state.session_started_at or "-")
    sc2.metric("last_confirmed_bar_ts", state.last_confirmed_bar_ts or "-")
    sc3.metric("worker_instance_id", state.worker_instance_id or "-")
    st.caption(
        "session_started_at이 실제로 오늘 처음 자동매매를 시작한 시각이 아니라 "
        "최근 재배포/재시작 시각으로 자주 바뀐다면, 위 쓰기 테스트가 성공이어도 "
        "실제로는 재시작 사이에 state 파일이 이어지지 않고 있다는 뜻입니다."
    )

    with st.expander("💾 데이터 저장 경로 상세(state/원장 파일 실제 경로·크기·수정시각)"):
        _macd2_state_info = file_info(_macd2_state_store.STATE_PATH)
        _macd2_signal_ledger_info = file_info(ledger.SIGNAL_LEDGER_PATH)
        _macd2_execution_ledger_info = file_info(ledger.EXECUTION_LEDGER_PATH)
        st.markdown(f"**MACD2 state 실제 경로**: `{_macd2_state_info['path']}`")
        st.json(_macd2_state_info)
        st.markdown(f"**신호원장(signal ledger) 실제 경로**: `{_macd2_signal_ledger_info['path']}`")
        st.json(_macd2_signal_ledger_info)
        st.markdown(f"**체결원장(execution ledger) 실제 경로**: `{_macd2_execution_ledger_info['path']}`")
        st.json(_macd2_execution_ledger_info)
        st.caption(
            f"마지막 쓰기 테스트: {_data_writable_status.get('checked_at') or '—'} "
            f"({'성공' if _data_writable_status.get('writable') else '실패: ' + str(_data_writable_status.get('error'))})"
        )
except Exception as exc:
    st.error(f"데이터 저장 경로 진단 실패 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")

# ── Daily stats (isolated) ──────────────────────────────────────────────
st.subheader("오늘 신호·거래 통계")
try:
    trading_date = (state.session_date or pd.Timestamp.now().strftime("%Y%m%d"))
    # docs §2: only THIS deployed code's SHA counts toward "current" stats —
    # a row from a different (older) worker_code_sha is excluded here and
    # only ever shown in the "과거/제외 신호" panel below.
    _current_worker_sha = snapshot.get("worker_code_sha") or None
    sig_summary = ledger.summarize_signals(
        trading_date,
        strategy_version=state.strategy_version,
        signal_rule=state.signal_rule,
        session_started_at=state.session_started_at,
        worker_code_sha=_current_worker_sha,
    )
    confirmed_summary = ledger.summarize_signals(
        trading_date,
        strategy_version=state.strategy_version,
        signal_rule=getattr(macd2_config, "CONFIRMED_SIGNAL_RULE", "KIS_MACD_COLOR_FLAG_CONFIRMED"),
        session_started_at=state.session_started_at,
        worker_code_sha=_current_worker_sha,
    )
    trade_summary = ledger.summarize_daily_trading(
        trading_date,
        budget=state.budget,
    )

    # 2026-08-05 fix: "오늘 빨간/파란 플래그" 개수는 신호 원장(signal ledger) 기반
    # sig_summary 대신, 오늘 하루 전체를 KIS 1분봉 이력에서 그대로 재계산하는
    # today_signal_overview(worker.compute_today_signal_overview)로 표시한다.
    # Render 무료 플랜은 재배포 시 data/ 아래 원장 CSV가 초기화되므로(docs
    # deploy_render.md), 원장 기반 집계는 재배포 이전 플래그를 계속 0건으로
    # 잃어버렸다 — 1분봉 이력은 재배포 후에도 KIS에서 당일치를 다시 받아오므로
    # 재배포 여부와 무관하게 오늘 발생한 플래그 전체가 항상 표시된다.
    _overview_all_today = snapshot.get("today_signal_overview") or []
    _today_red_count = sum(1 for r in _overview_all_today if r.get("direction") == "UP_RED")
    _today_blue_count = sum(1 for r in _overview_all_today if r.get("direction") == "DOWN_BLUE")

    g1, g2, g3 = st.columns(3)
    g1.metric("오늘 빨간 플래그", f"{_today_red_count}건")
    g2.metric("오늘 파란 플래그", f"{_today_blue_count}건")
    g3.metric("완료 거래", f"{trade_summary['round_trip_count']}건")
    st.caption("재배포와 무관하게 오늘 하루 전체 1분봉 이력에서 재계산한 건수입니다.")

    # MAJOR filter stats (원본 flag vs 승인 분리)
    _all_today = [r for r in ledger.load_signal_ledger() if r.get("trading_date") == trading_date]
    _onset = sig_summary.get("onset_signals") or []
    _major_approved_rows = [
        r for r in _onset
        if str(r.get("major_approved") or "").lower() in {"true", "1", "yes"}
        or str(r.get("major_decision") or "") == macd2_config.MAJOR_APPROVED
    ]
    _major_red = sum(1 for r in _major_approved_rows if r.get("direction") == "UP_RED")
    _major_blue = sum(1 for r in _major_approved_rows if r.get("direction") == "DOWN_BLUE")
    _filtered = [
        r for r in _onset
        if str(r.get("order_result") or "").upper() == macd2_config.FILTERED_OUT
        or str(r.get("major_approved") or "").lower() in {"false", "0", "no"}
    ]
    _filled_entries = int(trade_summary.get("buy_count") or 0)
    mg1, mg2, mg3, mg4, mg5, mg6 = st.columns(6)
    mg1.metric("원본 빨간 플래그", _today_red_count)
    mg2.metric("원본 파란 플래그", _today_blue_count)
    mg3.metric("MAJOR 승인 빨강", _major_red)
    mg4.metric("MAJOR 승인 파랑", _major_blue)
    mg5.metric("필터 탈락", len(_filtered))
    mg6.metric("실제 체결 진입", _filled_entries)

    if _filtered:
        st.markdown("**필터 탈락 신호**")
        _filt_rows = []
        for row in _filtered:
            _filt_rows.append({
                "시간": _signal_display_time(row),
                "방향": row.get("direction") or "-",
                "score": row.get("major_score") or "-",
                "required": row.get("major_required_score") or "-",
                "block reason": row.get("major_block_reason") or row.get("block_reason") or "-",
                "components": row.get("major_component_scores") or "-",
            })
        st.dataframe(pd.DataFrame(_filt_rows), use_container_width=True, height=220)

    st.caption(
        "KIS manual arrows today=- · "
        f"system provisional red/blue={sig_summary['red_count']}/{sig_summary['blue_count']} · "
        f"system confirmed red/blue={confirmed_summary['red_count']}/{confirmed_summary['blue_count']}"
    )
    onset_rows = sig_summary.get("onset_signals") or []
    if onset_rows:
        flag_rows = []
        for row in onset_rows:
            requested_at = _order_requested_at(row)
            v6_result, v6_unmet = _v6_unmet_summary(row)
            flag_rows.append({
                "flag_time": _signal_display_time(row),
                "direction": row.get("direction") or "-",
                "signal_id": row.get("signal_id") or "-",
                "entered": _trade_entered_status(row, requested_at),
                "v6_result": v6_result,
                "v6_unmet": v6_unmet,
                "v6_score": row.get("major_score") or "-",
                "v6_required": row.get("major_required_score") or "-",
                "v6_decision": row.get("major_decision") or "-",
                "order_requested": "YES" if requested_at != "-" else "NO",
                "order_requested_at": requested_at,
                "order_result": str(row.get("order_result") or "-"),
                "not_ordered_reason": _not_ordered_reason(row, requested_at),
                "failure_stage": row.get("failure_stage") or "-",
                "orderable_cash": row.get("orderable_cash") or "-",
                "nrcvb_buy_amt": row.get("nrcvb_buy_amt") or "-",
                "nrcvb_buy_qty": row.get("nrcvb_buy_qty") or "-",
                "psbl_qty_calc_unpr": row.get("psbl_qty_calc_unpr") or "-",
                "ask1": row.get("ask1") or "-",
                "order_price": row.get("order_price") or "-",
                "order_type": row.get("order_type") or "-",
                "usable_cash": row.get("usable_cash") or "-",
                "limit_buyable_qty": row.get("limit_buyable_qty") or "-",
                "budget_qty": row.get("budget_qty") or "-",
                "final_qty": row.get("final_qty") or "-",
                "sizing_price": row.get("sizing_price") or "-",
                "requested_qty": row.get("requested_qty") or "-",
                "expected_amount": row.get("expected_amount") or "-",
                "sizing_rt_cd": row.get("sizing_rt_cd") or "-",
                "sizing_msg_cd": row.get("sizing_msg_cd") or "-",
                "sizing_msg1": row.get("sizing_msg1") or "-",
                "broker_called": row.get("broker_called") or "-",
                "rt_cd": row.get("broker_rt_cd") or "-",
                "msg_cd": row.get("broker_msg_cd") or "-",
                "msg1": row.get("broker_msg1") or "-",
                "order_id": row.get("broker_order_id") or "-",
                "filled_qty": row.get("filled_qty") or "-",
                "fill_poll_result": row.get("fill_poll_result") or "-",
                "balance_qty": row.get("balance_qty") or "-",
            })
        red_times = [r["flag_time"] for r in flag_rows if r["direction"] == "UP_RED"]
        blue_times = [r["flag_time"] for r in flag_rows if r["direction"] == "DOWN_BLUE"]
        st.caption(f"빨간 플래그 {len(red_times)}건: {', '.join(red_times) if red_times else '-'}")
        st.caption(f"파란 플래그 {len(blue_times)}건: {', '.join(blue_times) if blue_times else '-'}")
        st.dataframe(pd.DataFrame(flag_rows), use_container_width=True, height=260)
    else:
        st.caption("No current-version provisional flags today.")

    for u in sig_summary.get("unexecuted_signals") or []:
        st.write(f"- `{u.get('signal_id')}` · {u.get('direction')} · 사유 `{u.get('reason')}`")
    excluded = sig_summary.get("excluded_signals") or []
    if excluded:
        with st.expander(f"과거/제외 신호 ({len(excluded)}건)"):
            st.dataframe(pd.DataFrame(excluded), use_container_width=True, height=260)

    # docs §3: recomputed today-overview, LIVE_CONFIRMED vs
    # HISTORICAL_REPLAY_ONLY — display only, never an order/filter input.
    _overview = snapshot.get("today_signal_overview") or []
    _live = [r for r in _overview if r.get("origin") == "LIVE_CONFIRMED"]
    _historical = [r for r in _overview if r.get("origin") == "HISTORICAL_REPLAY_ONLY"]
    st.markdown("**오늘 전체 신호 개요 (재계산, 참고용 — 주문권한 없음)**")
    ov1, ov2 = st.columns(2)
    ov1.metric("LIVE_CONFIRMED (Worker 시작 이후)", f"{len(_live)}건")
    ov2.metric("HISTORICAL_REPLAY_ONLY (Worker 시작 전)", f"{len(_historical)}건")
    if _overview:
        st.dataframe(pd.DataFrame(_overview), use_container_width=True, height=200)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Gross", f"{trade_summary['gross_pnl']:,.0f}원")
    m2.metric("비용", f"{trade_summary['total_cost']:,.0f}원")
    m3.metric("Net", f"{trade_summary['net_pnl']:,.0f}원")
    m4.metric("수익률", f"{trade_summary['return_pct']:.4f}%")
    m5.metric("승률", f"{trade_summary['win_rate_pct']:.1f}%")
except Exception as exc:
    st.error(f"통계 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")

# ── Ledger (isolated) ───────────────────────────────────────────────────
st.subheader("거래 원장")
try:
    rows = ledger.load_execution_ledger(limit=300)
    today_rows = ledger.filter_execution_rows_by_trading_date(rows, trading_date)
    st.caption(
        f"execution ledger path=`{ledger.EXECUTION_LEDGER_PATH}` · "
        f"loaded_rows={len(rows)} · today_rows={len(today_rows)}"
    )
    if today_rows:
        df = pd.DataFrame(today_rows)
        st.dataframe(df.iloc[::-1], use_container_width=True, height=360)
    elif rows:
        st.caption("오늘 원장 없음")
    else:
        st.caption("원장 없음")
except Exception as exc:
    st.error(f"원장 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")
