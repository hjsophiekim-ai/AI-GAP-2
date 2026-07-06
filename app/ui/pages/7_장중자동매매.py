"""
7_장중자동매매.py — 완전 자동 장중매매
기본 전략: Top3 시간분산 매수 + 3% 자동익절 (top3_timed_buy_3pct_takeprofit)
기존 전략: 눌림목 1종목 / 고수 눌림목 Top3 동시 감시 (하위 호환)
작업 모드: 매수+매도 / 자동매수만 / 자동매도만 (3%익절/1.5%손절)
"""
import sys
import csv
import json
from pathlib import Path
from datetime import datetime

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st
import pandas as pd

try:
    from streamlit_autorefresh import st_autorefresh
    _AUTOREFRESH_AVAILABLE = True
except ImportError:
    _AUTOREFRESH_AVAILABLE = False

try:
    from app.config import get_config
    from app.trading.broker_factory import create_broker
    from app.services.intraday_budget_allocator import IntradayBudgetAllocator
    from app.services.intraday_auto_trade_service import (
        IntradayAutoTradeService,
        Top3TimedBuyService,
        STATUS_WAITING, STATUS_PENDING, STATUS_HOLDING,
        STATUS_HALF_SOLD, STATUS_COOLING, STATUS_DONE, STATUS_ERROR,
        _ROOT as _SVC_ROOT,
    )
except Exception as e:
    st.error(f"모듈 로드 오류: {e}")
    st.stop()

try:
    from app.services.master_pullback_top3_service import (
        MasterPullbackTop3Service,
        STRATEGY_NAME as _MASTER_PB_STRATEGY_NAME,
    )
    _MASTER_PB_AVAILABLE = True
except Exception:
    _MASTER_PB_AVAILABLE = False

cfg = get_config()
_today = datetime.now().strftime("%Y%m%d")
_ic = cfg._raw.get("intraday_auto_trade", {})

# ── 전략 상수 ──────────────────────────────────────────────────────────────────
_STRATEGY_TIMED = "Top3 시간분산 매수 + 3% 자동익절"
_STRATEGY_MASTER_PB = "고수 눌림목 Top3 동시 감시"
_STRATEGY_LEGACY = "기존 자동매매 (눌림목 1종목)"

# ── 작업 모드 상수 ─────────────────────────────────────────────────────────────
_MODE_BUY_AND_SELL = "매수 + 매도 (기본)"
_MODE_BUY_ONLY    = "자동매수만 (매도 수동)"
_MODE_SELL_ONLY   = "자동매도만 (3%익절 / 1.5%손절)"
_SELL_ONLY_TAKE_PROFIT = 3.0
_SELL_ONLY_STOP_LOSS   = -1.5

# ── 유틸 ───────────────────────────────────────────────────────────────────────
_STATUS_EMOJI = {
    STATUS_WAITING:  "⏳ 대기",
    STATUS_PENDING:  "📤 주문중",
    STATUS_HOLDING:  "📈 보유중",
    STATUS_HALF_SOLD:"📉 절반매도",
    STATUS_COOLING:  "❄️ 쿨다운",
    STATUS_DONE:     "✅ 완료",
    STATUS_ERROR:    "❌ 오류",
    "WAITING":       "⏳ 대기",
    "HOLDING":       "📈 보유중",
    "SOLD":          "✅ 매도완료",
    "BUY_FAILED":    "❌ 매수실패",
    "ERROR":         "❌ 오류",
}

def _format_price(v):
    try:
        return f"{int(float(v)):,}"
    except Exception:
        return "-"

def _format_pct(v):
    try:
        return f"{float(v):+.2f}%"
    except Exception:
        return "-"

def _format_amt(v):
    try:
        v = float(v)
        if abs(v) >= 1_000_000:
            return f"{v/1_000_000:.1f}백만"
        return f"{v:,.0f}"
    except Exception:
        return "-"

def _safe_create_broker(mode, confirm_text="", runtime_real_mode=False,
                        runtime_enable_real_buy=False, runtime_enable_real_sell=False):
    try:
        return create_broker(cfg=cfg, mode=mode, confirm_text=confirm_text,
                             runtime_real_mode=runtime_real_mode,
                             runtime_enable_real_buy=runtime_enable_real_buy,
                             runtime_enable_real_sell=runtime_enable_real_sell)
    except TypeError:
        try:
            return create_broker(cfg=cfg, mode=mode, confirm_text=confirm_text,
                                 runtime_real_mode=runtime_real_mode)
        except TypeError:
            return create_broker(cfg=cfg, mode=mode)


