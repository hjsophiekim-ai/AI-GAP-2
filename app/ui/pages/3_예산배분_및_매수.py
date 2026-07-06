"""
3_예산배분_및_매수.py

거래량급증 Top10 또는 주도섹터 Top3 종목을 불러와
예산을 배분하고 매수 / 장중 자동매매를 실행합니다.
"""
import sys
import time
import types
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st
import pandas as pd
from datetime import datetime

try:
    from zoneinfo import ZoneInfo as _ZI
    _KST = _ZI("Asia/Seoul")
except ImportError:
    import datetime as _dtmod
    _KST = _dtmod.timezone(_dtmod.timedelta(hours=9))

try:
    from app.trading.budget_allocator import BudgetAllocator
    from app.trading.broker_factory import create_broker
    from app.trading.order_manager import OrderManager, _is_etf_like
    from app.trading.kis_client import KISTokenError
    from app.config import get_config
    from app.utils.stock_utils import format_amount, format_price
    from app.services.intraday_budget_allocator import IntradayBudgetAllocator
    from app.services.intraday_auto_trade_service import IntradayAutoTradeService
except Exception as e:
    st.error(f"모듈 로드 오류: {e}")
    st.stop()


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _safe_create_broker(cfg, mode, confirm_text="", runtime_real_mode=False,
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


def _vs_to_candidate(d: dict, rank: int = None):
    return types.SimpleNamespace(
        rank=rank if rank is not None else int(d.get("rank", 0)),
        symbol=str(d.get("symbol", "")),
        name=str(d.get("name", "")),
        current_price=float(d.get("current_price", 0)),
        change_rate=float(d.get("change_rate", 0)),
        trade_value=float(d.get("trade_value", 0)),
        final_score=float(d.get("final_score", 0)),
        gap_rate=float(d.get("change_rate", 0)),
    )


def _top3_alloc_to_plan(alloc: dict, rank: int) -> types.SimpleNamespace:
    """IntradayBudgetAllocator 결과 → OrderManager 호환 BuyPlan 객체."""
    return types.SimpleNamespace(
        rank=rank,
        symbol=str(alloc.get("symbol", "")),
        name=str(alloc.get("name", "")),
        current_price=float(alloc.get("current_price", 0)),
        allocated_quantity=int(alloc.get("allocated_quantity", 0)),
        allocated_amount=float(alloc.get("allocated_budget", 0)),
        change_rate=float(alloc.get("change_rate", 0)),
        trade_value=float(alloc.get("trading_value", alloc.get("trade_value", 0))),
        final_score=float(alloc.get("final_score", 0)),
        gap_rate=float(alloc.get("change_rate", 0)),
        allocated_weight=float(alloc.get("allocated_weight", 0)),
    )


def _load_vs_csv_today() -> list:
    date_str = datetime.now(_KST).strftime("%Y%m%d")
    csv_path = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "data" / "volume_spike" / f"{date_str}_volume_spike_top10.csv"
    )
    if not csv_path.exists():
        return []
    try:
        df = pd.read_csv(csv_path, dtype={"symbol": str})
        return [
            types.SimpleNamespace(
                rank=int(row.get("rank", i + 1)),
                symbol=str(row.get("symbol", "")),
                name=str(row.get("name", "")),
                current_price=float(row.get("current_price", 0)),
                change_rate=float(row.get("change_rate", 0)),
                trade_value=float(row.get("trade_value", 0)),
                final_score=float(row.get("final_score", 0)),
                gap_rate=float(row.get("change_rate", 0)),
            )
            for i, row in df.iterrows()
        ]
    except Exception:
        return []


def _load_top3_csv_today() -> list[dict]:
    date_str = datetime.now(_KST).strftime("%Y%m%d")
    root = Path(__file__).resolve().parent.parent.parent.parent
    pattern = f"sector_leader_top3_{date_str}_*.csv"
    matches = sorted((root / "data" / "output").glob(pattern), reverse=True)
    if not matches:
        return []
    try:
        df = pd.read_csv(matches[0], dtype={"symbol": str})
        return df.to_dict("records")
    except Exception:
        return []


