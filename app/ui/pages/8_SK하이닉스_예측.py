"""
8_SK하이닉스_예측.py — SK하이닉스 완전 자동 예측 탭.

"자동 예측 실행" 버튼 하나로:
  MU·NVDA·SOX·QQQ·USD/KRW·SK하이닉스 일봉·코스피랩 참고가를 자동 수집하고
  오늘/내일/3일/2주 가격 예측 + 스윙 매매 플래그를 출력합니다.

실전 주문 기능과 절대 연결하지 않습니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pandas as pd
import streamlit as st

# ── 모듈 임포트 ───────────────────────────────────────────────────────────────

try:
    from app.data_sources.auto_market_collector import collect_all
    _collector_ok = True
except Exception as _ce:
    _collector_ok = False
    _collector_err = str(_ce)

try:
    from app.features.hynix_auto_features import build_auto_features
    _auto_feat_ok = True
except Exception:
    _auto_feat_ok = False

try:
    from app.models.hynix_predictor import predict_hynix
    _pred_ok = True
except Exception:
    _pred_ok = False

try:
    from app.models.hynix_swing_flag import evaluate_swing_flag, FLAG_COLORS, FLAG_LABELS
    _swing_ok = True
except Exception:
    _swing_ok = False

try:
    from app.models.hynix_swing_explainer import generate_swing_explanation
    _explain_ok = True
except Exception:
    _explain_ok = False

try:
    from app.ml.hynix_forecast_engine import run_forecast, collection_rate_label
    _engine_ok = True
except Exception:
    _engine_ok = False

try:
    from app.storage.prediction_logger import log_prediction, load_predictions
    _log_ok = True
except Exception:
    _log_ok = False

try:
    from app.storage.swing_flag_logger import log_swing_flag, load_swing_flags
    _swing_log_ok = True
except Exception:
    _swing_log_ok = False

try:
    from app.models.hynix_weight_adjuster import (
        load_weights, save_weights, adjust_weights_from_predictions,
        load_swing_weights, save_swing_weights, adjust_swing_weights_from_flags,
    )
    _adj_ok = True
except Exception:
    _adj_ok = False

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_MICRON_1MIN = _ROOT / "data" / "micron" / "MU_1min.csv"
_MICRON_3MIN = _ROOT / "data" / "micron" / "MU_3min.csv"

# ─────────────────────────────────────────────────────────────────────────────
# 페이지 설정
# ─────────────────────────────────────────────────────────────────────────────

st.title("SK하이닉스 예측")
st.caption("마이크론(MU) 프리마켓 + 자동 수집 데이터로 SK하이닉스(000660) 매매 신호를 생성합니다.")

st.info(
    "⚠️ **투자 참고용** — 예측값은 확률적 추정치이며 실제 매매 손익의 책임은 전적으로 사용자에게 있습니다.",
    icon="⚠️",
)

# ─────────────────────────────────────────────────────────────────────────────
# 사이드바
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.subheader("설정")
    api_mode = st.selectbox(
        "KIS API 모드",
        options=["real", "mock"],
        index=0,
        help="해외주식 분봉은 real 키를 권장합니다.",
    )
    st.caption("API 키는 .env 파일에서만 읽습니다.")

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# 상단 컨트롤: 자동 예측 실행 / 새로고침
# ─────────────────────────────────────────────────────────────────────────────

top_c1, top_c2, top_c3 = st.columns([2, 1, 2])

with top_c1:
    auto_run = st.button(
        "자동 예측 실행",
        type="primary",
        use_container_width=True,
        help="데이터 자동 수집 → feature 계산 → 예측 실행",
    )

with top_c2:
    refresh_data = st.button(
        "데이터 새로고침",
        use_container_width=True,
        help="캐시를 초기화하고 최신 데이터를 다시 수집합니다.",
    )

with top_c3:
    last_collected = st.session_state.get("collected_at", "—")
    st.markdown(f"**마지막 수집:** {last_collected}")

# ─────────────────────────────────────────────────────────────────────────────
# 데이터 새로고침
# ─────────────────────────────────────────────────────────────────────────────

if refresh_data:
    for key in ["market_data", "auto_features", "hynix_pred", "hynix_swing",
                "swing_explanation", "collected_at"]:
        st.session_state.pop(key, None)
    st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# 자동 예측 실행
# ─────────────────────────────────────────────────────────────────────────────

if auto_run:
    for key in [
        "engine_result", "auto_features", "hynix_pred", "hynix_swing",
        "swing_explanation", "prediction_result", "target_price",
        "stop_loss", "expected_open", "expected_high", "expected_low",
        "expected_close", "previous_signal",
    ]:
        st.session_state.pop(key, None)
    if not _collector_ok:
        st.error(f"자동 수집 모듈 로드 실패: {_collector_err}")
    elif not _engine_ok:
        st.error("예측 엔진 모듈 로드 실패 — app/ml/hynix_forecast_engine.py 확인")
    else:
        prog = st.progress(0, text="데이터 수집 시작...")

        with st.spinner("시장 데이터 자동 수집 중..."):
            market_data = collect_all(mode=api_mode)
            st.session_state["market_data"] = market_data
            st.session_state["collected_at"] = market_data.get("collected_at", "—")[:16]

        prog.progress(40, text="예측 파이프라인 실행 중...")

        engine_result = run_forecast(market_data)
        st.session_state["engine_result"] = engine_result

        auto_feat = engine_result.get("auto_features")
        pred      = engine_result.get("prediction")
        swing_res = engine_result.get("swing")
        expl      = engine_result.get("explanation")
        status    = engine_result.get("status", "blocked")
        dq        = engine_result.get("data_quality", 0.0)

        if auto_feat:
            st.session_state["auto_features"] = auto_feat
        if pred:
            st.session_state["hynix_pred"] = pred
        if swing_res:
            st.session_state["hynix_swing"] = swing_res
        if expl:
            st.session_state["swing_explanation"] = expl

        prog.progress(85, text="예측 로그 저장 중...")

        if pred is not None and _log_ok and auto_feat is not None:
            try:
                log_prediction(
                    prediction=pred,
                    micron_features=auto_feat["micron_features"],
                    micron_current_price=market_data.get("mu", {}).get("current_price"),
                    kospilab_inputs={
                        "kospilab_expected_return_pct": auto_feat["predictor_kwargs"].get("kospilab_expected_return_pct"),
                    },
                    other_inputs={
                        "sox_return_pct":     auto_feat["predictor_kwargs"].get("sox_return_pct"),
                        "nvda_return_pct":    auto_feat["predictor_kwargs"].get("nvda_return_pct"),
                        "qqq_return_pct":     auto_feat["predictor_kwargs"].get("qqq_return_pct"),
                        "usd_krw_change_pct": auto_feat["predictor_kwargs"].get("usd_krw_change_pct"),
                        "data_quality":       dq,
                    },
                )
            except Exception:
                pass

        if swing_res is not None and _swing_log_ok and auto_feat is not None:
            try:
                log_swing_flag(
                    swing_result=swing_res,
                    hynix_prev_close=auto_feat.get("hynix_prev_close"),
                    micron_features=auto_feat["micron_features"],
                    kospilab_return=auto_feat.get("kospilab_return"),
                    tech_indicators=auto_feat.get("tech_indicators", {}),
                )
            except Exception:
                pass

        prog.progress(100, text="완료!")

        # ── 수집률 게이트 결과 표시 ───────────────────────────────────────────
        rate_label, rate_color = collection_rate_label(dq)
        rate_pct = f"{dq * 100:.0f}%"

        if status == "blocked":
            st.error(f"🔴 **데이터 수집률 {rate_pct} ({rate_label})** — {engine_result.get('message', '')}")
        elif status == "low_confidence":
            st.warning(f"🟡 **데이터 수집률 {rate_pct} ({rate_label})** — {engine_result.get('message', '')}")
        else:
            st.success(f"🟢 **데이터 수집률 {rate_pct} ({rate_label})** — 예측 완료")

        # ── 데이터 진단 (상단 고정 섹션) ────────────────────────────────────
        st.subheader("데이터 진단")
        diag = engine_result.get("diagnostics", {})
        hynix_data = market_data.get("hynix", {})
        index_data = market_data.get("index", {})

        mu_diag_cols = st.columns(3)
        for col, label, key in zip(
            mu_diag_cols,
            ["MU current", "MU 1m", "MU 3m"],
            ["mu", "mu_1min", "mu_3min"],
        ):
            info = diag.get(key, {})
            source = info.get("source") or "failed"
            status_text = info.get("status") or ("success" if info.get("ok") else "failed")
            with col:
                st.caption(f"{label}: {source} {status_text}")

        required_diag_sources = [
            ("하이닉스 현재가", diag.get("hynix_current", {})),
            ("하이닉스 20일 일봉", diag.get("hynix_daily", {})),
            ("MU", diag.get("mu", {})),
            ("NVDA", diag.get("nvda", {})),
            ("QQQ", diag.get("qqq", {})),
            ("SOXX/SOX", diag.get("sox", {})),
            ("USD/KRW", diag.get("usdkrw", {})),
        ]
        source_cols = st.columns(4)
        for idx, (label, info) in enumerate(required_diag_sources):
            ok = bool(info.get("ok"))
            source = info.get("source") or info.get("status") or "failed"
            status_text = "성공" if ok else "실패"
            with source_cols[idx % 4]:
                st.caption(f"{label}: {source} {status_text}")

        _SOURCE_ICON = {
            "KIS": "🏦",
            "kis": "🏦",
            "naver": "📰",
            "naver_global": "📰",
            "yfinance": "📊",
            "cache": "💾",
        }

        def _src_label(src: str | None) -> str:
            if not src:
                return "—"
            icon = _SOURCE_ICON.get(src, "")
            label_map = {
                "KIS": "KIS 성공", "kis": "KIS 성공",
                "naver": "네이버 성공", "naver_global": "네이버 성공",
                "yfinance": "yfinance 성공",
                "cache": "캐시 사용",
            }
            return f"{icon} {label_map.get(src, src)}"

        _diag_sources = [
            ("MU 현재가",      diag.get("mu", {})),
            ("NVDA 현재가",    diag.get("nvda", {})),
            ("SOX 지수",       diag.get("sox", {})),
            ("QQQ ETF",        diag.get("qqq", {})),
            ("USD/KRW",        diag.get("usdkrw", {})),
            ("하이닉스 일봉",  diag.get("hynix", {})),
            ("코스피랩",       diag.get("kospilab", {})),
        ]
        diag_cols = st.columns(len(_diag_sources))
        for col, (name, src_info) in zip(diag_cols, _diag_sources):
            ok   = src_info.get("ok", False)
            icon = "✅" if ok else "❌"
            src  = src_info.get("source") or src_info.get("status") or "—"
            err  = src_info.get("error") or ""
            with col:
                st.markdown(f"**{icon} {name}**")
                st.caption(_src_label(src))
                if not ok and err:
                    st.caption(f"⚠ {str(err)[:50]}")

        # 하이닉스 일봉 fallback 경로 상세
        hynix_chain = hynix_data.get("fallback_chain", [])
        if hynix_chain:
            with st.expander("하이닉스 일봉 수집 경로"):
                for step in hynix_chain:
                    color = "green" if "성공" in step else "red"
                    st.markdown(f":{color}[{step}]")

        # 지수 개별 수집 상태
        idx_detail = index_data.get("fallback_detail", {})
        if idx_detail:
            with st.expander("지수/ETF 수집 상세"):
                for sym, status in idx_detail.items():
                    color = "green" if status == "성공" else "red"
                    st.markdown(f"- **{sym}**: :{color}[{status}]")

        # 수집 오류 상세
        all_errors = engine_result.get("errors", []) + market_data.get("errors", [])
        if all_errors:
            with st.expander(f"수집 오류 상세 ({len(all_errors)}건)"):
                for e in all_errors:
                    st.warning(e)

# ─────────────────────────────────────────────────────────────────────────────
# 수집 데이터 현황 표시
# ─────────────────────────────────────────────────────────────────────────────

market_data  = st.session_state.get("market_data", {})
auto_feat    = st.session_state.get("auto_features", {})
pred         = st.session_state.get("hynix_pred")
swing        = st.session_state.get("hynix_swing")
swing_expl   = st.session_state.get("swing_explanation", "")

if market_data:
    with st.expander("자동 수집 데이터 현황", expanded=False):
        mu_src  = market_data.get("mu", {}).get("source", "없음")
        mu_cp   = market_data.get("mu", {}).get("current_price")
        nv_src  = market_data.get("nvda", {}).get("source", "없음")
        nv_ret  = market_data.get("nvda", {}).get("regular_return")
        idx_src = market_data.get("index", {}).get("source", "없음")
        hy_src  = market_data.get("hynix", {}).get("source", "없음")
        kl_st   = market_data.get("kospilab", {}).get("source_status", "failed")
        kl_ret  = market_data.get("kospilab", {}).get("hynix_reference_return")
        kl_err  = market_data.get("kospilab", {}).get("error_message")
        qqq     = market_data.get("index", {}).get("qqq_return")
        sox     = market_data.get("index", {}).get("sox_return")
        usdkrw  = market_data.get("index", {}).get("usdkrw_change")

        dc1, dc2, dc3 = st.columns(3)
        with dc1:
            st.markdown(f"**MU** [{mu_src}]")
            if mu_cp:
                st.write(f"현재가: ${mu_cp.get('price', 0):.2f}")
            mf = auto_feat.get("micron_features", {})
            pm_ret = mf.get("micron_premarket_return")
            if pm_ret is not None:
                st.write(f"프리마켓: {pm_ret:+.2f}%")
            strength = mf.get("micron_session_strength_score")
            if strength is not None:
                st.write(f"강도: {strength:.0f}/100")
        with dc2:
            st.markdown(f"**NVDA** [{nv_src}]")
            if nv_ret is not None:
                st.write(f"등락률: {nv_ret:+.2f}%")
            st.markdown(f"**SOX/QQQ** [{idx_src}]")
            if sox is not None:
                st.write(f"SOXX: {sox:+.2f}%")
            if qqq is not None:
                st.write(f"QQQ: {qqq:+.2f}%")
            if usdkrw is not None:
                st.write(f"USD/KRW: {usdkrw:+.2f}%")
        with dc3:
            st.markdown(f"**코스피랩** [{kl_st}]")
            if kl_ret is not None:
                st.write(f"하이닉스 참고 등락률: {kl_ret:+.2f}%")
            elif kl_err:
                st.write(f"실패: {kl_err[:60]}...")
            st.markdown(f"**하이닉스 일봉** [{hy_src}]")
            prev_close = auto_feat.get("hynix_prev_close")
            if prev_close:
                st.write(f"전일 종가: {prev_close:,.0f}원")

        # 데이터 품질
        quality = auto_feat.get("data_quality", 0)
        st.metric("데이터 품질 점수", f"{quality*100:.0f}/100", help="0=데이터 없음, 100=모든 데이터 수집 성공")

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# 스윙 매매 플래그 — 핵심 결과 (최상단 표시)
# ─────────────────────────────────────────────────────────────────────────────

if swing is not None and _swing_ok:
    flag_val    = swing.get("swing_flag", "NEUTRAL")
    flag_label  = swing.get("flag_label", "관망")
    flag_color  = swing.get("flag_color", "#95a5a6")
    score       = swing.get("swing_score", 50.0)
    cf          = swing.get("confidence_score", 0.0)
    action_text = swing.get("action_text", "—")

    # 신뢰도 게이트 (confidence < 40 → 추천 차단)
    _engine_result = st.session_state.get("engine_result", {})
    _confidence_blocked = _engine_result.get("confidence_blocked", cf < 40.0)

    st.subheader("스윙 매매 플래그")

    if _confidence_blocked:
        st.warning(
            f"⚠️ **신뢰도 {cf:.0f}/100 — 데이터 부족/저신뢰**\n\n"
            "수집된 데이터가 매매 추천을 내리기에 충분하지 않습니다. "
            "시장 데이터를 재수집하거나 수동 입력을 활용하세요.",
        )
        # 신뢰도 차단 상태에서는 플래그 카드를 흐리게 표시 (참고용)
        st.markdown(
            f"""<div style="background:#7f8c8d;color:#fff;padding:14px 24px;