def _load_timed_state() -> dict:
    """top3_timed_buy_3pct_takeprofit 상태파일 읽기."""
    path = _SVC_ROOT / f"data/state/top3_timed_buy_3pct_takeprofit_{_today}.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_timed_log() -> list[dict]:
    """top3_timed_buy_3pct_takeprofit 로그 읽기."""
    path = _SVC_ROOT / f"data/logs/top3_timed_buy_3pct_takeprofit_{_today}.csv"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _load_legacy_state(strategy_key: str) -> dict:
    ic = cfg._raw.get(strategy_key, {})
    if strategy_key == "master_pullback_top3":
        tmpl = ic.get("state_file", "data/state/master_pullback_top3_multi_entry_YYYYMMDD.json")
    else:
        tmpl = ic.get("state_file", "data/state/intraday_auto_trade_state_YYYYMMDD.json")
    path = _SVC_ROOT / tmpl.replace("YYYYMMDD", _today)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_legacy_log(strategy_key: str) -> list[dict]:
    ic = cfg._raw.get(strategy_key, {})
    if strategy_key == "master_pullback_top3":
        tmpl = ic.get("log_file", "data/logs/master_pullback_top3_YYYYMMDD.csv")
    else:
        tmpl = ic.get("log_file", "data/logs/intraday_auto_trades_YYYYMMDD.csv")
    path = _SVC_ROOT / tmpl.replace("YYYYMMDD", _today)
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


# ===========================================================================
# Page
# ===========================================================================
st.title("완전 자동 장중매매 — 주도섹터 Top3")

# ── Render 주의 문구 ──────────────────────────────────────────────────────────
st.info(
    "**Render 주의**: Render Web Service + Streamlit 구조에서는 브라우저 세션이 꺼지거나 "
    "앱이 sleep되면 자동감시가 중단될 수 있습니다. "
    "완전 백그라운드 자동매매가 필요하면 Render Background Worker 또는 별도 스케줄러가 필요합니다.",
    icon="⚠️",
)

st.divider()

# ── 전략 선택 ─────────────────────────────────────────────────────────────────
strategy_options = [_STRATEGY_TIMED, _STRATEGY_MASTER_PB, _STRATEGY_LEGACY]
selected_strategy = st.selectbox(
    "전략 선택",
    options=strategy_options,
    index=0,
    help="기본: Top3 시간분산 매수 + 3% 자동익절 (복잡한 조건 없이 시간 기반 순차매수)",
)

_use_timed = selected_strategy == _STRATEGY_TIMED
_use_master_pb = selected_strategy == _STRATEGY_MASTER_PB and _MASTER_PB_AVAILABLE
_strategy_cfg_key = "master_pullback_top3" if _use_master_pb else "intraday_auto_trade"

# ── 전략 정보 표시 ────────────────────────────────────────────────────────────
if _use_timed:
    sched = _ic.get("buy_schedule", {"rank1": "09:12", "rank2": "09:16", "rank3": "09:20"})
    alloc = _ic.get("budget_allocation", {"rank1": 0.45, "rank2": 0.35, "rank3": 0.20})
    tp_pct = float(_ic.get("take_profit_pct", 3.0))
    sl_pct = float(_ic.get("stop_loss_pct", -1.2))
    sl_enabled = bool(_ic.get("stop_loss_enabled", True))
    force_exit_time = _ic.get("force_exit_time", "15:10")

    st.success(f"🎯 **{_STRATEGY_TIMED}** — 09:10~09:30 시간 기반 순차매수, 1분봉 조건 불필요")
    with st.expander("전략 상세 설정 보기", expanded=True):
        si1, si2, si3 = st.columns(3)
        with si1:
            st.markdown("**매수 스케줄**")
            st.markdown(f"- 1위: `{sched.get('rank1', '09:12')}` ({alloc.get('rank1', 0.45)*100:.0f}%)")
            st.markdown(f"- 2위: `{sched.get('rank2', '09:16')}` ({alloc.get('rank2', 0.35)*100:.0f}%)")
            st.markdown(f"- 3위: `{sched.get('rank3', '09:20')}` ({alloc.get('rank3', 0.20)*100:.0f}%)")
        with si2:
            st.markdown("**매도 기준**")
            st.markdown(f"- 익절: `+{tp_pct:.1f}%`")
            st.markdown(f"- 손절: `{sl_pct:.1f}%` {'(활성)' if sl_enabled else '(비활성)'}")
            st.markdown(f"- 강제청산: `{force_exit_time}`")
        with si3:
            st.markdown("**특징**")
            st.markdown("- 1분봉 데이터 불필요")
            st.markdown("- 종목당 1회 매수 / 1회 매도")
            st.markdown("- 최소 안전조건만 확인")

elif _use_master_pb:
    st.success("🎯 고수 눌림목 Top3 동시 감시 전략 | 오전 9:15~10:00 Top3 순차 매수")
    with st.expander("고수 눌림목 Top3 설정", expanded=False):
        use_morning_budget_ratio = st.slider(
            "오전 매수 예산 비율", min_value=0.5, max_value=1.0, value=1.0, step=0.05)
        allow_second_entry = st.checkbox("10:00 이후 강화 재진입 허용", value=True)
    st.session_state["master_pb_morning_budget_ratio"] = use_morning_budget_ratio
    st.session_state["master_pb_allow_second"] = allow_second_entry

else:
    st.info("기존 자동매매: 1분봉/VWAP/EMA/RSI 기반 눌림목 전략")

if selected_strategy == _STRATEGY_MASTER_PB and not _MASTER_PB_AVAILABLE:
    st.error("master_pullback_top3_service 로드 실패. 기존 전략으로 fallback됩니다.")