def _show_buy_results(results, log_path=None):
    success_count = sum(1 for r in results if r.success)
    fail_count = sum(1 for r in results if not r.success and not r.excluded_reason
                     and r.error_type not in ("excluded_etf", "duplicate", "validation_error", "batch_aborted"))
    etf_count = sum(1 for r in results if r.error_type == "excluded_etf")
    skip_count = sum(1 for r in results if r.error_type in ("duplicate", "validation_error", "batch_aborted"))
    token_error = any(r.error_type == "token_403" for r in results)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("매수 성공", f"{success_count}건")
    c2.metric("매수 실패", f"{fail_count}건")
    c3.metric("ETF 제외", f"{etf_count}건")
    c4.metric("스킵", f"{skip_count}건")

    if token_error:
        st.error("tokenP 403 오류 — KIS 앱키/시크릿 또는 토큰 발급 횟수를 확인하세요.")

    result_rows = []
    for r in results:
        if r.excluded_reason:
            label = "ETF제외"
        elif r.error_type == "batch_aborted":
            label = "중단"
        elif r.error_type in ("duplicate", "validation_error"):
            label = "스킵"
        elif r.success:
            label = "성공"
        else:
            label = "실패"
        result_rows.append({
            "종목코드": r.symbol, "종목명": r.name, "수량": r.quantity,
            "가격": format_price(r.price), "주문번호": r.order_id,
            "결과": label, "메시지": (r.message[:80] + "…") if (not r.success and len(r.message) > 80) else (r.message if not r.success else ""),
        })

    if result_rows:
        def _hl(row):
            c = {"성공": "#d4edda", "ETF제외": "#fff3cd", "스킵": "#fff3cd", "중단": "#fff3cd"}.get(str(row.get("결과")), "#f8d7da")
            return [f"background-color:{c}"] * len(row)
        st.dataframe(pd.DataFrame(result_rows).style.apply(_hl, axis=1),
                     use_container_width=True, hide_index=True)

    # 실패 상세 메시지 (전체 출력)
    failed = [r for r in results if not r.success and not r.excluded_reason
              and r.error_type not in ("excluded_etf", "duplicate", "validation_error", "batch_aborted")]
    if failed:
        with st.expander(f"실패 상세 ({len(failed)}건)", expanded=True):
            for r in failed:
                st.markdown(f"**{r.name} ({r.symbol})**")
                st.code(r.message, language=None)

    if log_path:
        try:
            with open(log_path, "rb") as f:
                st.download_button("주문 로그 CSV 다운로드", f.read(), Path(log_path).name, "text/csv",
                                   key=f"dl_log_{hash(log_path)}")
        except Exception:
            pass

    if success_count > 0:
        st.success(f"{success_count}개 매수 완료!")
    else:
        st.error("매수 주문이 모두 실패했습니다.")


# ===========================================================================
# Page
# ===========================================================================

st.title("예산 배분 및 매수")

# ---------------------------------------------------------------------------
# Section 1 — 모드 / 예산
# ---------------------------------------------------------------------------
st.subheader("계좌 모드 및 예산")

col_mode, col_budget, col_shares = st.columns(3)
with col_mode:
    selected_mode = st.selectbox("계좌 모드", ["dry_run", "mock", "real"],
                                  help="dry_run: 가상 | mock: KIS 모의투자 | real: KIS 실전투자")
with col_budget:
    total_budget = st.number_input("총 예산 (원)", min_value=100_000, max_value=100_000_000,
                                    value=10_000_000, step=100_000, format="%d")
with col_shares:
    max_shares = st.number_input("종목당 최대 수량 (Top10 전용)", min_value=1, max_value=10, value=2)

if selected_mode == "dry_run":
    st.info("드라이런 모드: 실제 주문 없이 가상 매수가 실행됩니다.")
elif selected_mode == "mock":
    st.warning("모의투자 모드: KIS 모의투자 계좌에 주문됩니다.")
elif selected_mode == "real":
    st.error("실전투자 모드: 실제 KIS 계좌에 주문됩니다. 신중하게 확인하세요!")

_runtime_real_mode = False
_runtime_enable_real_buy = st.session_state.get("enable_real_buy", False)
_runtime_enable_real_sell = st.session_state.get("enable_real_sell", False)

