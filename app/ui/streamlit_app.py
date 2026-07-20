import sys
from pathlib import Path
# Ensure project root is in sys.path for Render/cloud deployment
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st
from datetime import datetime

try:
    from app.utils.runtime_info import write_runtime_info
    _RUNTIME_INFO = write_runtime_info()
except Exception:
    _RUNTIME_INFO = {}

st.set_page_config(
    page_title="AI-GAP 갭상승 자동매매",
    layout="wide",
    initial_sidebar_state="expanded",
)

# The Hynix scheduler must survive browser/login state, so bootstrap it before
# the auth gate. Account/config-heavy UI work still stays behind require_login().
try:
    from app.services.hynix_auto_trade_scheduler import ensure_auto_trade_background_threads
    ensure_auto_trade_background_threads()
except Exception:
    pass

from app.ui.auth_gate import require_login
require_login()


from app.utils.startup_log import log_step_start as _log_step_start
from app.utils.startup_log import log_step_done as _log_step_done
from app.utils.startup_log import log_step_failed as _log_step_failed


# ---------------------------------------------------------------------------
# Import app config with graceful error handling
# ---------------------------------------------------------------------------
_config_error: str | None = None
_cfg = None

_log_step_start("config_load")
try:
    from app.config import get_config
    _cfg = get_config()
    _log_step_done("config_load")
except Exception as exc:
    _config_error = str(exc)
    _log_step_failed("config_load", exc)

# ---------------------------------------------------------------------------
# 데이터 루트 디렉토리 생성 + 실제 쓰기 테스트 — 원장/상태/캐시를 쓰기 전에
# 가장 먼저 수행한다. Render Persistent Disk(AI_GAP_DATA_DIR)가 마운트는 됐지만
# 권한/용량 문제로 실제로는 못 쓰는 경우를 앱이 뭔가 쓰기 전에 조기 발견한다.
# ---------------------------------------------------------------------------
_log_step_start("data_dir_bootstrap")
try:
    from app.utils.data_paths import ensure_data_dirs, check_writable
    ensure_data_dirs()
    _data_writable_probe = check_writable()
    if _data_writable_probe.get("writable"):
        _log_step_done("data_dir_bootstrap")
    else:
        _log_step_failed("data_dir_bootstrap", RuntimeError(_data_writable_probe.get("error") or "unknown"))
except Exception as _data_dir_exc:
    _log_step_failed("data_dir_bootstrap", _data_dir_exc)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mode_badge(mode: str) -> str:
    """Return an HTML badge string coloured by trading mode."""
    colours = {
        "dry_run": ("#2ecc71", "#155724"),   # green
        "mock":    ("#f1c40f", "#856404"),    # yellow
        "real":    ("#e74c3c", "#721c24"),    # red
    }
    bg, fg = colours.get(mode, ("#adb5bd", "#212529"))
    label = {"dry_run": "DRY RUN", "mock": "MOCK", "real": "REAL"}.get(mode, mode.upper())
    return (
        f'<span style="background-color:{bg};color:{fg};padding:3px 10px;'
        f'border-radius:4px;font-weight:bold;font-size:0.85rem;">{label}</span>'
    )


def _is_market_open() -> bool:
    try:
        from app.utils.time_utils import is_market_open
        return is_market_open()
    except Exception:
        now = datetime.now()
        current = now.hour * 60 + now.minute
        return (9 * 60) <= current <= (15 * 60 + 30)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("AI-GAP")
    st.caption("갭상승 자동매매 시스템")
    st.divider()

    # Mode badge
    if _cfg is not None:
        mode = _cfg.mode
    else:
        mode = "dry_run"

    st.markdown("**현재 모드**")
    st.markdown(_mode_badge(mode), unsafe_allow_html=True)
    st.divider()

    # Real mode status indicator
    _real_mode_active_sb = st.session_state.get("real_mode_enabled", False)
    if _real_mode_active_sb:
        st.markdown(
            '<div style="background:#e74c3c;color:#fff;padding:8px 10px;border-radius:4px;'
            'font-weight:bold;font-size:0.85rem;">🔴 실전모드 ON</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="background:#2ecc71;color:#155724;padding:8px 10px;border-radius:4px;'
            'font-weight:bold;font-size:0.85rem;">🟢 모의/안전모드</div>',
            unsafe_allow_html=True,
        )
    st.divider()

    # Date / time — refreshes on each rerun
    now = datetime.now()
    st.markdown(f"**날짜**: {now.strftime('%Y-%m-%d')}")
    st.markdown(f"**시각**: {now.strftime('%H:%M:%S')}")
    st.divider()

    # Navigation
    st.markdown("**페이지 이동**")
    st.page_link("pages/0_API연결.py",              label="API 연결",      icon="🔌")
    st.page_link("pages/10_시장판단_자동매매.py",   label="시장판단 자동매매", icon="🧭")
    st.page_link("pages/6_주도섹터_Top3.py",        label="주도섹터 Top3", icon="🎯")
    st.page_link("pages/2_Top15_종목선정.py",       label="Top15 선정",    icon="🔍")
    st.page_link("pages/3_예산배분_및_매수.py",     label="예산배분·매수", icon="💰")
    st.page_link("pages/4_보유종목_및_일괄매도.py", label="보유·매도",     icon="📤")
    st.page_link("pages/5_자동매도.py",             label="자동매도",      icon="🤖")
    st.page_link("pages/8_SK하이닉스_예측.py",      label="SK하이닉스 예측", icon="🔮")

    st.divider()

    if not _real_mode_active_sb:
        st.warning("⚠️ 실전투자 기본 비활성화\n\nAPI 연결 페이지에서 실전모드 버튼을 활성화하세요.")

    try:
        from app.ui.real_emergency_controls import render_real_emergency_stop
        st.divider()
        render_real_emergency_stop(prefix="sidebar")
    except Exception as _emergency_exc:
        st.error(f"긴급정지 UI 로드 실패: {_emergency_exc}")