# ── 작업 모드 선택 (시간분산 전략 전용) ──────────────────────────────────────
if _use_timed:
    st.divider()
    selected_work_mode = st.selectbox(
        "작업 모드",
        options=[_MODE_BUY_AND_SELL, _MODE_BUY_ONLY, _MODE_SELL_ONLY],
        index=0,
        help=(
            "매수+매도(기본): 예약된 시간에 매수 후 조건 충족 시 자동매도\n"
            "자동매수만: 예약 매수만 실행, 매도는 수동 처리\n"
            "자동매도만: 현재 보유종목을 불러와 3% 익절 / 1.5% 손절 자동매도"
        ),
    )
    st.session_state["timed_work_mode"] = selected_work_mode

    if selected_work_mode == _MODE_SELL_ONLY:
        st.info(
            f"📤 **자동매도만 모드** — 보유종목을 불러와 "
            f"익절 +{_SELL_ONLY_TAKE_PROFIT:.1f}% / 손절 {_SELL_ONLY_STOP_LOSS:.1f}% 기준으로 자동매도합니다.\n"
            "매수는 실행하지 않습니다.",
            icon="🔵",
        )
    elif selected_work_mode == _MODE_BUY_ONLY:
        st.info("📥 **자동매수만 모드** — 예약 시간에 매수만 실행합니다. 매도는 수동으로 처리하세요.", icon="🔴")
else:
    selected_work_mode = _MODE_BUY_AND_SELL

# ── 손절 ON/OFF (시간분산 전략 전용) ─────────────────────────────────────────
if _use_timed:
    _sl_toggle = st.toggle(
        f"손절 활성화 ({sl_pct:.1f}%)",
        value=sl_enabled,
        help="비활성화 시 손절 없이 강제청산(15:10)까지 보유",
    )
    st.session_state["timed_stop_loss_enabled"] = _sl_toggle

st.divider()

# ── 계좌 모드 / 예산 ──────────────────────────────────────────────────────────
col_mode, col_budget = st.columns([1, 2])
with col_mode:
    selected_mode = st.selectbox(
        "계좌 모드",
        options=["dry_run", "mock", "real"],
        index=["dry_run", "mock", "real"].index(cfg.mode) if cfg.mode in ["dry_run", "mock", "real"] else 0,
        help="dry_run: 가상 | mock: KIS 모의투자 | real: KIS 실전투자",
    )
with col_budget:
    total_budget = st.number_input(
        "총 예산 (원)", min_value=1_000_000, max_value=100_000_000,
        value=int(_ic.get("total_budget", 10_000_000)),
        step=1_000_000, format="%d",
    )

if selected_mode == "dry_run":
    st.info("드라이런 모드: 실제 주문 없이 가상 매수/매도가 실행됩니다.")
elif selected_mode == "mock":
    st.warning("모의투자 모드: KIS 모의투자 계좌에 주문됩니다. (실제 돈 아님)")
elif selected_mode == "real":
    st.error("실전투자 모드: 실제 KIS 계좌에 주문됩니다. 신중하게 확인하세요!")

_runtime_real_mode = False
_runtime_enable_real_buy = st.session_state.get("enable_real_buy", False)
_runtime_enable_real_sell = st.session_state.get("enable_real_sell", False)

if selected_mode == "real":
    _real_mode_enabled = st.session_state.get("real_mode_enabled", False)
    if _real_mode_enabled:
        st.error("실전모드 활성화 중 — 실제 계좌로 자동매매가 실행됩니다.", icon="🔴")
        _runtime_real_mode = True
    else:
        st.error("실전모드 미활성화 — 'API 연결' 페이지에서 실전모드 버튼을 먼저 활성화하세요.")

_confirm_text = ""
if selected_mode == "real":
    try:
        _expected_text = cfg.real_confirm_text()
    except Exception:
        _expected_text = "I_UNDERSTAND_REAL_TRADING_RISK"
    _confirm_text = st.text_input(
        f"실전투자 확인 문구 입력 ('{_expected_text}')",
        type="password", placeholder=_expected_text,
    )
    if _confirm_text and _confirm_text != _expected_text:
        st.error("확인 문구가 틀립니다. 자동매매 버튼이 비활성화됩니다.")

_real_confirm_ok = (
    selected_mode != "real"
    or (_confirm_text and _confirm_text == (
        cfg.real_confirm_text() if callable(getattr(cfg, "real_confirm_text", None))
        else "I_UNDERSTAND_REAL_TRADING_RISK"
    ))
)

if selected_mode in ("mock", "real"):
    with st.expander("브로커 연결 상태", expanded=False):
        import json as _json
        _cache_path = _SVC_ROOT / "data" / "cache" / f"kis_token_{selected_mode}.json"
        if _cache_path.exists():
            try:
                with open(_cache_path) as _f:
                    _cd = _json.load(_f)
                _exp = _cd.get("expires_at", "")
                st.caption(f"KIS {selected_mode.upper()} 토큰: 만료 {_exp[:19] if _exp else '알 수 없음'}")
            except Exception:
                st.caption("토큰 캐시 읽기 실패")
        else:
            st.caption(f"KIS {selected_mode.upper()} 토큰: 없음 (첫 실행 시 발급)")
        c1, c2, c3 = st.columns(3)
        c1.metric("선택 모드", selected_mode.upper())
        c2.metric("실전매수", "허용" if _runtime_enable_real_buy else "차단")
        c3.metric("실전매도", "허용" if _runtime_enable_real_sell else "차단")