if selected_mode == "real":
    if st.session_state.get("real_mode_enabled", False):
        st.error("실전모드 활성화 중: 실제 계좌로 매수가 실행됩니다.", icon="🔴")
        _runtime_real_mode = True
    else:
        st.error("실전모드 미활성화 — 'API 연결' 페이지에서 실전모드 버튼을 먼저 활성화하세요.")

# real 모드: 실계좌 주문가능금액 및 안전한도 표시
if selected_mode == "real" and st.session_state.get("real_mode_enabled", False):
    _real_limits = get_config().get_real_order_limits()
    with st.expander("실계좌 안전한도 설정", expanded=True):
        lc1, lc2, lc3 = st.columns(3)
        with lc1:
            real_max_order = st.number_input(
                "1회 주문한도 (원)", min_value=100_000, max_value=50_000_000,
                value=int(_real_limits["per_order"]),
                step=500_000, format="%d", key="ui_real_max_order",
                help="REAL_MAX_ORDER_AMOUNT 환경변수로도 설정 가능"
            )
        with lc2:
            real_max_daily = st.number_input(
                "하루 주문한도 (원)", min_value=1_000_000, max_value=200_000_000,
                value=int(_real_limits["daily"]),
                step=1_000_000, format="%d", key="ui_real_max_daily",
                help="REAL_MAX_DAILY_ORDER_AMOUNT 환경변수로도 설정 가능"
            )
        with lc3:
            real_max_symbol = st.number_input(
                "종목당 보유한도 (원)", min_value=100_000, max_value=100_000_000,
                value=int(_real_limits["per_symbol"]),
                step=500_000, format="%d", key="ui_real_max_symbol",
                help="REAL_MAX_POSITION_AMOUNT_PER_SYMBOL 환경변수로도 설정 가능"
            )
        auto_reduce_on = st.checkbox(
            "한도 초과 시 수량 자동 조정", value=_real_limits.get("auto_reduce", True),
            key="ui_auto_reduce",
            help="활성화하면 한도 초과 시 주문 실패 대신 가능한 수량으로 자동 줄임"
        )
        st.session_state["real_order_limits"] = {
            "per_order": real_max_order,
            "daily": real_max_daily,
            "per_symbol": real_max_symbol,
            "auto_reduce": auto_reduce_on,
        }

        if st.button("실계좌 주문가능금액 조회", key="btn_fetch_orderable"):
            try:
                _b = _safe_create_broker(cfg=get_config(), mode="real",
                                          confirm_text="",
                                          runtime_real_mode=True,
                                          runtime_enable_real_buy=_runtime_enable_real_buy)
                _oc = _b.get_orderable_cash()
                st.session_state["real_orderable_cash"] = _oc
            except Exception as _ex:
                st.error(f"조회 실패: {_ex}")

        _real_oc = st.session_state.get("real_orderable_cash")
        if _real_oc is not None:
            if total_budget > _real_oc:
                st.warning(
                    f"설정 예산 {format_amount(total_budget)} > "
                    f"실계좌 주문가능금액 {format_amount(_real_oc)} — "
                    "예산을 주문가능금액 이내로 줄이거나 계좌에 자금을 추가하세요."
                )
            else:
                st.success(f"실계좌 주문가능금액: {format_amount(_real_oc)} (예산 내 사용 가능)")

confirm_text = ""
if selected_mode == "real":
    try:
        expected_text = get_config().real_confirm_text()
    except Exception:
        expected_text = "I_UNDERSTAND_REAL_TRADING_RISK"
    confirm_text = st.text_input(f"실전투자 확인 문구 입력 ('{expected_text}')",
                                  type="password", placeholder=expected_text)
    if confirm_text and confirm_text != expected_text:
        st.error("확인 문구가 틀립니다.")

real_confirm_ok = (
    selected_mode != "real"
    or (confirm_text and confirm_text == (
        get_config().real_confirm_text() if callable(getattr(get_config(), "real_confirm_text", None)) else "I_UNDERSTAND_REAL_TRADING_RISK"
    ))
)

st.divider()

# ---------------------------------------------------------------------------
# Section 2 — 종목 소스 선택 및 불러오기
# ---------------------------------------------------------------------------
st.subheader("종목 불러오기")

