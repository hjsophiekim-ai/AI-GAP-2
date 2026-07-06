"""
4_보유종목_및_일괄매도.py

보유종목 조회 후 다양한 매도 유형을 지원합니다.
- 수동 일괄매도
- 10:15 일괄매도 (예약)
- 조건 매도 (수익률 기준: 절반매도 / 전량매도 / 손절)
"""
import sys
from pathlib import Path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st
import pandas as pd
from datetime import datetime

try:
    from app.trading.order_manager import OrderManager
    from app.trading.kis_client import create_kis_client
    from app.trading.kis_mock_broker import KisMockBroker
    from app.trading.kis_real_broker import KisRealBroker
    from app.trading.dry_run_broker import DryRunBroker
    from app.config import get_config
    from app.models import Position
    from app.utils.stock_utils import format_amount, format_price, format_rate
except Exception as e:
    st.error(f"모듈 로드 오류: {e}")
    st.stop()

st.title("보유 종목 및 매도")


def _colour_rate(rate: float) -> str:
    text = format_rate(rate)
    colour = "#2ecc71" if rate >= 0 else "#e74c3c"
    return f'<span style="color:{colour};font-weight:bold">{text}</span>'


# ---------------------------------------------------------------------------
# Section 0 — 계좌 모드
# ---------------------------------------------------------------------------
st.subheader("계좌 모드")

selected_mode = st.selectbox(
    "계좌 모드",
    options=["mock", "real", "dry_run"],
    index=0,
    key="sell_page_mode",
    help="mock: KIS 모의투자 | real: KIS 실전투자 | dry_run: 가상(앱 내부)",
)

if selected_mode == "dry_run":
    st.info("드라이런 모드: 앱 내부 가상 포지션이 표시됩니다. (KIS 계좌 아님)")
elif selected_mode == "mock":
    st.info("모의투자 모드: KIS 모의투자 계좌의 실제 보유종목이 표시됩니다.")
elif selected_mode == "real":
    st.warning("실전투자 모드: 실제 KIS 계좌의 보유종목이 표시됩니다!")

# 실전모드 활성화 여부 확인
_runtime_real_mode = False
if selected_mode == "real":
    _real_mode_enabled = st.session_state.get("real_mode_enabled", False)
    if _real_mode_enabled:
        st.error(
            "실전모드 활성화 중: 실제 계좌로 매도가 실행됩니다.",
            icon="🔴",
        )
        _runtime_real_mode = True
    else:
        st.warning(
            "실전모드가 활성화되어 있지 않습니다.  \n"
            "보유종목 **조회**는 가능하지만, **매도 실행**은 'API 연결' 페이지에서 실전모드를 먼저 활성화하세요."
        )

confirm_text = ""
if selected_mode == "real":
    try:
        cfg_tmp = get_config()
        expected_text = cfg_tmp.real_confirm_text()
    except Exception:
        expected_text = "I_UNDERSTAND_REAL_TRADING_RISK"
    confirm_text = st.text_input(
        f"실전투자 확인 문구 ('{expected_text}') — 매도 실행 시 필요 (조회는 불필요)",
        type="password",
        placeholder=expected_text,
        key="sell_page_confirm",
    )
    if confirm_text and confirm_text != expected_text:
        st.error("확인 문구가 틀립니다. 모든 기능이 비활성화됩니다.")

if st.session_state.get("_sell_page_last_mode") != selected_mode:
    st.session_state.pop("sell_broker", None)
    st.session_state.pop("positions", None)
    st.session_state.pop("_sell_kis_client", None)
    st.session_state["_sell_page_last_mode"] = selected_mode

real_trade_ok = (
    selected_mode != "real"
    or (
        _runtime_real_mode
        and confirm_text
        and confirm_text == get_config().real_confirm_text()
    )
)

st.divider()