# ── 예산 배분 표시 (시간분산 / 자동매수 모드 전용) ────────────────────────────
_timed_work_mode = st.session_state.get("timed_work_mode", _MODE_BUY_AND_SELL)
if _use_timed and _timed_work_mode != _MODE_SELL_ONLY:
    st.divider()
    st.subheader("예산 배분 계획")
    alloc_r = _ic.get("budget_allocation", {"rank1": 0.45, "rank2": 0.35, "rank3": 0.20})
    b1 = round(total_budget * float(alloc_r.get("rank1", 0.45)))
    b2 = round(total_budget * float(alloc_r.get("rank2", 0.35)))
    b3 = round(total_budget * float(alloc_r.get("rank3", 0.20)))
    leftover = total_budget - b1 - b2 - b3
    if leftover > 0:
        b1 += leftover
    ba1, ba2, ba3 = st.columns(3)
    ba1.metric("1위 예산", f"{b1:,}원", f"{float(alloc_r.get('rank1',0.45))*100:.0f}%")
    ba2.metric("2위 예산", f"{b2:,}원", f"{float(alloc_r.get('rank2',0.35))*100:.0f}%")
    ba3.metric("3위 예산", f"{b3:,}원", f"{float(alloc_r.get('rank3',0.20))*100:.0f}%")
    st.caption("잔여 예산은 1위 종목에 추가 배정됩니다.")

st.divider()

# ── 종목 불러오기 (모드별 분기) ───────────────────────────────────────────────
if _use_timed and _timed_work_mode == _MODE_SELL_ONLY:
    # ── 자동매도 전용: 보유종목 불러오기 ─────────────────────────────────────
    st.subheader("보유종목 불러오기 (자동매도 전용)")
    col_load_pos, col_clear_pos = st.columns(2)
    with col_load_pos:
        if st.button("📂 현재 보유종목 조회", use_container_width=True):
            try:
                _tmp_broker = _safe_create_broker(
                    mode=selected_mode,
                    confirm_text=_confirm_text,
                    runtime_real_mode=_runtime_real_mode,
                    runtime_enable_real_buy=_runtime_enable_real_buy,
                    runtime_enable_real_sell=_runtime_enable_real_sell,
                )
                _positions_raw = _tmp_broker.get_positions()
                if not _positions_raw:
                    st.warning("보유종목 없음 또는 조회 실패")
                    st.session_state["intraday_positions_to_sell"] = []
                else:
                    # dict/object 모두 list[dict]로 정규화
                    _pos_list = []
                    for _p in _positions_raw:
                        if isinstance(_p, dict):
                            _pos_list.append(_p)
                        else:
                            _pos_list.append({
                                "symbol": getattr(_p, "symbol", ""),
                                "name": getattr(_p, "name", ""),
                                "quantity": getattr(_p, "quantity", 0),
                                "avg_price": getattr(_p, "avg_price", 0),
                                "current_price": getattr(_p, "current_price", 0),
                            })
                    st.session_state["intraday_positions_to_sell"] = _pos_list
                    st.success(f"보유종목 {len(_pos_list)}개 조회 완료")
            except Exception as _e:
                st.error(f"보유종목 조회 실패: {_e}")
    with col_clear_pos:
        if st.button("🗑 초기화", use_container_width=True):
            st.session_state["intraday_positions_to_sell"] = []
            st.rerun()

    _positions_to_sell = st.session_state.get("intraday_positions_to_sell", [])
    if _positions_to_sell:
        _pos_rows = []
        for _p in _positions_to_sell:
            _avg = float(_p.get("avg_price", 0) or 0)
            _cur = float(_p.get("current_price", _avg) or _avg)
            _qty = int(_p.get("quantity", 0) or 0)
            _pnl = round((_cur - _avg) / _avg * 100, 2) if _avg > 0 else 0.0
            _pos_rows.append({
                "종목코드": _p.get("symbol", ""),
                "종목명": _p.get("name", ""),
                "수량": _qty,
                "평균단가": _format_price(_avg),
                "현재가": _format_price(_cur),
                "평가손익": _format_pct(_pnl),
                f"익절목표 (+{_SELL_ONLY_TAKE_PROFIT:.0f}%)": _format_price(round(_avg * (1 + _SELL_ONLY_TAKE_PROFIT / 100))),
                f"손절가 ({_SELL_ONLY_STOP_LOSS:.1f}%)": _format_price(round(_avg * (1 + _SELL_ONLY_STOP_LOSS / 100))),
            })
        st.dataframe(pd.DataFrame(_pos_rows), use_container_width=True, hide_index=True)
    else:
        st.info("보유종목을 먼저 조회하세요.")

