"""
6_주도섹터_Top3.py

주도섹터 Top3 집중매수 전략 UI.
전략:
  1. NXT 거래대금 상위 수집 (08:00 이후)
  2. 거래량 급증 수집 (09:00 이후, 보조 확인용)
  3. 미국장 강세 섹터 자동 분석
  4. 섹터 강도 계산 → 대장주 선정 → Top3 확정
"""

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import io
from datetime import datetime

import pandas as pd
import streamlit as st

# ── 모듈 임포트 ────────────────────────────────────────────────────────────
try:
    from app.data.naver_nxt_turnover_collector import collect_nxt_turnover_stocks
    from app.data.naver_volume_spike_collector import collect_volume_spike_stocks
    from app.strategy.sector_mapper import SectorMapper
    from app.strategy.sector_strength_analyzer import SectorStrengthAnalyzer
    from app.services.us_sector_strength_service import USSectorStrengthService
    from app.strategy.sector_leader_top3_selector import SectorLeaderTop3Selector
    from app.config import get_config
    _IMPORTS_OK = True
    _IMPORT_ERR = ""
except Exception as _ie:
    _IMPORTS_OK = False
    _IMPORT_ERR = str(_ie)

st.title("주도섹터 Top3 집중매수")
st.caption("NXT 거래대금 상위 + 거래량 급증 + 미국장 섹터 분석 기반 Top3 선정")

if not _IMPORTS_OK:
    st.error(f"모듈 로드 오류: {_IMPORT_ERR}")
    st.stop()

# ── 전략 설명 ──────────────────────────────────────────────────────────────
with st.expander("전략 설명 펼치기", expanded=False):
    st.markdown("""
**주도섹터 Top3 전략**

| 단계 | 내용 |
|------|------|
| ① NXT 거래대금 수집 | 08:00 이후 거래대금 상위 종목 수집 |
| ② 거래량 급증 수집 | 09:00 이후 보조 확인용 수집 |
| ③ 미국장 섹터 분석 | 전날 미국 섹터 ETF 강도 자동 분석 |
| ④ 섹터 강도 계산 | 오늘 국내 주도섹터 Top5 산출 |
| ⑤ 미국/한국 섹터 매칭 | 두 섹터가 겹치면 추가 가점 |
| ⑥ 대장주 선정 | 각 섹터 내 거래대금 1위 + 조건 충족 |
| ⑦ Top3 확정 | final_score 상위 3개 (동일 섹터 최대 2개) |

**하드 제외 (fallback에서도 복구 불가)**
- 현재가 20,000원 미만
- 거래대금 20억 미만
- 상승률 2% 미만 또는 15% 초과
- ETF / ETN / 우선주 / 스팩 / 리츠 / 거래정지
""")

st.divider()


# ── 포맷 헬퍼 ─────────────────────────────────────────────────────────────

def _fmt_tv(val: float) -> str:
    if val >= 1_000_000_000_000:
        return f"{val / 1_000_000_000_000:.1f}조"
    elif val >= 100_000_000:
        return f"{val / 100_000_000:.0f}억"
    else:
        return f"{val / 100_000_000:.1f}억"


def _regime_badge(regime: str) -> str:
    badges = {
        "risk_on": "🟢 Risk-ON",
        "neutral": "🟡 Neutral",
        "risk_off": "🔴 Risk-OFF",
    }
    return badges.get(regime, "⚪ 알 수 없음")


# ── 선정 버튼 ──────────────────────────────────────────────────────────────