# ---------------------------------------------------------------------------
# Section 1 — 보유종목 조회
# ---------------------------------------------------------------------------
st.subheader("보유종목 조회")

col_fetch, col_refresh = st.columns([2, 5])
with col_fetch:
    fetch_clicked = st.button(
        "보유종목 조회",
        key="btn_fetch_positions",
        use_container_width=True,
        type="primary",
    )
with col_refresh:
    if st.button("새로고침 (현재가 업데이트)", key="btn_refresh_prices", use_container_width=True):
        positions_cached = st.session_state.get("positions", [])
        kis_cached = st.session_state.get("_sell_kis_client")
        broker_cached = st.session_state.get("sell_broker")
        if positions_cached and (kis_cached or broker_cached):
            with st.spinner("현재가 업데이트 중..."):
                for p in positions_cached:
                    try:
                        if kis_cached:
                            result = kis_cached.get_current_price(p.symbol)
                            if result and result.get("current_price"):
                                p.current_price = result["current_price"]
                        elif broker_cached:
                            price = broker_cached.get_current_price(p.symbol)
                            if price:
                                p.current_price = price
                    except Exception:
                        pass
            st.session_state["positions"] = positions_cached
            st.success("현재가 업데이트 완료")
        else:
            st.warning("먼저 보유종목을 조회하세요.")

if fetch_clicked:
    with st.spinner("보유종목을 조회하는 중..."):
        try:
            cfg = get_config()
            if selected_mode == "dry_run":
                # dry_run: 메모리 내 가상 포지션 조회
                budget = cfg.trading.get("total_budget", 10_000_000)
                broker = DryRunBroker(initial_balance=float(budget))
                st.session_state["sell_broker"] = broker
                positions = broker.get_positions()
                broker_type = "DryRunBroker"
            else:
                # mock/real: KIS API 직접 호출 (보유종목 조회는 읽기 전용 — 안전게이트 불필요)
                env_hints = {
                    "mock": "KIS_MOCK_APP_KEY, KIS_MOCK_APP_SECRET, KIS_MOCK_ACCOUNT_NO",
                    "real": "KIS_REAL_APP_KEY, KIS_REAL_APP_SECRET, KIS_ACCOUNT_NO",
                }
                _kis = create_kis_client(selected_mode)
                if _kis is None:
                    raise RuntimeError(
                        f"KIS {selected_mode} 클라이언트 초기화 실패. "
                        f".env 파일에 {env_hints.get(selected_mode, '인증정보')} 를 설정하세요."
                    )
                _bal = _kis.get_balance()
                if "error" in _bal:
                    raise RuntimeError(f"KIS 잔고 조회 실패: {_bal['error']}")
                positions = [
                    Position(
                        symbol=item["symbol"],
                        name=item["name"],
                        quantity=item["quantity"],
                        avg_price=item["avg_price"],
                        current_price=item["current_price"],
                    )
                    for item in (_bal.get("positions") or [])
                ]
                broker_type = "KisMockBroker" if selected_mode == "mock" else "KisRealBroker"
                # 현재가 새로고침용 KIS 클라이언트 저장 (현재가 업데이트 버튼에서 재사용)
                st.session_state["_sell_kis_client"] = _kis

            st.session_state["positions"] = positions
            st.session_state["broker_type"] = broker_type
            label = {"KisMockBroker": "KIS 모의투자", "KisRealBroker": "KIS 실전투자",
                     "DryRunBroker": "가상(드라이런)"}.get(broker_type, broker_type)
            st.success(f"조회 완료: {len(positions)}종목 ({label})")
        except RuntimeError as exc:
            st.error(f"오류: {exc}")
        except Exception as exc:
            st.error(f"보유종목 조회 실패: {exc}")

positions = st.session_state.get("positions", [])