else:
    # ── 자동매수 모드: Top3 불러오기 ──────────────────────────────────────────
    if st.button("📋 Top3 종목 불러오기", use_container_width=True):
        top3 = (st.session_state.get("sl_top3")
                or st.session_state.get("sector_leader_top3", []))
        if not top3:
            st.warning("'주도섹터 Top3' 탭에서 먼저 종목을 선정하세요.")
        else:
            if _use_timed:
                alloc_r2 = _ic.get("budget_allocation", {"rank1": 0.45, "rank2": 0.35, "rank3": 0.20})
                alloc_weights = [
                    float(alloc_r2.get("rank1", 0.45)),
                    float(alloc_r2.get("rank2", 0.35)),
                    float(alloc_r2.get("rank3", 0.20)),
                ]
                for i, stock in enumerate(top3[:3]):
                    stock["rank"] = i + 1
                    stock["allocated_budget"] = round(total_budget * alloc_weights[i])
                    stock["allocated_weight"] = alloc_weights[i]
            else:
                allocator = IntradayBudgetAllocator()
                top3 = allocator.allocate(top3, float(total_budget))
            st.session_state["intraday_allocated_top3"] = top3
            st.success(f"Top3 종목 {len(top3[:3])}개 로드 완료")

    if st.session_state.get("intraday_allocated_top3"):
        allocated_display = st.session_state["intraday_allocated_top3"]
        if _use_timed:
            sched_r = _ic.get("buy_schedule", {"rank1": "09:12", "rank2": "09:16", "rank3": "09:20"})
            sched_map = {1: sched_r.get("rank1"), 2: sched_r.get("rank2"), 3: sched_r.get("rank3")}
            rows = [{
                "순위": s.get("rank", ""),
                "매수예정시각": sched_map.get(s.get("rank"), "-"),
                "종목코드": s.get("symbol", ""),
                "종목명": s.get("name", ""),
                "현재가": _format_price(s.get("current_price", 0)),
                "배분예산": _format_price(s.get("allocated_budget", 0)),
                "배분비중": f"{s.get('allocated_weight', 0)*100:.1f}%",
            } for s in allocated_display[:3]]
        else:
            rows = [{
                "순위": s.get("rank", ""),
                "종목코드": s.get("symbol", ""),
                "종목명": s.get("name", ""),
                "현재가": _format_price(s.get("current_price", 0)),
                "배분비중": f"{s.get('allocated_weight', 0)*100:.1f}%",
                "배분예산": _format_price(s.get("allocated_budget", 0)),
            } for s in allocated_display]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        if _use_timed:
            st.info("Top3 종목을 불러오세요.")

st.divider()

# ── 자동매매 ON/OFF ───────────────────────────────────────────────────────────
st.subheader("자동매매 제어")
running = st.session_state.get("intraday_auto_trade_running", False)

# 모드별 시작 조건 체크
_can_start_sell_only = (
    _use_timed
    and _timed_work_mode == _MODE_SELL_ONLY
    and bool(st.session_state.get("intraday_positions_to_sell"))
)
_can_start_buy = (
    _timed_work_mode != _MODE_SELL_ONLY
    and bool(st.session_state.get("intraday_allocated_top3"))
)
_can_start = _can_start_sell_only or _can_start_buy

col_on, col_off, col_once, col_refresh = st.columns(4)
with col_on:
    _btn_label = "▶ 자동매매 ON" if _timed_work_mode != _MODE_SELL_ONLY else "▶ 자동매도 ON"
    _btn_disabled = running or (selected_mode == "real" and not _real_confirm_ok)
    if st.button(_btn_label, type="primary", use_container_width=True, disabled=_btn_disabled):
        if not _can_start:
            if _timed_work_mode == _MODE_SELL_ONLY:
                st.error("보유종목을 먼저 조회하세요.")
            else:
                st.error("먼저 Top3 종목을 불러오세요.")
        else:
            st.session_state["intraday_auto_trade_running"] = True
            st.session_state["intraday_selected_mode"] = selected_mode
            st.session_state["intraday_selected_strategy"] = selected_strategy
            st.session_state["intraday_selected_work_mode"] = _timed_work_mode
            st.rerun()
with col_off:
    if st.button("⏹ 자동매매 OFF", use_container_width=True, disabled=not running):
        st.session_state["intraday_auto_trade_running"] = False
        st.rerun()
with col_once:
    manual_run = st.button("🔄 1회 실행", use_container_width=True)
with col_refresh:
    if st.button("🔃 화면 갱신", use_container_width=True):
        st.rerun()

if running:
    _active_work_mode_disp = st.session_state.get("intraday_selected_work_mode", _MODE_BUY_AND_SELL)
    _mode_label = {
        _MODE_BUY_AND_SELL: "매수+매도",
        _MODE_BUY_ONLY: "자동매수만",
        _MODE_SELL_ONLY: "자동매도만",
    }.get(_active_work_mode_disp, _active_work_mode_disp)
    st.success(f"🟢 자동매매 실행 중 ({_mode_label}) — 10초마다 자동 갱신")
    if selected_mode == "real":
        st.warning("브라우저 세션이 꺼지면 자동매매가 중단됩니다.", icon="⚠️")
else:
    st.info("⚫ 자동매매 대기 중")