# ---------------------------------------------------------------------------
# Real mode status banner — top of every page
# ---------------------------------------------------------------------------

_real_mode_active = st.session_state.get("real_mode_enabled", False)
if _real_mode_active:
    st.error(
        "현재 실전모드입니다. 실제 계좌에서 매수와 매도가 모두 실행될 수 있습니다.",
        icon="🔴",
    )
else:
    st.success(
        "현재 모의/안전 모드입니다. 실제 주문은 실행되지 않습니다.",
        icon="🟢",
    )

# ---------------------------------------------------------------------------
# Config-level import error banner
# ---------------------------------------------------------------------------

if _config_error:
    st.error(
        f"**설정 파일 로드 오류**\n\n"
        f"app.config 모듈을 불러오는 중 오류가 발생했습니다:\n\n```\n{_config_error}\n```\n\n"
        "config.yaml 파일과 환경변수(.env)를 확인하십시오."
    )

# ---------------------------------------------------------------------------
# Main page — overview
# ---------------------------------------------------------------------------

st.title("갭상승 Top15 선정 및 자동매매 프로그램")
st.caption("Korean Stock Gap-Up Scanner & Automated Trader — AI-GAP")

st.divider()

# --- Quick status dashboard ---
st.subheader("시스템 상태")

market_open = _is_market_open()
col1, col2, col3 = st.columns(3)

with col1:
    market_label = "장 중 (운영중)" if market_open else "장 외 (휴장)"
    market_icon  = "🟢" if market_open else "🔴"
    st.metric(label="시장 상태", value=f"{market_icon} {market_label}")

with col2:
    st.metric(label="운영 모드", value=mode.upper())

with col3:
    if _cfg is not None:
        try:
            budget = _cfg.trading.get("total_budget", 0)
            st.metric(label="총 예산", value=f"{budget:,.0f} 원")
        except Exception:
            st.metric(label="총 예산", value="설정 오류")
    else:
        st.metric(label="총 예산", value="N/A")

st.divider()

# --- Page link buttons ---
st.subheader("페이지")

pg_col1, pg_col2, pg_col3, pg_col4 = st.columns(4)

with pg_col1:
    st.page_link(
        "pages/0_API연결.py",
        label="🔌 API 연결",
        use_container_width=True,
    )
    st.caption("Mock/Real 계좌 연결 상태 확인")

with pg_col2:
    st.page_link(
        "pages/2_Top15_종목선정.py",
        label="🔍 Top15 선정",
        use_container_width=True,
    )
    st.caption("갭상승 Top15 종목 선정 (원클릭)")

with pg_col3:
    st.page_link(
        "pages/3_예산배분_및_매수.py",
        label="💰 예산배분·매수",
        use_container_width=True,
    )
    st.caption("수동/9:20 일괄 매수")

with pg_col4:
    st.page_link(
        "pages/4_보유종목_및_일괄매도.py",
        label="📤 보유·매도",
        use_container_width=True,
    )
    st.caption("조건/수동/10:15 일괄 매도")

st.divider()

# --- Config summary (informational) ---
if _cfg is not None:
    with st.expander("설정 요약 보기", expanded=False):
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("**거래 설정**")
            trading = _cfg.trading
            st.json(trading if isinstance(trading, dict) else {})
        with col_b:
            st.markdown("**안전 설정**")
            safety = _cfg.safety
            st.json(safety if isinstance(safety, dict) else {})

# ---------------------------------------------------------------------------
# 백그라운드 스레드 부트스트랩 — 제목·사이드바·대시보드가 이미 렌더된 뒤에만 실행한다.
# 특정 페이지를 열지 않아도, 서버 프로세스가 살아있는 한 auto_trade_on 상태를
# 감지해 동작해야 하므로 이 페이지 로드 시 계속 호출은 하되, 렌더 순서를
# "화면 먼저, 부트스트랩 나중"으로 바꿔 Render 콜드스타트 시 흰 화면 무한로딩을
# 방지한다. 두 함수 모두 이미 실행 중이면 아무것도 하지 않는 멱등(idempotent)
# 호출이며, auto_trade_on=False(기본값)이면 내부에서 KIS API를 전혀 호출하지
# 않는다(각 함수는 스레드만 시작하고 즉시 반환 — 실제 사이클/틱 로직이 그
# 안에서 auto_trade_on을 확인한다).
# ---------------------------------------------------------------------------
_log_step_start("watcher_bootstrap")
try:
    from app.trading.dynamic_exit_watcher import ensure_watcher_running
    ensure_watcher_running()
    _log_step_done("watcher_bootstrap")
except Exception as _watcher_exc:
    _log_step_failed("watcher_bootstrap", _watcher_exc)

_log_step_start("cycle_thread_bootstrap")
try:
    from app.services.hynix_auto_trade_scheduler import ensure_cycle_thread_running, ensure_fast_trend_watcher_running
    ensure_cycle_thread_running()
    ensure_fast_trend_watcher_running()
    _log_step_done("cycle_thread_bootstrap")
except Exception as _cycle_exc:
    _log_step_failed("cycle_thread_bootstrap", _cycle_exc)