if st.session_state.get("broker_type") and positions is not None:
    mode_label = {"KisMockBroker": "KIS 모의투자", "KisRealBroker": "KIS 실전투자",
                  "DryRunBroker": "가상(드라이런)"}.get(
        st.session_state["broker_type"], st.session_state["broker_type"]
    )
    st.caption(f"연결 계좌: {mode_label}")

if positions:
    total_market_value = sum(p.market_value for p in positions)
    total_cost = sum(p.cost for p in positions)
    total_pnl = sum(p.profit_amount for p in positions)
    pnl_rate = (total_pnl / total_cost * 100) if total_cost else 0.0

    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    col_m1.metric("보유 종목", f"{len(positions)}개")
    col_m2.metric("총 평가금액", format_amount(total_market_value))
    col_m3.metric("총 평가손익", format_amount(total_pnl), delta=format_rate(pnl_rate))
    col_m4.metric("수익률", format_rate(pnl_rate))

    rows_html = "".join(
        f"<tr>"
        f"<td>{p.symbol}</td><td>{p.name}</td>"
        f"<td style='text-align:right'>{p.quantity:,}주</td>"
        f"<td style='text-align:right'>{format_price(p.avg_price)}</td>"
        f"<td style='text-align:right'>{format_price(p.current_price)}</td>"
        f"<td style='text-align:right'>{_colour_rate(p.profit_rate)}</td>"
        f"<td style='text-align:right;color:{'#2ecc71' if p.profit_amount>=0 else '#e74c3c'}'>"
        f"{format_amount(p.profit_amount)}</td>"
        f"</tr>"
        for p in positions
    )
    st.markdown(
        f"<style>.pos-t{{width:100%;border-collapse:collapse;font-size:.9rem}}"
        f".pos-t th{{background:#1e2d3d;color:#fff;padding:8px 12px;text-align:left}}"
        f".pos-t td{{padding:6px 12px;border-bottom:1px solid #2d3f50}}"
        f".pos-t tr:hover td{{background:#1a2535}}</style>"
        f"<table class='pos-t'><thead><tr>"
        f"<th>종목코드</th><th>종목명</th><th>보유수량</th>"
        f"<th>평균단가</th><th>현재가</th><th>수익률(%)</th><th>평가손익(원)</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table>",
        unsafe_allow_html=True,
    )
elif st.session_state.get("positions") is not None:
    st.info("보유 중인 종목이 없습니다.")

st.divider()

# ---------------------------------------------------------------------------
# Section 2 — 매도 조건 설정
# ---------------------------------------------------------------------------
st.subheader("매도 조건 설정")

col_tp1, col_tp2, col_sl = st.columns(3)
with col_tp1:
    tp1_rate = st.number_input("절반 매도 수익률 (%)", min_value=0.5, max_value=20.0, value=3.0, step=0.5)
with col_tp2:
    tp2_rate = st.number_input("전량 매도 수익률 (%)", min_value=1.0, max_value=30.0, value=5.0, step=0.5)
with col_sl:
    sl_rate = st.number_input("손절(익절) 하락률 (%)", min_value=0.5, max_value=10.0, value=2.0, step=0.5,
                               help="매수 평균단가 대비 이 비율 하락 시 전량 매도")

# 매도 조건 체크 결과 미리보기
if positions:
    half_sell = [p for p in positions if tp1_rate <= p.profit_rate < tp2_rate]
    full_sell = [p for p in positions if p.profit_rate >= tp2_rate]
    stop_loss = [p for p in positions if p.profit_rate <= -sl_rate]

    with st.expander(f"+{tp1_rate:.0f}% 절반매도 대상 — {len(half_sell)}개", expanded=bool(half_sell)):
        if half_sell:
            for p in half_sell:
                st.write(f"- **{p.name}** ({p.symbol}) | {format_rate(p.profit_rate)} | {p.quantity}주 @ {format_price(p.current_price)}")
        else:
            st.write("해당 없음")

    with st.expander(f"+{tp2_rate:.0f}% 전량매도 대상 — {len(full_sell)}개", expanded=bool(full_sell)):
        if full_sell:
            for p in full_sell:
                st.write(f"- **{p.name}** ({p.symbol}) | {format_rate(p.profit_rate)} | {p.quantity}주 @ {format_price(p.current_price)}")
        else:
            st.write("해당 없음")

    with st.expander(f"-{sl_rate:.0f}% 하락 손절 대상 — {len(stop_loss)}개", expanded=bool(stop_loss)):
        if stop_loss:
            for p in stop_loss:
                st.write(f"- **{p.name}** ({p.symbol}) | {format_rate(p.profit_rate)} | {p.quantity}주 @ {format_price(p.current_price)}")
        else:
            st.write("해당 없음")