# ── run_once 실행 ─────────────────────────────────────────────────────────────
if running or manual_run:
    _active_mode = st.session_state.get("intraday_selected_mode", selected_mode)
    _active_strategy = st.session_state.get("intraday_selected_strategy", selected_strategy)
    _active_work_mode = st.session_state.get("intraday_selected_work_mode", _timed_work_mode)

    # 모드별 실행 가능 여부 검증
    _is_sell_only_mode = _active_strategy == _STRATEGY_TIMED and _active_work_mode == _MODE_SELL_ONLY
    allocated = st.session_state.get("intraday_allocated_top3", [])
    _positions_runtime = st.session_state.get("intraday_positions_to_sell", [])

    if _is_sell_only_mode and not _positions_runtime:
        st.warning("자동매도 전용 모드: 보유종목을 먼저 조회하세요.")
        st.session_state["intraday_auto_trade_running"] = False
    elif not _is_sell_only_mode and not allocated:
        st.warning("Top3 종목을 먼저 불러오세요.")
        st.session_state["intraday_auto_trade_running"] = False
    else:
        try:
            with st.spinner("실행 중..."):
                broker = _safe_create_broker(
                    mode=_active_mode,
                    confirm_text=_confirm_text,
                    runtime_real_mode=_runtime_real_mode,
                    runtime_enable_real_buy=_runtime_enable_real_buy,
                    runtime_enable_real_sell=_runtime_enable_real_sell,
                )
                kis_client = getattr(broker, "_kis", None) or getattr(broker, "kis_client", None)

                if _active_strategy == _STRATEGY_TIMED:
                    svc = Top3TimedBuyService(broker=broker, kis_client=kis_client, cfg=cfg)
                    svc.stop_loss_enabled = st.session_state.get("timed_stop_loss_enabled", True)

                    if _active_work_mode == _MODE_SELL_ONLY:
                        svc.enable_auto_buy = False
                        svc.enable_auto_sell = True
                        svc.take_profit_pct = _SELL_ONLY_TAKE_PROFIT
                        svc.stop_loss_pct = _SELL_ONLY_STOP_LOSS
                        svc.stop_loss_enabled = True
                        svc.load_from_positions(
                            _positions_runtime,
                            take_profit_pct=_SELL_ONLY_TAKE_PROFIT,
                            stop_loss_pct=_SELL_ONLY_STOP_LOSS,
                        )
                    elif _active_work_mode == _MODE_BUY_ONLY:
                        svc.enable_auto_buy = True
                        svc.enable_auto_sell = False
                        svc.load_top3(allocated[:3], float(total_budget))
                    else:  # BUY_AND_SELL (기본)
                        svc.enable_auto_buy = True
                        svc.enable_auto_sell = True
                        svc.load_top3(allocated[:3], float(total_budget))

                elif "Top3" in _active_strategy and _MASTER_PB_AVAILABLE:
                    svc = MasterPullbackTop3Service(broker=broker, kis_client=kis_client, cfg=cfg)
                    svc.total_budget = float(total_budget)
                    svc.use_morning_budget_ratio = st.session_state.get("master_pb_morning_budget_ratio", 1.0)
                    svc.allow_second_entry = st.session_state.get("master_pb_allow_second", True)
                    svc.load_top3(allocated)
                else:
                    svc = IntradayAutoTradeService(broker=broker, kis_client=kis_client, cfg=cfg)
                    svc.total_budget = float(total_budget)
                    svc.load_top3(allocated)

                result = svc.run_once()
            st.session_state["intraday_last_result"] = result
            st.session_state["intraday_last_at"] = datetime.now().strftime("%H:%M:%S")
        except Exception as ex:
            st.error(f"실행 오류: {ex}")
            st.session_state["intraday_auto_trade_running"] = False

st.divider()

# ===========================================================================
# 감시 종목 현황
# ===========================================================================
st.subheader("감시 종목 현황")

last_at = st.session_state.get("intraday_last_at", "")
if last_at:
    st.caption(f"마지막 실행: {last_at}")