border-radius:12px;text-align:center;margin-bottom:12px;opacity:0.55;">
  <div style="font-size:0.9rem;opacity:0.80;margin-bottom:2px;">참고용 (신뢰도 부족)</div>
  <div style="font-size:1.8rem;font-weight:bold;margin:4px 0;">{flag_label}</div>
  <div style="font-size:0.80rem;opacity:0.70;">Swing Score {score:.0f}/100 · 신뢰도 {cf:.0f}/100</div>
</div>""",
            unsafe_allow_html=True,
        )
    else:
        # ── 핵심 카드: 플래그 + 액션 + 신뢰도 ───────────────────────────────────
        st.markdown(
            f"""<div style="background:{flag_color};color:#fff;padding:18px 28px;
border-radius:12px;text-align:center;margin-bottom:12px;">
  <div style="font-size:1.0rem;opacity:0.88;margin-bottom:2px;">현재 플래그 · Swing Score {score:.0f}/100</div>
  <div style="font-size:2.4rem;font-weight:bold;margin:6px 0;">{flag_label}</div>
  <div style="font-size:1.15rem;font-weight:600;margin:8px 0;opacity:0.97;">{action_text}</div>
  <div style="font-size:0.80rem;opacity:0.78;">신뢰도 {cf:.0f}/100 · {flag_val}</div>