st.divider()

# ---------------------------------------------------------------------------
# Section 3 — 매도 유형 선택 및 실행
# ---------------------------------------------------------------------------
st.subheader("매도 실행")

sell_type = st.radio(
    "매도 유형",
    options=[
        "조건 매도 (수익률 기준)",
        "수동 일괄매도",
        "선택 매도",
        "10:15 일괄매도 (예약)",
        "11:50 일괄매도 (예약)",
    ],
    horizontal=True,
)

has_positions = bool(positions)


def _get_or_create_broker():
    """매도용 브로커 생성. mock / real / dry_run 완전 분리."""
    if selected_mode == "mock":
        # KisMockBroker: 안전게이트 없음, 세션 캐시 재사용
        cached = st.session_state.get("sell_broker")
        if cached is not None and isinstance(cached, KisMockBroker):
            return cached
        kis = st.session_state.get("_sell_kis_client") or create_kis_client("mock")
        if kis is None:
            raise RuntimeError(
                "KIS mock 클라이언트 초기화 실패. "
                ".env에 KIS_MOCK_APP_KEY, KIS_MOCK_APP_SECRET, KIS_MOCK_ACCOUNT_NO 를 설정하세요."
            )
        broker = KisMockBroker(kis)
        st.session_state["sell_broker"] = broker
        return broker

    elif selected_mode == "real":
        # KisRealBroker: 안전게이트 적용, runtime_real_mode 반영위해 항상 새로 생성
        kis = st.session_state.get("_sell_kis_client") or create_kis_client("real")
        if kis is None:
            raise RuntimeError(
                "KIS real 클라이언트 초기화 실패. "
                ".env에 KIS_REAL_APP_KEY, KIS_REAL_APP_SECRET, KIS_ACCOUNT_NO 를 설정하세요."
            )
        return KisRealBroker(
            kis,
            cfg=get_config(),
            confirm_text=confirm_text,
            runtime_real_mode=_runtime_real_mode,
        )

    else:  # dry_run
        cached = st.session_state.get("sell_broker")
        if cached is not None and isinstance(cached, DryRunBroker):
            return cached
        cfg_tmp = get_config()
        budget = cfg_tmp.trading.get("total_budget", 10_000_000)
        broker = DryRunBroker(initial_balance=float(budget))
        st.session_state["sell_broker"] = broker
        return broker


def _save_and_show_results(results, order_mgr):
    try:
        log_path = order_mgr.save_order_log(results)
        st.caption(f"주문 로그: {log_path}")
    except Exception:
        pass
    st.session_state["sell_results"] = results
    success_cnt = sum(1 for r in results if r.success)
    fail_cnt = len(results) - success_cnt
    st.success(f"매도 완료: 성공 {success_cnt}건 / 실패 {fail_cnt}건")
    total_proceeds = sum(r.quantity * r.price for r in results if r.success)
    st.metric("총 매도금액", format_amount(total_proceeds))