if _use_timed:
    # ── Top3 시간분산 전략 상태 테이블 ──────────────────────────────────────
    timed_data = _load_timed_state()
    sym_states = timed_data.get("symbols", {})

    _disp_work_mode = st.session_state.get("intraday_selected_work_mode", _timed_work_mode)
    _is_sell_only_disp = _disp_work_mode == _MODE_SELL_ONLY

    if not sym_states:
        if _is_sell_only_disp:
            st.info("감시 중인 종목 없음 — 보유종목을 조회하고 자동매도를 시작하세요.")
        else:
            st.info("감시 중인 종목 없음 — Top3를 불러오고 자동매매를 시작하세요.")
    else:
        sched_r3 = _ic.get("buy_schedule", {"rank1": "09:12", "rank2": "09:16", "rank3": "09:20"})
        # 기본 익절/손절 (config), 종목별 override는 상태에서 읽음
        _cfg_tp = float(_ic.get("take_profit_pct", 3.0))
        _cfg_sl = float(_ic.get("stop_loss_pct", -1.2))
        monitor_rows = []
        for sym, s in sym_states.items():
            avg_p = float(s.get("avg_buy_price", 0) or 0)
            cur_p = float(s.get("current_price", 0) or 0)
            qty = int(s.get("buy_quantity", 0) or 0)
            profit_rate = float(s.get("profit_rate", 0) or 0)
            # 종목별 익절/손절 기준 (자동매도 모드에서는 per-symbol 값 사용)
            tp_pct_sym = float(s.get("take_profit_pct", _cfg_tp) or _cfg_tp)
            sl_pct_sym = float(s.get("stop_loss_pct", _cfg_sl) or _cfg_sl)
            tp_target = round(avg_p * (1 + tp_pct_sym / 100)) if avg_p > 0 else 0
            sl_price = round(avg_p * (1 + sl_pct_sym / 100)) if avg_p > 0 else 0
            rank = s.get("rank", "")
            sched_key = f"rank{rank}"
            scheduled_time = sched_r3.get(sched_key, "-") if not _is_sell_only_disp else "-"
            row = {
                "종목코드": sym,
                "종목명": s.get("name", sym),
            }
            if not _is_sell_only_disp:
                row["순위"] = rank
                row["배분예산"] = _format_price(s.get("allocated_budget", 0))
                row["매수예정"] = scheduled_time
            row.update({
                "매수완료": "✅" if s.get("bought_today") else "—",
                "매수시각": s.get("buy_time", "")[:16] or "-",
                "매수가": _format_price(avg_p) if avg_p > 0 else "-",
                "수량": qty if qty > 0 else "-",
                "현재가": _format_price(cur_p) if cur_p > 0 else "-",
                "수익률": _format_pct(profit_rate) if s.get("bought_today") else "-",
                f"익절목표가(+{tp_pct_sym:.1f}%)": _format_price(tp_target) if tp_target > 0 else "-",
                f"손절가({sl_pct_sym:.1f}%)": _format_price(sl_price) if sl_price > 0 else "-",
                "매도완료": "✅" if s.get("sold_today") else "—",
                "매도사유": s.get("sell_reason", "-") or "-",
                "상태": _STATUS_EMOJI.get(s.get("status", ""), s.get("status", "")),
                "최종확인": s.get("last_checked_at", "")[:16] or "-",
            })
            monitor_rows.append(row)

        if monitor_rows:
            def _timed_row_color(row):
                status_str = str(row.get("상태", ""))
                sold = str(row.get("매도완료", ""))
                if "✅" in sold:
                    return ["background-color:#e3f2fd"] * len(row)
                if "보유" in status_str or "HOLDING" in status_str:
                    return ["background-color:#e8f5e9"] * len(row)
                if "오류" in status_str or "실패" in status_str:
                    return ["background-color:#ffebee"] * len(row)
                return [""] * len(row)

            st.dataframe(
                pd.DataFrame(monitor_rows).style.apply(_timed_row_color, axis=1),
                use_container_width=True, hide_index=True,
            )

        bought_count = sum(1 for s in sym_states.values() if s.get("bought_today"))
        sold_count = sum(1 for s in sym_states.values() if s.get("sold_today"))
        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("보유/감시", f"{bought_count} / {len(sym_states)}종목")
        mc2.metric("매도 완료", f"{sold_count}종목")
        if _is_sell_only_disp:
            mc3.metric("익절기준", f"+{_SELL_ONLY_TAKE_PROFIT:.1f}%")
            mc4.metric("손절기준", f"{_SELL_ONLY_STOP_LOSS:.1f}%")
        else:
            mc3.metric("익절기준", f"+{_cfg_tp:.1f}%")
            mc4.metric("강제청산", force_exit_time)

elif _use_master_pb:
    # ── 고수 눌림목 Top3 상태 테이블 ─────────────────────────────────────────
    state_data = _load_legacy_state(_strategy_cfg_key)
    sym_states = state_data.get("symbols_state", state_data.get("symbols", {}))

    if not sym_states:
        st.info("감시 중인 종목 없음 — Top3를 불러오고 1회 이상 실행하세요.")
    else:
        monitor_rows = []
        for sym, s in sym_states.items():
            avg_p = float(s.get("avg_buy_price", 0) or 0)
            cur_p = float(s.get("current_price", 0) or 0)
            qty = int(s.get("position_quantity", 0) or 0)
            pnl_rate = ((cur_p - avg_p) / avg_p * 100) if avg_p > 0 and cur_p > 0 else None
            status = s.get("status", "")
            monitor_rows.append({
                "종목명": f"{s.get('name', sym)}({sym})",
                "상태": _STATUS_EMOJI.get(status, status),
                "진입횟수": f"{s.get('entries_count', 0)}회",
                "보유수량": qty if qty > 0 else "-",
                "평균단가": _format_price(avg_p) if avg_p > 0 else "-",
                "현재가": _format_price(cur_p) if cur_p > 0 else "-",
                "수익률": (_format_pct(pnl_rate) if pnl_rate is not None else "-"),
                "실현손익": _format_amt(s.get("realized_pnl", 0)),
                "배분예산": _format_price(s.get("allocated_budget", 0)),
                "마지막사유": s.get("last_reason", ""),
            })
        if monitor_rows:
            def _pb_row_color(row):
                status_str = str(row.get("상태", ""))
                if "보유" in status_str:
                    return ["background-color:#e8f5e9"] * len(row)
                if "절반" in status_str:
                    return ["background-color:#fff9c4"] * len(row)
                if "오류" in status_str:
                    return ["background-color:#ffebee"] * len(row)
                return [""] * len(row)
            st.dataframe(
                pd.DataFrame(monitor_rows).style.apply(_pb_row_color, axis=1),
                use_container_width=True, hide_index=True,
            )
        total_realized = sum(float(s.get("realized_pnl", 0) or 0) for s in sym_states.values())
        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("총 진입", f"{state_data.get('total_entries_today', 0)}회")
        mc2.metric("실현손익", _format_amt(total_realized))
        mc3.metric("감시 종목", len(sym_states))
        mc4.metric("오전 진입 완료", sum(1 for s in sym_states.values() if s.get("morning_entry_done")))