source_tab_vs, source_tab_top3 = st.tabs(["거래량급증 Top10", "주도섹터 Top3"])

# ── Tab 1: 거래량급증 Top10 ──────────────────────────────────────────────
with source_tab_vs:
    st.caption("'거래량급증 Top10 선정' 탭에서 선정한 종목을 불러옵니다.")
    if st.button("Top10 불러오기", use_container_width=True, key="btn_load_vs"):
        loaded = []
        vs_dicts = st.session_state.get("volume_spike_top10") or []
        if vs_dicts:
            loaded = [_vs_to_candidate(d) for d in vs_dicts]
            source_label = f"세션 (거래량급증 {len(loaded)}개)"
        else:
            loaded = _load_vs_csv_today()
            source_label = f"오늘 CSV ({len(loaded)}개)" if loaded else ""

        if loaded:
            st.session_state["top15"] = loaded
            st.session_state["stock_source"] = "top10"
            st.success(f"Top10 {len(loaded)}개 로드 완료")
        else:
            st.warning("거래량급증 종목이 없습니다. '거래량급증 Top10 선정' 탭에서 먼저 선정하세요.")

    if st.session_state.get("stock_source") == "top10" and st.session_state.get("top15"):
        top15 = st.session_state["top15"]
        rows = [{"순위": c.rank, "종목코드": c.symbol, "종목명": c.name,
                 "현재가": format_price(c.current_price), "상승률(%)": f"{c.change_rate:.2f}",
                 "최종점수": f"{c.final_score:.2f}",
                 "ETF": "⚠️" if _is_etf_like(c.symbol, c.name) else ""}
                for c in top15]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ── Tab 2: 주도섹터 Top3 ─────────────────────────────────────────────────
