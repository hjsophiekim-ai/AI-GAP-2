"""
2_Top15_종목선정.py

거래량 급증 종목 기반 Top10 선정
데이터 소스: https://finance.naver.com/sise/sise_quant_high.naver
현재가 조회: https://finance.naver.com/ (일반 페이지)
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

try:
    from app.data.naver_volume_spike_collector import collect_volume_spike_stocks
    from app.strategy.volume_spike_selector import VolumeSpikeSelector
    from app.config import get_config
except Exception as e:
    st.error(f"모듈 로드 오류: {e}")
    st.stop()

st.title("거래량급증 Top10 종목 선정")
st.caption("데이터 소스: 네이버 증권 거래량급증 — https://finance.naver.com/sise/sise_quant_high.naver")

st.info(
    "**상승률 조건: 3% 이상 18% 이하**  \n"
    "- 3% 미만: 매수 탄력 부족으로 제외  \n"
    "- 18% 초과: 추격매수 위험으로 제외  \n"
    "- 상승률 조건 위반 종목은 Top10 부족 시에도 fallback 복구 금지  \n"
    "- Top10 부족 시 1만원 이상 종목까지 확대 (가격 완화 fallback)"
)

st.divider()

# ---------------------------------------------------------------------------
# formatter helpers
# ---------------------------------------------------------------------------

def _fmt_tv(val: float) -> str:
    if val >= 1_000_000_000_000:
        return f"{val / 1_000_000_000_000:.1f}조"
    elif val >= 100_000_000:
        return f"{val / 100_000_000:.0f}억"
    else:
        return f"{val / 100_000_000:.1f}억"


# ---------------------------------------------------------------------------
# 선정 버튼
# ---------------------------------------------------------------------------

if st.button("거래량급증 Top10 선정하기", type="primary", use_container_width=True):
    vs_progress = st.progress(0, text="시작 중...")
    vs_log_area = st.empty()
    vs_msgs: list[str] = []

    def _log(msg: str) -> None:
        vs_msgs.append(msg)
        vs_log_area.markdown("\n".join(f"- {m}" for m in vs_msgs))

    try:
        # STEP 1: 네이버 거래량급증 수집
        vs_progress.progress(20, text="STEP 1: 네이버 거래량 급증 종목 수집 중...")
        _log("STEP 1: 네이버 거래량 급증 데이터 수집 (sise_quant_high.naver)")
        try:
            raw_vs = collect_volume_spike_stocks(max_pages=3, max_stocks=80)
            _log(f"  수집 완료: {len(raw_vs)}개")
        except Exception as ex:
            _log(f"  수집 실패: {ex}")
            raw_vs = []

        if not raw_vs:
            st.warning("거래량 급증 종목 수집 실패 또는 0개. 네트워크 연결을 확인하세요.")
            vs_progress.empty()
        else:
            # STEP 2: 상승률 5~15% 필터 + Top10 선정
            vs_progress.progress(60, text="STEP 2: 상승률 5~15% 필터 및 Top10 선정 중...")
            _log("STEP 2: 상승률 5~15% 필터 + Top10 선정")

            vs_selector = VolumeSpikeSelector()
            top10_vs, diag = vs_selector.select(raw_vs)

            _log(f"  전체 수집: {diag['total']}개")
            _log(f"  타입 제외 (ETF/ETN 등): {diag['excluded_type']}개")
            _log(f"  5% 미만 제외: {diag['excluded_below_5pct']}개")
            _log(f"  15% 초과 제외: {diag['excluded_above_15pct']}개")
            _log(f"  상승률 조건 통과: {diag['passed_rate_filter']}개")
            _log(f"  거래대금 30억+ primary: {diag['primary_pass']}개")
            _log(f"  거래대금 완화 fallback: {diag['fallback_added']}개")
            _log(f"  가격완화(1만원+) fallback: {diag.get('price_relaxed_added', 0)}개")
            _log(f"  최종 Top10: {diag['final_top10']}개")

            # STEP 3: CSV 저장
            vs_progress.progress(85, text="STEP 3: CSV 저장 중...")
            try:
                date_str = datetime.now().strftime("%Y%m%d")
                saved_vs = vs_selector.save_top10_csv(top10_vs, date_str=date_str)
                vs_selector.save_excluded_csv(date_str=date_str)
                _log(f"  CSV 저장: {saved_vs}")
            except Exception as ex:
                _log(f"  CSV 저장 실패: {ex}")

            st.session_state["volume_spike_top10"] = top10_vs
            st.session_state["volume_spike_diag"] = diag
            st.session_state["volume_spike_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            vs_progress.progress(100, text="완료!")
            if top10_vs:
                st.success(f"거래량급증 Top{len(top10_vs)}개 선정 완료!")
            else:
                st.warning("선정된 종목이 없습니다. 장 시작 전이거나 조건을 충족하는 종목이 없습니다.")

    except Exception as ex:
        st.error(f"거래량급증 선정 오류: {ex}")
        vs_progress.empty()


# ---------------------------------------------------------------------------
# 결과 표시
# ---------------------------------------------------------------------------

if st.session_state.get("volume_spike_top10"):
    top10_vs = st.session_state["volume_spike_top10"]
    diag = st.session_state.get("volume_spike_diag", {})
    at = st.session_state.get("volume_spike_at", "")
    if at:
        st.caption(f"선정 시각: {at}")

    # 진단 지표
    d_col1, d_col2, d_col3, d_col4, d_col5 = st.columns(5)
    d_col1.metric("5% 미만 제외", f"{diag.get('excluded_below_5pct', 0)}개")
    d_col2.metric("15% 초과 제외", f"{diag.get('excluded_above_15pct', 0)}개")
    d_col3.metric("상승률 조건 통과", f"{diag.get('passed_rate_filter', 0)}개")
    d_col4.metric("가격완화 fallback", f"{diag.get('price_relaxed_added', 0)}개")
    d_col5.metric("최종 Top10", f"{diag.get('final_top10', 0)}개")

    # 결과 테이블
    vs_rows = []
    for s in top10_vs:
        cr = s.get("change_rate", 0.0)
        cr_score = s.get("change_rate_score", 0.0)
        tv = s.get("trade_value", 0.0)
        cr_band = (
            "8~12% (최선호)" if 8.0 <= cr <= 12.0
            else ("5~8% (안정)" if 5.0 <= cr < 8.0
                  else "12~15% (주의)")
        )
        fallback_mark = ""
        if tv < 3_000_000_000:
            fallback_mark = "TV완화"
        if s.get("price_relaxed"):
            fallback_mark = ("TV완화+가격완화" if fallback_mark else "가격완화")
        vs_rows.append({
            "순위": s.get("rank", ""),
            "종목코드": s.get("symbol", ""),
            "종목명": s.get("name", ""),
            "현재가": f"{int(s.get('current_price', 0)):,}",
            "상승률(%)": round(cr, 2),
            "구간": cr_band,
            "상승률점수": cr_score,
            "거래대금": _fmt_tv(tv),
            "최종점수": round(s.get("final_score", 0.0), 2),
            "비고": fallback_mark,
        })

    st.dataframe(
        pd.DataFrame(vs_rows),
        use_container_width=True,
        hide_index=True,
    )

    # CSV 다운로드
    vs_csv_buf = io.StringIO()
    pd.DataFrame(vs_rows).to_csv(vs_csv_buf, index=False, encoding="utf-8-sig")
    st.download_button(
        label="거래량급증 Top10 CSV 다운로드",
        data=vs_csv_buf.getvalue().encode("utf-8-sig"),
        file_name=f"{datetime.now().strftime('%Y%m%d')}_volume_spike_top10.csv",
        mime="text/csv",
        key="dl_vs_top10",
    )

    st.page_link(
        "pages/3_예산배분_및_매수.py",
        label="→ 예산배분 및 매수로 이동",
        icon="💰",
    )