if st.button("🎯 주도섹터 Top3 선정하기", type="primary", use_container_width=True):
    prog = st.progress(0, text="시작 중...")
    log_area = st.empty()
    msgs: list[str] = []

    def _log(msg: str) -> None:
        msgs.append(msg)
        log_area.markdown("\n".join(f"- {m}" for m in msgs))

    try:
        # STEP 1: NXT 거래대금 수집
        prog.progress(10, text="STEP 1: NXT 거래대금 상위 수집 중...")
        _log("STEP 1: NXT 거래대금 상위 수집 (sise_quant.naver)")
        try:
            nxt_stocks = collect_nxt_turnover_stocks(max_pages=5, max_stocks=100)
            _log(f"  수집 완료: {len(nxt_stocks)}개")
        except Exception as ex:
            _log(f"  수집 실패: {ex}")
            nxt_stocks = []

        # STEP 2: 거래량 급증 수집 (보조)
        prog.progress(25, text="STEP 2: 거래량 급증 보조 수집 중...")
        _log("STEP 2: 거래량 급증 보조 수집 (sise_quant_high.naver)")
        try:
            vs_stocks = collect_volume_spike_stocks(max_pages=3, max_stocks=80)
            _log(f"  수집 완료: {len(vs_stocks)}개")
        except Exception as ex:
            _log(f"  수집 실패: {ex}")
            vs_stocks = []

        # STEP 3: 미국장 섹터 분석
        prog.progress(40, text="STEP 3: 미국장 강세 섹터 분석 중...")
        _log("STEP 3: 미국장 강세 섹터 자동 분석")
        try:
            us_svc = USSectorStrengthService()
            us_result = us_svc.get_us_sector_strength()
            _log(f"  데이터 소스: {us_result.get('data_source_used', '?')}")
            _log(f"  시장 레짐: {us_result.get('market_regime', '?')}")
            _log(f"  미국 강세 섹터: {', '.join(us_result.get('strong_sectors', [])[:3])}")
        except Exception as ex:
            _log(f"  미국장 분석 실패 (0점 처리): {ex}")
            us_result = {"market_regime": "neutral", "data_source_used": "none",
                         "strong_sectors": [], "moderate_sectors": [], "sector_scores": {}}

        # STEP 4: 섹터 분류 + 섹터 강도 계산
        prog.progress(55, text="STEP 4: 섹터 분류 및 강도 계산 중...")
        _log("STEP 4: 섹터 분류 및 강도 계산")

        if not nxt_stocks:
            st.warning("NXT 거래대금 수집 실패 (0개). 네트워크 연결을 확인하거나 장 시작 후 재시도하세요.")
            prog.empty()
            log_area.empty()
            st.stop()

        mapper = SectorMapper()
        nxt_classified = mapper.classify_stocks(nxt_stocks)
        vs_symbols = {s["symbol"] for s in vs_stocks if s.get("symbol")}

        analyzer = SectorStrengthAnalyzer()
        sector_analysis = analyzer.analyze(nxt_classified, volume_spike_symbols=vs_symbols, us_sector_results=us_result)
        top_sectors = analyzer.get_top_sectors(n=5)
        _log(f"  섹터 분류 완료: {len(nxt_classified)}개 종목")
        _log(f"  섹터 수: {len(sector_analysis)}개")
        _log(f"  국내 강세 섹터 Top3: {', '.join(s['sector'] for s in top_sectors[:3])}")

        # STEP 5: Top3 선정
        prog.progress(75, text="STEP 5: Top3 선정 중...")
        _log("STEP 5: Top3 선정")
        selector = SectorLeaderTop3Selector()
        top3, diag, excluded = selector.select(nxt_classified, vs_stocks, us_result)
        _log(f"  후보 평가: {diag.get('candidates_evaluated', 0)}개")
        _log(f"  하드 제외: {diag.get('hard_excluded', 0)}개")
        _log(f"  최종 Top3: {len(top3)}개")

        # STEP 6: CSV 저장
        prog.progress(88, text="STEP 6: CSV 저장 중...")
        _log("STEP 6: CSV 저장")
        date_str = datetime.now().strftime("%Y%m%d")
        time_str = datetime.now().strftime("%H%M")
        try:
            csv_top3 = selector.save_top3_csv(top3, date_str=date_str, time_str=time_str)
            csv_sector = selector.save_sector_strength_csv(sector_analysis, date_str=date_str, time_str=time_str)
            csv_excl = selector.save_excluded_csv(excluded, date_str=date_str, time_str=time_str)
            _log(f"  Top3 저장: {csv_top3}")
            _log(f"  섹터강도 저장: {csv_sector}")
            _log(f"  제외종목 저장: {csv_excl}")
        except Exception as ex:
            _log(f"  CSV 저장 실패: {ex}")

        # 세션 저장
        st.session_state["sl_top3"] = top3
        st.session_state["sl_diag"] = diag
        st.session_state["sl_excluded"] = excluded
        st.session_state["sl_sector_analysis"] = sector_analysis
        st.session_state["sl_top_sectors"] = top_sectors
        st.session_state["sl_us_result"] = us_result
        st.session_state["sl_nxt_count"] = len(nxt_classified)
        st.session_state["sl_vs_count"] = len(vs_stocks)
        st.session_state["sl_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        prog.progress(100, text="완료!")
        if top3:
            st.success(f"주도섹터 Top{len(top3)}개 선정 완료!")
        else:
            st.warning("선정 기준을 충족하는 종목이 없습니다. 장 개시 후(09:00 이후) 재시도하세요.")

    except Exception as ex:
        st.error(f"Top3 선정 오류: {ex}")
        prog.empty()


# ── 결과 표시 ──────────────────────────────────────────────────────────────

if st.session_state.get("sl_top3") is not None:
    top3 = st.session_state["sl_top3"]
    diag = st.session_state.get("sl_diag", {})
    excluded = st.session_state.get("sl_excluded", [])
    sector_analysis = st.session_state.get("sl_sector_analysis", {})
    top_sectors = st.session_state.get("sl_top_sectors", [])
    us_result = st.session_state.get("sl_us_result", {})
    at = st.session_state.get("sl_at", "")
    nxt_count = st.session_state.get("sl_nxt_count", 0)
    vs_count = st.session_state.get("sl_vs_count", 0)

    if at:
        st.caption(f"선정 시각: {at}")

    # ── 요약 지표 ──────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("NXT 거래대금 수집", f"{nxt_count}개")
    c2.metric("거래량 급증 수집", f"{vs_count}개")
    c3.metric("최종 Top3", f"{len(top3)}개")
    c4.metric("제외 종목", f"{len(excluded)}개")

    st.divider()

    # ── 미국장 분석 결과 ───────────────────────────────────────────────────
    st.subheader("어제 미국장 강세 섹터")
    regime = us_result.get("market_regime", "neutral")
    src = us_result.get("data_source_used", "none")
    st.markdown(f"**시장 레짐:** {_regime_badge(regime)}  |  **데이터 소스:** {src}")

    if regime == "risk_off":
        st.warning("⚠️ 미국장 Risk-OFF 감지: us_sector_match_score 50% 축소 적용됨")

    us_col1, us_col2 = st.columns(2)
    with us_col1:
        st.markdown("**Strong 섹터** (score ≥ 70)")
        strong = us_result.get("strong_sectors", [])
        if strong:
            for s in strong[:5]:
                score = us_result.get("sector_scores", {}).get(s, 0)
                st.markdown(f"- {s} ({score:.0f}점)")
        else:
            st.caption("없음 (미국 데이터 없거나 장외)")

    with us_col2:
        st.markdown("**Moderate 섹터** (score 50-70)")
        moderate = us_result.get("moderate_sectors", [])
        if moderate:
            for s in moderate[:5]:
                score = us_result.get("sector_scores", {}).get(s, 0)
                st.markdown(f"- {s} ({score:.0f}점)")
        else:
            st.caption("없음")

    spy_ch = us_result.get("spy_change", None)
    qqq_ch = us_result.get("qqq_change", None)
    if spy_ch is not None:
        st.caption(f"SPY: {spy_ch:+.2f}%  |  QQQ: {qqq_ch:+.2f}%")

    st.divider()

    # ── 국내 강세 섹터 Top5 ───────────────────────────────────────────────
    st.subheader("오늘 국내 강세 섹터 Top5")
    if top_sectors:
        sector_rows = []
        for rank_i, sec in enumerate(top_sectors[:5], 1):
            sec_key = sec.get("sector", "")
            leader = sec.get("leader")
            sector_rows.append({
                "순위": rank_i,
                "섹터": sec_key,
                "거래대금 합계": _fmt_tv(sec.get("sector_total_trading_value", 0)),
                "평균상승률(%)": round(sec.get("sector_avg_change_rate", 0), 2),
                "종목수": sec.get("sector_stock_count", 0),
                "급증중복": sec.get("volume_spike_overlap_count", 0),
                "미국매칭": "✅" if sec.get("us_sector_match") else "",
                "강도점수": round(sec.get("sector_strength_score", 0), 1),
                "대장주": leader.get("name", "") if leader else "없음",
            })
        st.dataframe(pd.DataFrame(sector_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("섹터 분석 결과 없음")

    st.divider()

    # ── Top3 결과 테이블 ───────────────────────────────────────────────────
    st.subheader(f"최종 선정 Top{len(top3)}")
    if top3:
        top3_rows = []
        for s in top3:
            top3_rows.append({
                "순위": s.get("rank", ""),
                "종목코드": s.get("symbol", ""),
                "종목명": s.get("name", ""),
                "섹터": s.get("sector", ""),
                "서브테마": s.get("subtheme", ""),
                "현재가": f"{int(s.get('current_price', 0)):,}",
                "상승률(%)": round(s.get("change_rate", 0), 2),
                "거래대금": _fmt_tv(s.get("trading_value", 0)),
                "섹터강도": round(s.get("sector_strength_score", 0), 1),
                "대장주점수": round(s.get("sector_leader_score", 0), 1),
                "미국매칭": round(s.get("us_sector_match_score", 0), 1),
                "급증확인": round(s.get("volume_spike_confirm_score", 0), 1),
                "MA보너스": round(s.get("ma_bonus", 0), 1),
                "리스크감점": round(s.get("risk_penalty", 0), 1),
                "최종점수": round(s.get("final_score", 0), 1),
                "선정이유": s.get("selected_reason", ""),
                "미국매칭이유": s.get("us_sector_reason", ""),
            })

        st.dataframe(
            pd.DataFrame(top3_rows),
            use_container_width=True,
            hide_index=True,
        )

        # CSV 다운로드
        buf = io.StringIO()
        pd.DataFrame(top3_rows).to_csv(buf, index=False, encoding="utf-8-sig")
        st.download_button(
            label="Top3 CSV 다운로드",
            data=buf.getvalue().encode("utf-8-sig"),
            file_name=f"{datetime.now().strftime('%Y%m%d_%H%M')}_sector_leader_top3.csv",
            mime="text/csv",
            key="dl_top3",
        )

        st.page_link(
            "pages/3_예산배분_및_매수.py",
            label="→ 예산배분 및 매수로 이동",
            icon="💰",
        )

    else:
        st.warning("선정된 종목이 없습니다.")

    st.divider()

    # ── 제외 종목 ──────────────────────────────────────────────────────────
    with st.expander(f"제외 종목 ({len(excluded)}개)", expanded=False):
        if excluded:
            excl_rows = []
            for s in excluded[:50]:
                excl_rows.append({
                    "종목코드": s.get("symbol", ""),
                    "종목명": s.get("name", ""),
                    "현재가": f"{int(s.get('current_price', 0)):,}",
                    "상승률(%)": round(s.get("change_rate", 0), 2),
                    "거래대금": _fmt_tv(s.get("trading_value", s.get("trade_value", 0))),
                    "제외사유": s.get("excluded_reason", ""),
                })
            st.dataframe(pd.DataFrame(excl_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("제외 종목 없음")

    st.divider()

    # ── 진단 정보 (Debug) ──────────────────────────────────────────────────
    with st.expander("진단 정보 (Debug)", expanded=False):
        st.subheader("선정 파이프라인 통계")
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("NXT 수집", diag.get("total_nxt", 0))
        d2.metric("하드 제외", diag.get("hard_excluded", 0))
        d3.metric("평가 대상 (eligible)", diag.get("candidates_evaluated", diag.get("after_hard_filter", 0)))
        d4.metric("섹터 수", diag.get("sectors_found", 0))

        d5, d6 = st.columns(2)
        d5.metric("Fallback 사용", "✅" if diag.get("fallback_used") else "❌")
        d6.metric("최종 Top3", diag.get("top3_count", 0))

        st.subheader("미국 ETF 데이터 진단")
        us_status = us_result.get("us_sector_data_status", "unknown")
        etf_ok_count = us_result.get("successful_etf_count", 0)
        failed_etfs = us_result.get("failed_etfs", [])

        status_color = {"ok": "🟢", "partial_failed": "🔴", "no_data": "⚫"}.get(us_status, "⚪")
        st.markdown(f"**ETF 수집 상태:** {status_color} `{us_status}`")
        total_etf = etf_ok_count + len(failed_etfs)
        st.markdown(f"**성공 ETF 수:** {etf_ok_count} / {total_etf if total_etf else '?'}")

        if us_status == "partial_failed":
            st.warning("⚠️ 섹터 ETF 5개 미만 성공 → strong_sectors=[] 적용 (US 가점 없음)")

        if failed_etfs:
            st.markdown(f"**실패 ETF ({len(failed_etfs)}개):** {', '.join(failed_etfs[:15])}")
        else:
            st.caption("실패 ETF: 없음")

        unknown_count = sum(1 for s in excluded if s.get("excluded_reason") == "unknown_sector")
        if unknown_count:
            st.markdown(f"**unknown 섹터 제외:** {unknown_count}개 (하드 제외)")

        neg_cr = sum(1 for s in excluded if s.get("excluded_reason") == "negative_change_rate")
        low_cr = sum(1 for s in excluded if s.get("excluded_reason") == "change_rate_below_min")
        high_cr = sum(1 for s in excluded if s.get("excluded_reason") == "change_rate_above_max")
        if neg_cr or low_cr or high_cr:
            st.markdown(f"**상승률 필터:** 음수={neg_cr}개, 2%미만={low_cr}개, 15%초과={high_cr}개")