with source_tab_top3:
    st.caption("'주도섹터 Top3 선정' 탭에서 선정한 종목을 불러옵니다.")
    if st.button("Top3 불러오기", use_container_width=True, key="btn_load_top3"):
        top3_raw = (st.session_state.get("sl_top3")
                    or st.session_state.get("sector_leader_top3")
                    or [])
        if not top3_raw:
            top3_raw = _load_top3_csv_today()
        if top3_raw:
            st.session_state["top3_stocks"] = top3_raw
            st.session_state["stock_source"] = "top3"
            st.success(f"주도섹터 Top3 {len(top3_raw)}개 로드 완료")
        else:
            st.warning("주도섹터 Top3 종목이 없습니다. '주도섹터 Top3 선정' 탭에서 먼저 선정하세요.")

    if st.session_state.get("stock_source") == "top3" and st.session_state.get("top3_stocks"):
        top3_stocks = st.session_state["top3_stocks"]
        rows = []
        for s in top3_stocks:
            rows.append({
                "순위": s.get("rank", ""),
                "종목코드": s.get("symbol", ""),
                "종목명": s.get("name", ""),
                "섹터": s.get("sector", ""),
                "현재가": format_price(float(s.get("current_price", 0))),
                "상승률(%)": f"{float(s.get('change_rate', 0)):.2f}",
                "최종점수": f"{float(s.get('final_score', 0)):.1f}",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.divider()

# ---------------------------------------------------------------------------
# Section 3 — 예산 배분
# ---------------------------------------------------------------------------
st.subheader("예산 배분")

stock_source = st.session_state.get("stock_source", "")

col_alloc, _ = st.columns([1, 3])
with col_alloc:
    if st.button("예산 배분 계산", use_container_width=True, key="btn_allocate"):
        if stock_source == "top10":
            top15 = st.session_state.get("top15", [])
            if not top15:
                st.warning("먼저 Top10 종목을 불러오세요.")
            else:
                try:
                    allocator = BudgetAllocator()
                    buy_plan = allocator.allocate(top15, total_budget=float(total_budget),
                                                   max_shares=int(max_shares))
                    st.session_state["buy_plan"] = buy_plan
                    st.session_state["buy_plan_source"] = "top10"
                    st.success(f"Top10 배분 완료: {len(buy_plan)}개")
                except Exception as e:
                    st.error(f"예산 배분 오류: {e}")

        elif stock_source == "top3":
            top3_stocks = (st.session_state.get("top3_stocks")
                   or st.session_state.get("sl_top3")
                   or [])
            if not top3_stocks:
                st.warning("먼저 Top3 종목을 불러오세요.")
            else:
                try:
                    allocator = IntradayBudgetAllocator()
                    allocs = allocator.allocate(top3_stocks, float(total_budget))
                    buy_plan = [_top3_alloc_to_plan(a, i + 1) for i, a in enumerate(allocs)]
                    st.session_state["buy_plan"] = buy_plan
                    st.session_state["buy_plan_source"] = "top3"
                    st.session_state["top3_allocs"] = allocs
                    st.success(f"Top3 차등 배분 완료: {len(buy_plan)}개")
                except Exception as e:
                    st.error(f"예산 배분 오류: {e}")
        else:
            st.warning("먼저 종목을 불러오세요 (Top10 또는 Top3 탭).")

buy_plan = st.session_state.get("buy_plan", [])
buy_plan_source = st.session_state.get("buy_plan_source", "")

if buy_plan:
    rows = []
    for p in buy_plan:
        row = {
            "순위": p.rank, "종목코드": p.symbol, "종목명": p.name,
            "현재가": format_price(p.current_price),
            "배분수량": p.allocated_quantity,
            "배분금액": format_amount(p.allocated_amount),
            "ETF": "⚠️" if _is_etf_like(p.symbol, p.name) else "",
        }
        if buy_plan_source == "top3":
            row["비중(%)"] = f"{getattr(p, 'allocated_weight', 0)*100:.1f}%"
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    total_inv = sum(p.allocated_amount for p in buy_plan)
    remaining = float(total_budget) - total_inv
    etf_count = sum(1 for p in buy_plan if _is_etf_like(p.symbol, p.name))
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("배분 종목", f"{len(buy_plan)}개")
    m2.metric("총 투자금", format_amount(total_inv))
    m3.metric("잔여 예산", format_amount(remaining))
    m4.metric("ETF 제외 예정", f"{etf_count}개")

    if buy_plan_source == "top3":
        st.info("Top3 배분: 1위 약 50% / 2위 약 30% / 3위 약 20% (final_score 가중 보정)")

st.divider()

# ---------------------------------------------------------------------------
# Section 4 — 매수 실행 (수동/예약/선택)
# ---------------------------------------------------------------------------
st.subheader("매수 실행")

buy_type = st.radio("매수 유형", ["수동 매수", "9:20 일괄매수 (예약)", "종목 선택 매수"],
                    horizontal=True)

has_plan = bool(buy_plan)
_execute_buy = False
_selected_plan_for_buy = buy_plan

if buy_type == "수동 매수":
    if st.button("현재 리스트 전부 매수", disabled=not (has_plan and real_confirm_ok),
                 type="primary", use_container_width=True, key="btn_manual_buy_all"):
        _execute_buy = True
    if not has_plan:
        st.caption("먼저 예산 배분 계산을 실행하세요.")

elif buy_type == "9:20 일괄매수 (예약)":
    now_kst = datetime.now(_KST)
    is_920 = (now_kst.hour == 9 and 18 <= now_kst.minute <= 22)
    col_t, col_r = st.columns([4, 1])
    with col_t:
        st.markdown(f"**현재 시각 (KST)**: {now_kst.strftime('%H:%M:%S')}")
    with col_r:
        if st.button("시간 갱신", key="btn_refresh_time"):
            st.rerun()
    if is_920:
        st.success("9:20 매수 시간입니다!")
    else:
        st.info("9:18~9:22 사이에 버튼이 활성화됩니다.")
    if st.button("9:20 일괄매수 실행", disabled=not (has_plan and is_920 and real_confirm_ok),
                 type="primary", use_container_width=True):
        _execute_buy = True
    if not is_920:
        st.caption(f"현재 {now_kst.strftime('%H:%M')} — 9:18~9:22 사이에 '시간 갱신'을 누르세요.")

elif buy_type == "종목 선택 매수":
    if not buy_plan:
        st.warning("먼저 위에서 '예산 배분 계산'을 실행하세요.")
    else:
        option_labels = [
            f"#{p.rank}  {p.name} ({p.symbol})  |  {format_price(p.current_price)}  |  {format_amount(p.allocated_amount)} / {p.allocated_quantity}주"
            for p in buy_plan
        ]
        selected_labels = st.multiselect("매수할 종목 선택", option_labels, default=[],
                                          key="sel_buy_symbols")
        label_to_plan = dict(zip(option_labels, buy_plan))
        selected_plan = [label_to_plan[l] for l in selected_labels if l in label_to_plan]
        if selected_plan:
            c1, c2 = st.columns(2)
            c1.metric("선택 종목", f"{len(selected_plan)}개")
            c2.metric("예상 투자금", format_amount(sum(p.allocated_amount for p in selected_plan)))
            if st.checkbox("위 종목을 매수하겠습니다", key="chk_sel_buy"):
                if st.button("선택 종목 매수 실행", disabled=not real_confirm_ok,
                             type="primary", use_container_width=True, key="btn_sel_buy"):
                    _execute_buy = True
                    _selected_plan_for_buy = selected_plan
        else:
            st.info("위에서 매수할 종목을 선택하세요.")

# ── 공통 매수 실행 ─────────────────────────────────────────────────────────
if _execute_buy:
    try:
        cfg = get_config()
        with st.spinner("브로커 초기화 중..."):
            broker = _safe_create_broker(cfg=cfg, mode=selected_mode, confirm_text=confirm_text,
                                          runtime_real_mode=_runtime_real_mode,
                                          runtime_enable_real_buy=_runtime_enable_real_buy,
                                          runtime_enable_real_sell=_runtime_enable_real_sell)
        order_manager = OrderManager(broker=broker, cfg=cfg)
        with st.spinner(f"{len(_selected_plan_for_buy)}개 종목 매수 중..."):
            results = order_manager.execute_buy_plans(_selected_plan_for_buy)
        st.session_state["buy_results"] = results
        log_path = None
        try:
            log_path = order_manager.save_order_log(results)
        except Exception:
            pass
        _show_buy_results(results, log_path=log_path)
    except KISTokenError as e:
        st.error(f"토큰 오류 (403): {e}")
    except RuntimeError as e:
        st.error(f"안전장치 차단: {e}")
    except Exception as e:
        st.error(f"브로커 생성 실패: {e}")

if st.session_state.get("buy_results") and not _execute_buy:
    prev = st.session_state["buy_results"]
    st.info(f"이전 매수 결과: {sum(1 for r in prev if r.success)}/{len(prev)}건 성공")
    if st.button("→ 보유종목으로 이동"):
        st.switch_page("pages/4_보유종목_및_일괄매도.py")

st.divider()

# ---------------------------------------------------------------------------
# Section 5 — 장중 자동매매 (주도섹터 Top3 전용)
# ---------------------------------------------------------------------------
st.subheader("장중 자동매매 — 주도섹터 Top3 전용")

top3_stocks = st.session_state.get("top3_stocks", [])

if not top3_stocks:
    st.info("주도섹터 Top3 종목을 위 탭에서 먼저 불러온 후 사용하세요.")
else:
    st.caption(
        "1분봉/3분봉 기반 Buy/Sell Flag를 자동 판단하여 매수·매도를 실행합니다.  \n"
        "매수: VWAP 위 + EMA5>EMA20 + 눌림 -1.2~-3.8% + 양봉전환 + RSI 42~72  \n"
        "매도: 손절(-1.2%) / 전량익절(+3.2%) / 절반익절(+1.8%) / trailing stop / VWAP이탈"
    )

    # 안전장치 표시
    if selected_mode == "real" and not (_runtime_real_mode and _runtime_enable_real_buy):
        st.warning("실전모드 또는 실전매수가 비활성화 상태입니다. 모의모드로 동작합니다.")

    # ON/OFF 토글
    col_on, col_off, col_run = st.columns(3)
    with col_on:
        if st.button("자동매매 ON", type="primary", use_container_width=True, key="btn_auto_on"):
            st.session_state["intraday_auto_running"] = True
            st.session_state["intraday_service_loaded"] = False
            st.rerun()
    with col_off:
        if st.button("자동매매 OFF", use_container_width=True, key="btn_auto_off"):
            st.session_state["intraday_auto_running"] = False
            st.rerun()
    with col_run:
        manual_run = st.button("수동 run_once", use_container_width=True, key="btn_run_once")

    is_running = st.session_state.get("intraday_auto_running", False)
    if is_running:
        st.success("자동매매 실행 중 — 10초마다 자동 갱신합니다.")
    else:
        st.info("자동매매 OFF 상태")

    # 서비스 인스턴스 생성 / 재사용
    def _get_service():
        cfg = get_config()
        broker = _safe_create_broker(cfg=cfg, mode=selected_mode, confirm_text=confirm_text,
                                      runtime_real_mode=_runtime_real_mode,
                                      runtime_enable_real_buy=_runtime_enable_real_buy,
                                      runtime_enable_real_sell=_runtime_enable_real_sell)
        kis_client = getattr(broker, "_kis", None) or getattr(broker, "kis_client", None)
        svc = IntradayAutoTradeService(broker=broker, kis_client=kis_client, cfg=cfg)
        # 총 예산을 UI 입력값으로 덮어쓰기
        svc.total_budget = float(total_budget)
        svc.load_top3(top3_stocks)
        return svc

    # 자동 또는 수동 실행
    status_area = st.empty()
    result_area = st.empty()

    if is_running or manual_run:
        try:
            with st.spinner("자동매매 실행 중..."):
                svc = _get_service()
                summary = svc.run_once()
            st.session_state["intraday_last_summary"] = summary
        except Exception as e:
            st.error(f"자동매매 오류: {e}")
            st.session_state["intraday_auto_running"] = False

    # 상태 표시
    last_summary = st.session_state.get("intraday_last_summary")
    if last_summary:
        st.caption(f"마지막 실행: {last_summary.get('checked_at', '')}")

        # 종목별 상태 테이블
        try:
            svc_display = _get_service()
            state_rows = []
            status_emoji = {
                "WAITING_ENTRY": "⏳ 대기",
                "BUY_ORDER_PENDING": "📤 주문중",
                "HOLDING": "📈 보유",
                "HALF_SOLD": "📉 절반매도",
                "COOLING_DOWN": "❄️ 쿨다운",
                "DONE": "✅ 완료",
                "ERROR": "❌ 오류",
            }
            for sym, sym_state in svc_display.symbols_state.items():
                profit_rate = 0.0
                avg_p = float(sym_state.get("avg_buy_price", 0) or 0)
                cur_p = float(sym_state.get("current_price", 0) or 0)
                if avg_p > 0 and cur_p > 0:
                    profit_rate = (cur_p - avg_p) / avg_p * 100

                state_rows.append({
                    "종목": f"{sym_state.get('name', sym)} ({sym})",
                    "상태": status_emoji.get(sym_state.get("status", ""), sym_state.get("status", "")),
                    "진입횟수": f"{sym_state.get('entries_count', 0)}/{svc_display.max_entries_per_symbol}",
                    "보유수량": sym_state.get("position_quantity", 0),
                    "평균단가": format_price(avg_p) if avg_p > 0 else "-",
                    "현재가": format_price(cur_p) if cur_p > 0 else "-",
                    "수익률": f"{profit_rate:+.2f}%" if avg_p > 0 else "-",
                    "실현손익": format_amount(float(sym_state.get("realized_pnl", 0) or 0)),
                    "마지막사유": sym_state.get("last_reason", ""),
                })
            if state_rows:
                st.dataframe(pd.DataFrame(state_rows), use_container_width=True, hide_index=True)
        except Exception as ex:
            st.warning(f"상태 조회 오류: {ex}")

        # 액션 로그
        actions = last_summary.get("actions", [])
        if actions:
            with st.expander(f"이번 실행 액션 ({len(actions)}건)", expanded=True):
                for a in actions:
                    sym = a.get("symbol", "")
                    act = a.get("action", "")
                    reason = a.get("reason", a.get("sell_type", ""))
                    success = a.get("success", a.get("order_success", False))
                    icon = "✅" if success else "❌"
                    st.markdown(f"- {icon} **{sym}** `{act}` — {reason}")

    # 자동 루프 (ON 상태에서 10초 후 rerun)
    if is_running:
        time.sleep(10)
        st.rerun()