else:
    # ── 기존 전략 상태 테이블 ─────────────────────────────────────────────────
    state_data = _load_legacy_state(_strategy_cfg_key)
    sym_states = state_data.get("symbols", {})
    if not sym_states:
        st.info("감시 중인 종목 없음 — Top3를 불러오고 1회 이상 실행하세요.")
    else:
        monitor_rows = []
        for sym, s in sym_states.items():
            avg_p = float(s.get("avg_buy_price", 0) or 0)
            cur_p = float(s.get("current_price", 0) or 0)
            qty = int(s.get("position_quantity", 0) or 0)
            pnl_rate = ((cur_p - avg_p) / avg_p * 100) if avg_p > 0 and cur_p > 0 else None
            monitor_rows.append({
                "종목명": f"{s.get('name', sym)}({sym})",
                "상태": _STATUS_EMOJI.get(s.get("status", ""), s.get("status", "")),
                "진입횟수": f"{s.get('entries_count', 0)}회",
                "보유수량": qty if qty > 0 else "-",
                "평균단가": _format_price(avg_p) if avg_p > 0 else "-",
                "현재가": _format_price(cur_p) if cur_p > 0 else "-",
                "수익률": _format_pct(pnl_rate) if pnl_rate is not None else "-",
                "실현손익": _format_amt(s.get("realized_pnl", 0)),
                "배분예산": _format_price(s.get("allocated_budget", 0)),
            })
        if monitor_rows:
            st.dataframe(pd.DataFrame(monitor_rows), use_container_width=True, hide_index=True)
        total_entries = state_data.get("total_entries_today", 0)
        max_entries = _ic.get("max_total_entries_per_day", 3)
        mc1, mc2, mc3 = st.columns(3)
        mc1.metric("오늘 총 진입", f"{total_entries} / {max_entries}회")
        mc2.metric("실현손익", _format_amt(sum(float(s.get("realized_pnl", 0) or 0) for s in sym_states.values())))
        mc3.metric("감시 종목", len(sym_states))

st.divider()

# ===========================================================================
# 오늘 거래내역
# ===========================================================================
st.subheader(f"오늘 거래내역 ({_today})")

if _use_timed:
    trade_logs = _load_timed_log()
    col_map = {
        "timestamp": "시각", "action": "구분", "symbol": "종목코드", "name": "종목명",
        "rank": "순위", "quantity": "수량", "price": "가격", "order_amount": "주문금액",
        "profit_rate": "수익률", "reason": "사유", "status": "상태",
        "order_no": "주문번호", "error_message": "오류",
    }
else:
    trade_logs = _load_legacy_log(_strategy_cfg_key)
    col_map = {
        "timestamp": "시각", "action": "구분", "symbol": "종목코드", "name": "종목명",
        "quantity": "수량", "price": "가격", "reason": "사유", "sell_type": "매도유형",
        "order_success": "성공", "order_id": "주문번호", "error": "오류",
    }

if not trade_logs:
    st.info("오늘 거래내역 없음")
else:
    df_log = pd.DataFrame(trade_logs)
    df_log = df_log.rename(columns={k: v for k, v in col_map.items() if k in df_log.columns})
    if "구분" in df_log.columns:
        df_log["구분"] = df_log["구분"].map(
            {"buy": "🔴 매수", "sell": "🔵 매도", "force_exit": "🟠 강제청산",
             "force_close": "🟠 강제청산", "take_profit": "✅ 익절", "stop_loss": "🛑 손절"}
        ).fillna(df_log["구분"])

    def _log_color(row):
        act = str(row.get("구분", ""))
        if "매수" in act:
            return ["background-color:#fce4ec"] * len(row)
        if "매도" in act or "청산" in act or "익절" in act:
            return ["background-color:#e3f2fd"] * len(row)
        if "손절" in act:
            return ["background-color:#fff9c4"] * len(row)
        return [""] * len(row)

    st.dataframe(df_log.style.apply(_log_color, axis=1), use_container_width=True, hide_index=True)

    buys = [r for r in trade_logs if r.get("action") == "buy"]
    sells = [r for r in trade_logs if r.get("action") in ("sell", "force_close", "force_exit")]
    s1, s2, s3 = st.columns(3)
    s1.metric("매수", f"{len(buys)}건")
    s2.metric("매도", f"{len(sells)}건")
    s3.metric("전체 로그", f"{len(trade_logs)}건")

    csv_bytes = df_log.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button(
        "거래내역 CSV 다운로드", csv_bytes,
        file_name=f"intraday_trades_{_today}.csv", mime="text/csv",
    )

st.divider()
st.page_link("pages/6_주도섹터_Top3.py", label="← 주도섹터 Top3 선정으로 이동", icon="🎯")

# ── 자동 갱신 (st_autorefresh 또는 fallback) ─────────────────────────────────
if running:
    if _AUTOREFRESH_AVAILABLE:
        st_autorefresh(interval=10_000, key="intraday_autorefresh")
    else:
        import time as _time
        _time.sleep(10)
        st.rerun()