</div>""",
            unsafe_allow_html=True,
        )

    # ── 구체적 매매 타이밍 (신뢰도 충분할 때만) ──────────────────────────────
    if not _confidence_blocked:
        buy_txt    = swing.get("buy_timing_text")
        sell_txt   = swing.get("sell_timing_text")
        bot_win    = swing.get("bottom_window_text")
        top_win    = swing.get("top_window_text")
        sell_ratio = swing.get("sell_ratio_text")

        timing_lines = []
        if buy_txt:
            timing_lines.append(f"📥 **매수 타이밍:** {buy_txt}")
        if sell_txt:
            timing_lines.append(f"📤 **매도 타이밍:** {sell_txt}")
        if bot_win:
            timing_lines.append(f"🔵 **저점 예상:** {bot_win}")
        if top_win:
            timing_lines.append(f"🔴 **고점 예상:** {top_win}")
        if timing_lines:
            st.markdown("  \n".join(timing_lines))

        # "분할매도"/"매도"는 전량매도가 아니다 — 권장 매도비중을 명확히 표시.
        if sell_ratio:
            st.info(
                f"ℹ️ **{flag_label}는 보유 물량 전체를 파는 것이 아닙니다.** "
                f"권장 매도 비중: **{sell_ratio}** — 나머지는 계속 보유하며 상황을 지켜보세요. "
                f"(전량 매도는 '강력매도' 플래그일 때만 해당됩니다)"
            )

    # ── 확률 & 신뢰도 ─────────────────────────────────────────────────────────
    sw1, sw2, sw3 = st.columns(3)
    with sw1:
        bp = swing.get("bottom_probability", 0.0)
        st.metric("단기 저점 확률", f"{bp:.1f}%")
    with sw2:
        tp = swing.get("top_probability", 0.0)
        st.metric("단기 고점 확률", f"{tp:.1f}%")
    with sw3:
        st.metric("신뢰도", f"{cf:.1f}/100")

    # ── 가격 구간 & 매매 가이드 ───────────────────────────────────────────────
    st.markdown("#### 매매 가이드")
    pg1, pg2, pg3, pg4 = st.columns(4)
    _pfmt = lambda v: f"{v:,.0f}원" if v else "—"
    buy_zone_note = swing.get("buy_zone_note")
    target_label = "목표가 (분할매도)" if flag_val in ("TAKE_PROFIT", "SELL", "STRONG_SELL") else "목표가 (익절)"
    with pg1:
        st.metric("매수 적정 구간",
                  _pfmt(swing.get("buy_zone_low")),
                  delta=f"~ {_pfmt(swing.get('buy_zone_high'))}")
        if buy_zone_note:
            st.caption(f"ℹ️ {buy_zone_note}")
    with pg2:
        st.metric(target_label, _pfmt(swing.get("target_price")))
    with pg3:
        st.metric("손절가", _pfmt(swing.get("stop_loss_price")))
    with pg4:
        hold_days = swing.get("expected_holding_days")
        st.metric("예상 보유기간", f"{hold_days}거래일" if hold_days else "—")

    # ── 판단 이유 (explainer) ────────────────────────────────────────────────
    if swing_expl:
        st.markdown(
            f'<div style="background:#f8f9fa;border-left:4px solid {flag_color};'
            f'padding:12px 16px;border-radius:4px;line-height:1.8;margin-top:8px;">'
            f'{swing_expl}</div>',
            unsafe_allow_html=True,
        )

    with st.expander("컴포넌트별 신호"):
        comp = swing.get("component_scores", {})
        comp_labels = {
            "micron_premarket": "마이크론 프리마켓",
            "kospilab":         "코스피랩",
            "tech_position":    "기술적 지표",
            "volume_momentum":  "거래량/수급",
            "semiconductor":    "반도체 지수",
            "currency_risk":    "환율 리스크",
        }
        comp_df = pd.DataFrame([
            {"지표": comp_labels.get(k, k),
             "신호": f"{v:+.4f}",
             "방향": "▲ 매수" if v > 0.05 else ("▼ 매도" if v < -0.05 else "중립")}
            for k, v in comp.items()
        ])
        st.dataframe(comp_df, use_container_width=True, hide_index=True)

    st.divider()

elif not (auto_run or market_data):
    st.info("'자동 예측 실행' 버튼을 누르면 스윙 플래그와 가격 예측이 자동으로 생성됩니다.")

# ─────────────────────────────────────────────────────────────────────────────
# 단기 전고점 예측 (신규)
# ─────────────────────────────────────────────────────────────────────────────

st.subheader("단기 전고점 예측")

if market_data:
    try:
        from app.models.hynix_short_term_signal import predict_hynix_signal
        short_term_signal = predict_hynix_signal(market_data)
    except Exception as _sts_exc:
        short_term_signal = None
        st.error(f"단기 전고점 예측 모듈 로드 실패: {_sts_exc}")

    if short_term_signal is not None:
        if short_term_signal.get("blocked"):
            missing_list = short_term_signal.get("missing_data", [])
            missing_text = ", ".join(missing_list) if missing_list else "알 수 없음"
            st.warning(
                "🔴 **필수 데이터 누락으로 단기 전고점 예측을 생성하지 않았습니다.**\n\n"
                f"누락된 데이터: {missing_text}"
            )
        else:
            score = short_term_signal["short_term_score"]
            direction = short_term_signal["direction"]
            st_c1, st_c2 = st.columns([1, 2])
            with st_c1:
                st.metric("단기 방향 점수", f"{score:.0f}/100", delta=direction)
            with st_c2:
                st.markdown(f"**매매 판단: {short_term_signal['judgement']}**")
                if short_term_signal.get("news_warning"):
                    st.caption(f"⚠️ {short_term_signal['news_warning']}")

            sup_c1, sup_c2, sup_c3 = st.columns(3)
            supports = short_term_signal["support_levels"]
            for col, label, val in zip([sup_c1, sup_c2, sup_c3], ["지지선 1", "지지선 2", "지지선 3"], supports):
                with col:
                    st.metric(label, f"{val:,.0f}원" if val else "—")

            tgt_c1, tgt_c2, tgt_c3 = st.columns(3)
            targets = short_term_signal["target_levels"]
            probs = short_term_signal["target_probabilities"]
            for col, label, val, prob_key in zip(
                [tgt_c1, tgt_c2, tgt_c3], ["목표가 1", "목표가 2", "목표가 3"], targets, ["target_1", "target_2", "target_3"]
            ):
                with col:
                    st.metric(label, f"{val:,.0f}원" if val else "—", delta=f"도달확률 {probs.get(prob_key, 0):.0f}%")

            with st.expander("판단 근거 Top 5"):
                for i, reason in enumerate(short_term_signal.get("reasons_top5", []), start=1):
                    st.markdown(f"{i}. {reason}")

            with st.expander("점수 세부 내역"):
                st.json(short_term_signal.get("score_breakdown", {}))

            st.caption(f"ℹ️ {short_term_signal['disclaimer']}")
else:
    st.info("'자동 예측 실행' 버튼을 누르면 단기 전고점 예측도 함께 생성됩니다.")

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# 가격 예측 결과
# ─────────────────────────────────────────────────────────────────────────────

if pred:
    st.subheader("가격 예측 결과")

    _fmt = lambda v: f"{v:,.0f}원" if v else "—"

    # 오늘 예상 OHLC
    st.markdown("#### 오늘 예상 흐름")
    st.caption(
        " | ".join([
            f"current_price={_fmt(pred.get('current_price'))}",
            f"base_source={pred.get('base_price_source') or 'unknown'}",
            f"prev_close={_fmt(pred.get('hynix_prev_close'))}",
            f"expected_return={pred.get('today_return_pct', 0):+.2f}%",
        ])
    )
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("예상 시가", _fmt(pred.get("today_open_expected")))
    with c2:
        st.metric("예상 고가", _fmt(pred.get("today_high_expected")))
    with c3:
        st.metric("예상 저가", _fmt(pred.get("today_low_expected")))
    with c4:
        ret = pred.get("today_return_pct", 0)
        st.metric("예상 종가", _fmt(pred.get("today_close_expected")), delta=f"{ret:+.2f}%")

    # 단기 예상
    st.markdown("#### 단기 예상")
    ca, cb = st.columns(2)
    with ca:
        t_ret = pred.get("tomorrow_return_pct", 0)
        st.metric("내일 예상 등락률", f"{t_ret:+.2f}%")
    with cb:
        d3_ret = pred.get("day3_return_pct", 0)
        st.metric("3일 후 예상 등락률", f"{d3_ret:+.2f}%")

    # 2주 예상
    st.markdown("#### 향후 2주 예상")
    cw1, cw2 = st.columns(2)
    with cw1:
        st.markdown(
            f"**향후 2주 최고점**\n"
            f"- 예상일: {pred.get('two_week_high_date', '—')}\n"
            f"- 예상가: {_fmt(pred.get('two_week_high_price'))}\n"
            f"- 확률: {pred.get('two_week_high_prob', 0)*100:.0f}%"
        )
    with cw2:
        st.markdown(
            f"**향후 2주 최저점**\n"
            f"- 예상일: {pred.get('two_week_low_date', '—')}\n"
            f"- 예상가: {_fmt(pred.get('two_week_low_price'))}\n"
            f"- 확률: {pred.get('two_week_low_prob', 0)*100:.0f}%"
        )

    # 확률 & 신뢰도
    cp1, cp2, cp3 = st.columns(3)
    with cp1:
        st.metric("상승 확률", f"{pred.get('up_probability', 50):.1f}%")
    with cp2:
        st.metric("하락 확률", f"{pred.get('down_probability', 50):.1f}%")
    with cp3:
        st.metric("신뢰도", f"{pred.get('confidence_score', 0):.1f}/100")

    # 차트
    st.markdown("#### 데이터 차트")
    tab1min, tab3min, tabhy = st.tabs(["MU 1분봉", "MU 3분봉", "하이닉스 20일 일봉"])

    with tab1min:
        df_1min = None
        if market_data.get("mu", {}).get("df_1min") is not None:
            df_1min = market_data["mu"]["df_1min"]
        if market_data.get("mu", {}).get("minute_1m_status") == "real candle success" and df_1min is not None and not df_1min.empty:
            if "datetime" in df_1min.columns:
                st.line_chart(df_1min.set_index("datetime")[["close"]])
            else:
                st.line_chart(df_1min[["close"]])
            with st.expander("1분봉 데이터"):
                st.dataframe(df_1min.tail(30), use_container_width=True)
        else:
            st.info("MU 1분봉 데이터 없음")

    with tab3min:
        df_3min = None
        if market_data.get("mu", {}).get("df_3min") is not None:
            df_3min = market_data["mu"]["df_3min"]
        if market_data.get("mu", {}).get("minute_3m_status") == "real candle success" and df_3min is not None and not df_3min.empty:
            idx_col = "datetime" if "datetime" in df_3min.columns else df_3min.columns[0]
            st.line_chart(df_3min.set_index(idx_col)[["close"]])
        else:
            st.info("MU 3분봉 데이터 없음")

    with tabhy:
        df_hy = market_data.get("hynix", {}).get("df_daily")
        if df_hy is not None and not df_hy.empty:
            recent = df_hy.tail(20)
            idx_col = "datetime" if "datetime" in recent.columns else recent.columns[0]
            if idx_col in recent.columns:
                st.line_chart(recent.set_index(idx_col)[["close"]])
            st.dataframe(recent, use_container_width=True)
        else:
            st.info("하이닉스 일봉 데이터 없음")

    with st.expander("예측 신호 상세"):
        st.json(pred.get("signals", {}))

    st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# 다중 시간대 가격 예측 (신규) — 30분/1시간/3시간/오늘종가/내일시가
# ─────────────────────────────────────────────────────────────────────────────

st.subheader("다중 시간대 가격 예측 (30분·1시간·3시간·오늘종가·내일시가)")

_price_pred = (st.session_state.get("engine_result", {}) or {}).get("price_prediction")

if _price_pred:
    if _price_pred.get("base_price") is None:
        st.warning(f"⚠️ {_price_pred.get('message', '가격 기준점이 없어 다중 horizon 예측을 생성하지 못했습니다.')}")
    else:
        _dq = _price_pred.get("data_quality_score", 0.0)
        _dq_icon = "🟢" if _dq >= 65 else ("🟡" if _dq >= 45 else "🔴")
        _base_price = _price_pred.get("base_price") or 0.0
        st.caption(
            f"{_dq_icon} 데이터 품질 {_dq:.0f}/100 · 모델 {_price_pred.get('model_version')} · "
            f"기준가 {_base_price:,.0f}원({_price_pred.get('base_price_source')}) · "
            + ("미국장 휴장/주말 모드" if _price_pred.get("holiday_mode") else "정상 거래일")
        )

        _horizon_specs = [
            ("30분 후", "predicted_price_30m", "confidence_30m", "expected_return_pct_30m"),
            ("1시간 후", "predicted_price_1h", "confidence_1h", "expected_return_pct_1h"),
            ("3시간 후", "predicted_price_3h", "confidence_3h", "expected_return_pct_3h"),
            ("오늘 종가", "predicted_close_today", "confidence_close", "expected_return_pct_close"),
            ("내일 시가", "predicted_open_tomorrow", "confidence_tomorrow_open", "expected_return_pct_tomorrow_open"),
        ]
        _hcols = st.columns(5)
        for _col, (_label, _pkey, _ckey, _rkey) in zip(_hcols, _horizon_specs):
            with _col:
                _pv = _price_pred.get(_pkey)
                _rv = _price_pred.get(_rkey)
                _cv = _price_pred.get(_ckey, 0.0) or 0.0
                st.metric(_label, f"{_pv:,.0f}원" if _pv else "—",
                          delta=f"{_rv:+.2f}%" if _rv is not None else None)
                st.caption(f"신뢰도 {_cv:.0f}/100")

        st.markdown("#### 내일 시가 방향 확률")
        _tp1, _tp2, _tp3 = st.columns(3)
        with _tp1:
            st.metric("상승", f"{_price_pred.get('probability_up_tomorrow', 0):.1f}%")
        with _tp2:
            st.metric("횡보", f"{_price_pred.get('probability_sideways_tomorrow', 0):.1f}%")
        with _tp3:
            st.metric("하락", f"{_price_pred.get('probability_down_tomorrow', 0):.1f}%")

        _warnings = _price_pred.get("missing_data_warning", [])
        if _warnings:
            with st.expander(f"⚠️ 데이터 품질 경고 ({len(_warnings)}건)"):
                for _w in _warnings:
                    st.warning(_w)

        _reasons = _price_pred.get("key_reasons", [])
        if _reasons:
            with st.expander("판단 근거 Top 5"):
                for _i, _r in enumerate(_reasons, start=1):
                    st.markdown(f"{_i}. {_r}")

        _dcol1, _dcol2 = st.columns(2)
        with _dcol1:
            with st.expander("사용된 데이터 소스"):
                st.json(_price_pred.get("data_sources_used", {}))
        with _dcol2:
            with st.expander("4단계 신호 점수 (-1=강한 하락 ~ +1=강한 상승)"):
                st.json(_price_pred.get("stage_scores", {}))

    st.caption("ℹ️ 위 예측은 확률적 추정치이며 수익을 보장하지 않습니다. 반드시 신뢰도/데이터 품질과 함께 참고하세요.")
elif market_data:
    st.info("다중 시간대 가격 예측 데이터를 생성하지 못했습니다 — '자동 예측 실행'을 다시 눌러보세요.")
else:
    st.info("'자동 예측 실행' 버튼을 누르면 30분/1시간/3시간/오늘종가/내일시가 예측이 함께 생성됩니다.")

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# 기술적 지표 표시
# ─────────────────────────────────────────────────────────────────────────────

tech = auto_feat.get("tech_indicators", {}) if auto_feat else {}
if any(v is not None for v in tech.values()):
    with st.expander("SK하이닉스 기술적 지표 (자동 계산)"):
        ti_c1, ti_c2 = st.columns(2)
        with ti_c1:
            rsi = tech.get("rsi_14")
            st.metric("RSI 14", f"{rsi:.1f}" if rsi is not None else "—",
                      delta="과매도" if rsi and rsi < 30 else ("과매수" if rsi and rsi > 70 else None))
            bb = tech.get("bollinger_pct")
            st.metric("볼린저밴드 %B", f"{bb:.1f}%" if bb is not None else "—")
            high_pct = tech.get("from_20d_high_pct")
            st.metric("20일 고점 대비", f"{high_pct:.1f}%" if high_pct is not None else "—")
        with ti_c2:
            macd_cross = tech.get("macd_signal_cross")
            cross_label = {1: "골든크로스 ▲", -1: "데드크로스 ▼", 0: "없음"}.get(macd_cross, "—")
            st.metric("MACD 크로스", cross_label)
            ma5 = tech.get("ma5_position_pct")
            st.metric("5일선 대비 현재가", f"{ma5:+.1f}%" if ma5 is not None else "—")
            vol_ch = tech.get("volume_change_pct")
            st.metric("거래량 변화율", f"{vol_ch:+.1f}%" if vol_ch is not None else "—")

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# 수동 보정 입력 (접힌 expander — 디버그/비상용)
# ─────────────────────────────────────────────────────────────────────────────

with st.expander("수동 보정 입력 — 필요할 때만 사용", expanded=False):
    st.caption(
        "자동 수집에 실패했거나, 값을 직접 보정하고 싶을 때만 사용하세요. "
        "'수동 예측 실행' 버튼을 누르면 이 값으로 예측이 재실행됩니다."
    )

    m_c1, m_c2 = st.columns(2)
    with m_c1:
        m_klab_price  = st.number_input("코스피랩 예상가 (원)",          min_value=0.0,   value=0.0,  step=1000.0)
        m_klab_ret    = st.number_input("코스피랩 예상 등락률 (%)",       min_value=-20.0, max_value=20.0, value=0.0, step=0.01)
        m_prev_close  = st.number_input("SK하이닉스 전일 종가 (원)",      min_value=0.0,   value=0.0,  step=500.0)
        m_prev_ret    = st.number_input("SK하이닉스 전일 등락률 (%)",     min_value=-30.0, max_value=30.0, value=0.0, step=0.01)
    with m_c2:
        m_sox  = st.number_input("SOX 등락률 (%)",    min_value=-15.0, max_value=15.0, value=0.0, step=0.01)
        m_nvda = st.number_input("NVDA 등락률 (%)",   min_value=-20.0, max_value=20.0, value=0.0, step=0.01)
        m_qqq  = st.number_input("QQQ 등락률 (%)",    min_value=-15.0, max_value=15.0, value=0.0, step=0.01)
        m_usd  = st.number_input("USD/KRW 변화율 (%)", min_value=-5.0,  max_value=5.0,  value=0.0, step=0.01)

    m_sub = st.expander("최근 수익률 / 기술적 지표 (선택)")
    with m_sub:
        ms_c1, ms_c2 = st.columns(2)
        with ms_c1:
            m_3d   = st.number_input("최근 3일 수익률 (%)",  value=0.0, step=0.01)
            m_5d   = st.number_input("최근 5일 수익률 (%)",  value=0.0, step=0.01)
            m_10d  = st.number_input("최근 10일 수익률 (%)", value=0.0, step=0.01)
            m_vol  = st.number_input("거래량 변화율 (%)",    value=0.0, step=0.1)
        with ms_c2:
            m_rsi      = st.number_input("RSI 14",                  min_value=0.0,   max_value=100.0, value=50.0, step=0.1)
            m_bb       = st.number_input("볼린저밴드 위치 (%)",      min_value=0.0,   max_value=150.0, value=50.0, step=1.0)
            m_from_hi  = st.number_input("20일 고점 대비 위치 (%)", min_value=-50.0, max_value=0.0,   value=0.0,  step=0.5)
            m_from_lo  = st.number_input("20일 저점 대비 위치 (%)", min_value=0.0,   max_value=100.0, value=0.0,  step=0.5)
        m_macd_cross = st.selectbox("MACD 크로스", [("없음", 0), ("골든크로스 ▲", 1), ("데드크로스 ▼", -1)], format_func=lambda x: x[0])
        m_candle     = st.selectbox("전일 캔들",   [("보통", 0), ("장대양봉 ▲", 1), ("장대음봉 ▼", -1)], format_func=lambda x: x[0])

    manual_run = st.button("수동 예측 실행", use_container_width=True)

    if manual_run and _pred_ok:
        # 수동 MU feature는 세션에 저장된 자동 feature 재사용
        mf = (auto_feat or {}).get("micron_features", {k: None for k in [
            "micron_premarket_return", "micron_premarket_open_to_now",
            "micron_premarket_high_to_now", "micron_premarket_low_to_now",
            "micron_premarket_30m_momentum", "micron_premarket_60m_momentum",
            "micron_premarket_vwap", "micron_premarket_volume_change",
            "micron_regular_return", "micron_aftermarket_return",
            "micron_session_strength_score",
        ]})
        _ti_m = {
            "rsi_14":            m_rsi       if m_rsi      != 50.0 else None,
            "bollinger_pct":     m_bb        if m_bb       != 50.0 else None,
            "macd_signal_cross": m_macd_cross[1],
            "prev_candle_type":  m_candle[1],
            "from_20d_high_pct": m_from_hi   if m_from_hi  != 0.0  else None,
            "from_20d_low_pct":  m_from_lo   if m_from_lo  != 0.0  else None,
            "ma5_position_pct":  None,
            "ma20_position_pct": None,
            "ma60_position_pct": None,
            "return_3d_pct":     m_3d        if m_3d       != 0.0  else None,
            "return_5d_pct":     m_5d        if m_5d       != 0.0  else None,
            "return_10d_pct":    m_10d       if m_10d      != 0.0  else None,
            "volume_change_pct": m_vol       if m_vol      != 0.0  else None,
            "macd": None,
        }
        try:
            pred_m = predict_hynix(
                micron_features=mf,
                kospilab_expected_price=m_klab_price if m_klab_price > 0 else None,
                kospilab_expected_return_pct=m_klab_ret if m_klab_ret != 0 else None,
                sox_return_pct=m_sox if m_sox != 0 else None,
                nvda_return_pct=m_nvda if m_nvda != 0 else None,
                qqq_return_pct=m_qqq if m_qqq != 0 else None,
                usd_krw_change_pct=m_usd if m_usd != 0 else None,
                hynix_prev_close=m_prev_close if m_prev_close > 0 else None,
                hynix_prev_return_pct=m_prev_ret if m_prev_ret != 0 else None,
                hynix_return_3d_pct=m_3d if m_3d != 0 else None,
                hynix_return_5d_pct=m_5d if m_5d != 0 else None,
                hynix_return_10d_pct=m_10d if m_10d != 0 else None,
                hynix_volume_change_pct=m_vol if m_vol != 0 else None,
            )
            st.session_state["hynix_pred"] = pred_m

            if _swing_ok:
                swing_m = evaluate_swing_flag(
                    micron_features=mf,
                    kospilab_expected_return_pct=m_klab_ret if m_klab_ret != 0 else None,
                    tech_indicators=_ti_m,
                    sox_return_pct=m_sox if m_sox != 0 else None,
                    nvda_return_pct=m_nvda if m_nvda != 0 else None,
                    qqq_return_pct=m_qqq if m_qqq != 0 else None,
                    usd_krw_change_pct=m_usd if m_usd != 0 else None,
                    hynix_prev_close=m_prev_close if m_prev_close > 0 else None,
                    prediction=pred_m,
                )
                st.session_state["hynix_swing"] = swing_m
                if _explain_ok:
                    expl_m = generate_swing_explanation(
                        swing_result=swing_m,
                        micron_features=mf,
                        tech_indicators=_ti_m,
                        kospilab_return=m_klab_ret if m_klab_ret != 0 else None,
                    )
                    st.session_state["swing_explanation"] = expl_m

            st.success("수동 예측 완료! 위 결과가 업데이트됩니다.")
            st.rerun()
        except Exception as me:
            st.error(f"수동 예측 실패: {me}")

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# 예측 이력 & 스윙 이력
# ─────────────────────────────────────────────────────────────────────────────

hist_tab, swing_hist_tab, weight_tab = st.tabs(["예측 이력", "스윙 이력", "모델 가중치"])

with hist_tab:
    if _log_ok:
        try:
            history = load_predictions()
            if history:
                df_hist = pd.DataFrame(history)
                completed = df_hist[df_hist["actual_close"].apply(lambda x: bool(x and str(x).strip()))]
                if not completed.empty:
                    try:
                        correct = sum(
                            (float(r["today_return_pct"]) >= 0) == (
                                float(r["actual_close"]) >= float(r.get("actual_open") or r["actual_close"])
                            )
                            for _, r in completed.iterrows()
                            if r.get("today_return_pct") and r.get("actual_close")
                        )
                        accuracy = correct / len(completed) * 100
                        st.metric("예측 방향 적중률", f"{accuracy:.1f}%", delta=f"{len(completed)}건")
                    except Exception:
                        pass
                cols = ["predicted_at", "today_return_pct", "confidence_score", "actual_close"]
                show = [c for c in cols if c in df_hist.columns]
                st.dataframe(df_hist[show].tail(20), use_container_width=True)
            else:
                st.info("예측 이력 없음")
        except Exception as e:
            st.warning(f"이력 로드 실패: {e}")
    else:
        st.warning("예측 로거 모듈 로드 실패")

with swing_hist_tab:
    if _swing_log_ok:
        try:
            swing_hist = load_swing_flags()
            if swing_hist:
                df_sw = pd.DataFrame(swing_hist)
                confirmed = df_sw[df_sw["flag_hit"].apply(lambda x: str(x) in ("0", "1"))]
                if not confirmed.empty:
                    hit_rate = confirmed["flag_hit"].apply(lambda x: int(str(x))).mean() * 100
                    st.metric("플래그 적중률", f"{hit_rate:.1f}%", delta=f"{len(confirmed)}건")
                show_cols = ["evaluated_at", "swing_flag", "flag_label", "swing_score",
                             "confidence_score", "actual_return_3d", "flag_hit"]
                avail = [c for c in show_cols if c in df_sw.columns]
                st.dataframe(df_sw[avail].tail(20), use_container_width=True)
            else:
                st.info("스윙 플래그 이력 없음")
        except Exception as e:
            st.warning(f"스윙 이력 로드 실패: {e}")
    else:
        st.warning("스윙 로거 모듈 로드 실패")

with weight_tab:
    if _adj_ok:
        wc1, wc2 = st.columns(2)
        with wc1:
            st.markdown("**가격 예측 모델 가중치**")
            weights = load_weights()
            wdf = pd.DataFrame([{"지표": k, "가중치": f"{v*100:.1f}%"} for k, v in weights.items()])
            st.dataframe(wdf, use_container_width=True, hide_index=True)
            if st.button("가중치 자동 조정", key="adj_pred"):
                if _log_ok:
                    try:
                        adj = adjust_weights_from_predictions(load_predictions())
                        save_weights(adj["new_weights"], reason=adj["reason"])
                        st.success(f"조정: {adj['reason']}")
                    except Exception as exc:
                        st.error(str(exc))

        with wc2:
            st.markdown("**스윙 모델 가중치**")
            sw_weights = load_swing_weights()
            sw_labels = {
                "micron_premarket": "마이크론 프리마켓",
                "kospilab":         "코스피랩",
                "tech_position":    "기술적 지표",
                "volume_momentum":  "거래량/수급",
                "semiconductor":    "반도체 지수",
                "currency_risk":    "환율 리스크",
            }
            sw_df = pd.DataFrame([
                {"지표": sw_labels.get(k, k), "가중치": f"{v*100:.1f}%"}
                for k, v in sw_weights.items()
            ])
            st.dataframe(sw_df, use_container_width=True, hide_index=True)
            if st.button("스윙 가중치 자동 조정", key="adj_swing"):
                if _swing_log_ok:
                    try:
                        sadj = adjust_swing_weights_from_flags(load_swing_flags())
                        save_swing_weights(sadj["new_weights"], reason=sadj["reason"])
                        st.success(f"조정: {sadj['reason']}")
                    except Exception as exc:
                        st.error(str(exc))
    else:
        st.warning("가중치 조정 모듈 로드 실패")

st.divider()
st.caption(
    "이 페이지는 예측/분석 전용입니다. "
    "실전 주문 기능과 연결되지 않습니다. "
    "투자 참고용이며 실제 매매 책임은 사용자에게 있습니다."
)