# ── 조건 매도 ──────────────────────────────────────────────────────────────
if sell_type == "조건 매도 (수익률 기준)":
    st.write(f"- **+{tp1_rate:.0f}%** 도달 종목: 절반 매도")
    st.write(f"- **+{tp2_rate:.0f}%** 도달 종목: 전량 매도")
    st.write(f"- **-{sl_rate:.0f}%** 하락 종목: 손절 전량 매도")

    if not has_positions:
        st.warning("먼저 보유종목을 조회하세요.")
    else:
        exit_plans: list[dict] = []
        pos_map = {p.symbol: p for p in positions}

        for p in positions:
            if p.profit_rate >= tp2_rate:
                exit_plans.append({"symbol": p.symbol, "name": p.name,
                                   "action": "sell_all", "quantity": p.quantity,
                                   "current_price": p.current_price,
                                   "reason": f"+{p.profit_rate:.1f}% 전량매도"})
            elif p.profit_rate >= tp1_rate:
                exit_plans.append({"symbol": p.symbol, "name": p.name,
                                   "action": "sell_half", "quantity": max(1, p.quantity // 2),
                                   "current_price": p.current_price,
                                   "reason": f"+{p.profit_rate:.1f}% 절반매도"})
            elif p.profit_rate <= -sl_rate:
                exit_plans.append({"symbol": p.symbol, "name": p.name,
                                   "action": "sell_all", "quantity": p.quantity,
                                   "current_price": p.current_price,
                                   "reason": f"{p.profit_rate:.1f}% 하락 손절"})

        if exit_plans:
            for plan in exit_plans:
                action_label = {"sell_all": "전량매도", "sell_half": "절반매도"}.get(plan["action"], plan["action"])
                st.write(f"- **{plan['name']}** ({plan['symbol']}) | {action_label} {plan['quantity']}주 | {plan['reason']}")

            confirm_cond = st.checkbox("위 매도 내역을 확인했습니다", key="chk_cond_sell")
            if st.button("조건 매도 실행", disabled=not (confirm_cond and real_trade_ok), type="primary"):
                try:
                    broker = _get_or_create_broker()
                    cfg = get_config()
                    order_mgr = OrderManager(broker, cfg=cfg)
                    cond_results = []
                    with st.spinner("조건 매도 중..."):
                        for plan in exit_plans:
                            pos = pos_map.get(plan["symbol"])
                            if pos is None:
                                continue
                            if plan["action"] == "sell_all":
                                r = order_mgr.execute_sell_all([pos])
                                cond_results.extend(r)
                            else:
                                r = order_mgr.execute_sell_partial(pos, plan["quantity"], plan["current_price"])
                                cond_results.append(r)
                    _save_and_show_results(cond_results, order_mgr)
                except RuntimeError as exc:
                    st.error(f"안전장치 차단: {exc}")
                except Exception as exc:
                    st.error(f"매도 실패: {exc}")
        else:
            st.success("현재 매도 조건을 충족하는 종목이 없습니다.")

# ── 수동 일괄매도 ──────────────────────────────────────────────────────────
elif sell_type == "수동 일괄매도":
    st.warning("현재 보유 중인 모든 종목을 즉시 매도합니다.")
    if not has_positions:
        st.warning("먼저 보유종목을 조회하세요.")
    else:
        confirm_bulk = st.checkbox("전체 매도를 확인했습니다", key="chk_bulk_sell")
        if st.button(
            "일괄매도 실행",
            disabled=not (confirm_bulk and real_trade_ok),
            type="primary",
            use_container_width=True,
        ):
            try:
                broker = _get_or_create_broker()
                cfg = get_config()
                order_mgr = OrderManager(broker, cfg=cfg)
                with st.spinner("전체 종목 매도 중..."):
                    bulk_results = order_mgr.execute_sell_all(positions)
                _save_and_show_results(bulk_results, order_mgr)
            except RuntimeError as exc:
                st.error(f"안전장치 차단: {exc}")
            except Exception as exc:
                st.error(f"매도 실패: {exc}")

# ── 선택 매도 ──────────────────────────────────────────────────────────────
elif sell_type == "선택 매도":
    if not has_positions:
        st.warning("먼저 보유종목을 조회하세요.")
    else:
        option_labels = [
            f"{p.name} ({p.symbol})  |  수익률: {format_rate(p.profit_rate)}  |  {p.quantity}주  |  현재가: {format_price(p.current_price)}"
            for p in positions
        ]
        selected_labels = st.multiselect(
            "매도할 종목 선택 (복수 선택 가능)",
            options=option_labels,
            default=[],
            placeholder="종목을 선택하세요",
            key="sel_sell_symbols",
        )

        label_to_pos = dict(zip(option_labels, positions))
        selected_positions_sell = [label_to_pos[l] for l in selected_labels if l in label_to_pos]

        if selected_positions_sell:
            total_market_val_sel = sum(p.market_value for p in selected_positions_sell)
            total_pnl_sel = sum(p.profit_amount for p in selected_positions_sell)
            c1, c2, c3 = st.columns(3)
            c1.metric("선택 종목", f"{len(selected_positions_sell)}개")
            c2.metric("예상 매도금액", format_amount(total_market_val_sel))
            c3.metric("예상 손익", format_amount(total_pnl_sel))

            confirm_sel_sell = st.checkbox("위 종목을 즉시 매도하겠습니다", key="chk_sel_sell")
            if st.button(
                "선택 종목 즉시 매도",
                disabled=not (confirm_sel_sell and real_trade_ok),
                type="primary",
                use_container_width=True,
                key="btn_sel_sell",
            ):
                try:
                    broker = _get_or_create_broker()
                    cfg = get_config()
                    order_mgr = OrderManager(broker, cfg=cfg)
                    with st.spinner(f"{len(selected_positions_sell)}개 종목 매도 중..."):
                        sel_sell_results = order_mgr.execute_sell_all(selected_positions_sell)
                    _save_and_show_results(sel_sell_results, order_mgr)
                except RuntimeError as exc:
                    st.error(f"안전장치 차단: {exc}")
                except Exception as exc:
                    st.error(f"매도 실패: {exc}")
        else:
            st.info("위에서 매도할 종목을 선택하세요.")

# ── 10:15 일괄매도 ─────────────────────────────────────────────────────────
elif sell_type == "10:15 일괄매도 (예약)":
    now = datetime.now()
    h, m = now.hour, now.minute
    is_1015 = (h == 10 and 13 <= m <= 17)

    st.markdown(f"**현재 시각**: {now.strftime('%H:%M:%S')}")

    if is_1015:
        st.success("10:15 매도 시간입니다! 아래 버튼을 눌러 일괄매도를 실행하세요.")
    else:
        st.info("10:15 일괄매도 예약 중 — 10:13~10:17 사이에 버튼이 활성화됩니다.")

    if not has_positions:
        st.warning("먼저 보유종목을 조회하세요.")
    else:
        confirm_sched = st.checkbox("10:15 일괄매도를 확인했습니다", key="chk_sched_sell")
        if st.button(
            "10:15 일괄매도 실행",
            disabled=not (is_1015 and confirm_sched and real_trade_ok),
            type="primary",
            use_container_width=True,
        ):
            try:
                broker = _get_or_create_broker()
                cfg = get_config()
                order_mgr = OrderManager(broker, cfg=cfg)
                with st.spinner("10:15 일괄매도 중..."):
                    sched_results = order_mgr.execute_sell_all(positions)
                _save_and_show_results(sched_results, order_mgr)
            except RuntimeError as exc:
                st.error(f"안전장치 차단: {exc}")
            except Exception as exc:
                st.error(f"매도 실패: {exc}")

        if not is_1015:
            st.caption(f"현재 {now.strftime('%H:%M')} — 10:13~10:17 사이에만 활성화됩니다.")

# ── 11:50 일괄매도 ─────────────────────────────────────────────────────────
elif sell_type == "11:50 일괄매도 (예약)":
    now = datetime.now()
    h, m = now.hour, now.minute
    is_1150 = (h == 11 and 48 <= m <= 52)

    st.markdown(f"**현재 시각**: {now.strftime('%H:%M:%S')}")
    st.info(
        "갭상승 당일 점심 직전 청산 전략입니다.  \n"
        "11:48~11:52 사이에 버튼이 활성화됩니다."
    )

    if is_1150:
        st.success("11:50 매도 시간입니다! 아래 버튼을 눌러 일괄매도를 실행하세요.")
    else:
        st.info("11:50 일괄매도 대기 중 — 11:48~11:52 사이에 버튼이 활성화됩니다.")

    if not has_positions:
        st.warning("먼저 보유종목을 조회하세요.")
    else:
        confirm_1150 = st.checkbox("11:50 일괄매도를 확인했습니다", key="chk_1150_sell")
        if st.button(
            "11:50 일괄매도 실행",
            disabled=not (is_1150 and confirm_1150 and real_trade_ok),
            type="primary",
            use_container_width=True,
        ):
            try:
                broker = _get_or_create_broker()
                cfg = get_config()
                order_mgr = OrderManager(broker, cfg=cfg)
                with st.spinner("11:50 일괄매도 중..."):
                    sell_1150_results = order_mgr.execute_sell_all(positions)
                _save_and_show_results(sell_1150_results, order_mgr)
            except RuntimeError as exc:
                st.error(f"안전장치 차단: {exc}")
            except Exception as exc:
                st.error(f"매도 실패: {exc}")

        if not is_1150:
            st.caption(f"현재 {now.strftime('%H:%M')} — 11:48~11:52 사이에만 활성화됩니다.")

# ---------------------------------------------------------------------------
# Section 4 — 매도 결과
# ---------------------------------------------------------------------------
if st.session_state.get("sell_results"):
    st.divider()
    st.subheader("매도 결과")
    sell_results = st.session_state["sell_results"]
    if not sell_results:
        st.info("매도 결과가 없습니다.")
    else:
        rows_html = "".join(
            f"<tr>"
            f"<td>{r.symbol}</td><td>{r.name}</td>"
            f"<td style='text-align:right'>{r.quantity:,}주</td>"
            f"<td style='text-align:right'>{format_price(r.price)}</td>"
            f"<td style='text-align:right'>{format_amount(r.quantity * r.price)}</td>"
            f"<td style='color:{'#2ecc71' if r.success else '#e74c3c'};font-weight:bold'>"
            f"{'성공' if r.success else '실패'}</td>"
            f"<td>{r.message}</td>"
            f"</tr>"
            for r in sell_results
        )
        st.markdown(
            f"<style>.res-t{{width:100%;border-collapse:collapse;font-size:.9rem}}"
            f".res-t th{{background:#1e2d3d;color:#fff;padding:8px 12px;text-align:left}}"
            f".res-t td{{padding:6px 12px;border-bottom:1px solid #2d3f50}}"
            f".res-t tr:hover td{{background:#1a2535}}</style>"
            f"<table class='res-t'><thead><tr>"
            f"<th>종목코드</th><th>종목명</th><th>수량</th>"
            f"<th>매도가</th><th>매도금액</th><th>결과</th><th>메시지</th>"
            f"</tr></thead><tbody>{rows_html}</tbody></table>",
            unsafe_allow_html=True,
        )
        success_results = [r for r in sell_results if r.success]
        total_proceeds = sum(r.quantity * r.price for r in success_results)
        s1, s2, s3 = st.columns(3)
        s1.metric("성공/실패", f"{len(success_results)}건/{len(sell_results)-len(success_results)}건")
        s2.metric("총 매도금액", format_amount(total_proceeds))
        s3.metric("매도 완료 시각", datetime.now().strftime("%H:%M:%S"))
