"""ARM3 하네스 사본 생성 — 원본: research_20261029_n1_morning_stop_p70r/harness/test_zz_par.py

production 코드는 한 줄도 바꾸지 않는다. 아래 자리에만 **덧붙인다**.

| # | 덧붙이는 것 | env |
|---|---|---|
| ① | 전략모드를 env 로 고른다 (CHOP / N1 / P3 / E) — VAR 별칭 대신 | `WK_MODE` |
| ② | MU Assist / XS 토글 + MU·체결강도 **재생 피드**(ws_shadow 1초 스냅샷) | `WK_MUA` `WK_XS` `WK_CUKILL` |
| ③ | 실제 ETF/하이닉스 **1초 틱**을 시세·호가·E 감시가로 쓴다 | `WK_TICKPX` |
| ④ | +3% 틱 ARM 수익보호선 (B=SOFT / C=MEDIUM) | `WK_ARM` |
| ⑤ | 진입 게이트 전수 추적 로그 (T+3 판정 한 건마다 모든 하위게이트) | 항상 |

parity ①②③(hist lag / fill lag / fine tick)은 원본 그대로 살아 있다.

    python research_20261030_tick_arm3/scripts/h0_make.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")

SRC = ROOT / "research_20261029_n1_morning_stop_p70r" / "harness" / "test_zz_par.py"
DST = ROOT / "research_20261030_tick_arm3" / "harness" / "test_zz_arm3.py"

# ── ① 모듈 최상단 상수 ────────────────────────────────────────────────────
HEAD_CONST = '''
# ── 2026-10-30 ARM3 연구 파라미터 (env. 전부 기본값이면 PAR 하네스와 동일) ──
#: ws_shadow_logger 가 모은 1초 스냅샷 루트 (snapshots/<YYYY-MM-DD>/<종목>.csv).
_RT = Path(os.environ.get("ARM3_RT", "") or "")
#: 1 이면 ETF/하이닉스 시세·호가를 1분봉 선형보간이 아니라 **실제 1초 스냅샷**으로 준다.
_TICKPX = os.environ.get("WK_TICKPX", "0") == "1"
#: NONE / SOFT(B) / MEDIUM(C)
_ARM = (os.environ.get("WK_ARM", "NONE") or "NONE").upper()
#: 전략모드 — 비어 있으면 기존 VAR 분기 그대로.
_MODE = (os.environ.get("WK_MODE", "") or "").upper()
_MUA = os.environ.get("WK_MUA", "0") == "1"
_XS = os.environ.get("WK_XS", "0") == "1"
_CUKILL = os.environ.get("WK_CUKILL", "0") == "1"
#: 보호선 사다리 — 사용자 사전등록값. 키 = 틱 최고 순수익(%), 값 = 보호선(%).
_ARM_LADDER = {
    "SOFT":   ((4.0, 2.5), (3.5, 2.0), (3.0, 1.5)),
    "MEDIUM": ((4.0, 3.0), (3.5, 2.5), (3.0, 2.0)),
    # 2026-10-31 후속: 추세약화 확인형. 사다리는 SOFT 와 같고 **활성화 조건만** 다르다.
    "W1":     ((4.0, 2.5), (3.5, 2.0), (3.0, 1.5)),
    "W2":     ((4.0, 2.5), (3.5, 2.0), (3.0, 1.5)),
}
#: 보호 활성화에 필요한 추세약화 신호 개수. 0 = +3% 도달 즉시 활성(기존 SOFT/MEDIUM).
_ARM_NEED = {"SOFT": 0, "MEDIUM": 0, "W1": 1, "W2": 2}

# ── 2026-10-31 ⑤ CHOP 진입 + N1 runner 승격 ───────────────────────────────
#: 1 이면 CHOP regime 진입 중 production n1_adaptive.snapshot().ok 인 것만
#: TREND 로 각인해 B3 스택 대신 N1 래더(runner)로 관리한다. 진입게이트는 불변.
_TRBP = os.environ.get("WK_TREND_BYPASS", "0") == "1"

# ── 2026-10-31 ④ ARM 3% ONLY — 트레일링 arm 시작점만 3.5% -> 3.0% ─────────
#: 0 = OFF. 값이 있으면 트레일링 arm 임계값(%)을 그 값으로 바꾼다. 보호선 자체
#: (N1_MORNING_TRAILING_STOP 1.5%)와 나머지 청산은 **한 줄도 바꾸지 않는다**.
_TARM = float(os.environ.get("WK_TRAIL_ARM", "0") or 0)

# ── 2026-10-31 ③ 오후 XS VWAP 근접 override ───────────────────────────────
#: 0 = OFF. 값이 있으면 그 값(%)을 VWAP '불리폭' 상한으로 쓴다 (0.10 / 0.20).
_VWOV = float(os.environ.get("WK_VWAP_OVR", "0") or 0)

# ── 2026-10-31 ② 청산 유예 (ONE_BAR_HOLD / TWO_BAR_HOLD) ──────────────────
#: NONE / ONE(B, 1봉) / TWO(C, 최대 2봉)
_HOLD = (os.environ.get("WK_HOLD", "NONE") or "NONE").upper()
_HOLD_BARS = {"ONE": 1, "TWO": 2}.get(_HOLD, 0)
#: 유예 **대상** 청산사유 — "1~2봉짜리 일시 휩쏘일 수 있는" 것만. 부분 문자열로 본다.
_HOLD_TARGET = (
    "OPPOSITE_SIGNAL", "TW2_3SLOT_SELL_ONLY",          # 반대 플래그 (진입 미승인분)
    "H50_WHIPSAW_RELEASE", "H50_MAX_HOLD_EXIT", "MU_ASSIST_H50_GAP_EXIT",
    "C1_PEAK_PROTECTION_EXIT", "TIME_WINDOW_TRAILING_STOP",
    "TIME_WINDOW_PROFIT_LOCK_STOP", "TIME_WINDOW_AFTER_TP1_STOP",
    "TIME_WINDOW_BREAKEVEN_STOP", "B3_MAXHOLD",
)
#: **절대 유예하지 않는다** — hard stop / 구조 확정이탈 / 익절 / 장마감.
_HOLD_NEVER = (
    "STOP_LOSS", "B3_SL", "FORCED_LIQUIDATION", "TP1", "TP2", "B3_TP",
    "AFTERNOON_TP", "P3_PARTIAL", "TICK_ARM3_STOP", "RUNNER",
)


def _arm_line(peak):
    """틱 최고 순수익 peak(%) 에 대응하는 보호선(%). 사다리 밖이면 None.

    +5% 이상에서 보호선을 더 올리지 않는다 — 최고 단은 peak>=4.0 하나뿐이다.
    그 위 구간은 기존 TP/C1 러너 규칙이 그대로 지배한다(사용자 사전등록)."""
    for lv, line in _ARM_LADDER.get(_ARM, ()):
        if peak >= lv:
            return line
    return None


_TICK_CACHE = {}


def _tick_df(sym, day):
    """그 날 1초 스냅샷. ts(naive KST) index, price/best_bid/best_ask."""
    key = (sym, day)
    if key not in _TICK_CACHE:
        p = _RT / "snapshots" / day / f"{sym}.csv"
        if not _RT or not p.exists():
            _TICK_CACHE[key] = None
        else:
            df = pd.read_csv(p, usecols=["ts", "price", "best_bid", "best_ask"])
            df["ts"] = pd.to_datetime(df["ts"])
            df = df.dropna(subset=["price"]).drop_duplicates("ts", keep="last")
            _TICK_CACHE[key] = df.set_index("ts").sort_index()
    return _TICK_CACHE[key]


def _tick_row(sym, now, max_age_sec=90):
    """now 시점에 **이미 수신된** 마지막 1초 스냅샷. 미래 데이터는 쓰지 않는다."""
    ts = pd.Timestamp(now)
    nv = ts.tz_localize(None) if ts.tzinfo is not None else ts
    df = _tick_df(sym, nv.strftime("%Y-%m-%d"))
    if df is None or df.empty:
        return None
    i = df.index.searchsorted(nv, side="right") - 1
    if i < 0 or (nv - df.index[i]).total_seconds() > max_age_sec:
        return None
    return df.iloc[i]


def _tick_window(sym, t0, t1):
    """(t0, t1] 구간의 1초 스냅샷 전부 — 틱 최고가/보호선 이탈 판정용."""
    a, b = pd.Timestamp(t0), pd.Timestamp(t1)
    a = a.tz_localize(None) if a.tzinfo is not None else a
    b = b.tz_localize(None) if b.tzinfo is not None else b
    df = _tick_df(sym, b.strftime("%Y-%m-%d"))
    if df is None or df.empty:
        return None
    w = df[(df.index > a) & (df.index <= b)]
    return w if len(w) else None
'''

# ── ② MU / 체결강도 재생 피드 (원본: research_20261026_mu_cttr 하네스) ──────
MU_FEED = '''    monkeypatch.setattr(worker, "_git_sha", lambda: "replay")

    # ── 2026-10-26 MU×체결강도 재생 시계 보정 (정의 변경 아님 — 가시범위만 라이브와 맞춘다) ──
    from app.trading.macd2 import exec_strength as _xs
    from app.trading.macd2 import mu_assist as _mua

    _CTTR_CACHE: dict = {}
    _MU_CACHE: dict = {}

    def _cttr_series(root, day):
        key = (str(root), day)
        if key not in _CTTR_CACHE:
            p = Path(root) / "snapshots" / day / "000660.csv"
            if not p.exists():
                _CTTR_CACHE[key] = None
            else:
                df = pd.read_csv(p, usecols=["ts", "cttr"])
                df["ts"] = pd.to_datetime(df["ts"])
                _CTTR_CACHE[key] = df.dropna(subset=["cttr"]).set_index("ts")["cttr"].sort_index()
        return _CTTR_CACHE[key]

    def _mean_cttr_replay(now, data_dir=None):
        """exec_strength.mean_cttr 와 같은 정의([now-60s, now) 평균), 파일 전체에서 찾는다."""
        root = data_dir if data_dir is not None else config.EXEC_STRENGTH_DATA_DIR
        if not root:
            return None
        end = _xs._naive_kst(now)
        s = _cttr_series(root, end.strftime("%Y-%m-%d"))
        if s is None or s.empty:
            return None
        w = s[(s.index >= end - timedelta(seconds=_xs.WINDOW_SEC)) & (s.index < end)]
        return float(w.mean()) if len(w) else None

    _orig_load_mu_1m = _mua.load_mu_1m

    def _load_mu_1m_replay(now, data_dir=None):
        """그 날 전체가 아니라 now 까지 **끝난** 분봉만 — 라이브와 같은 가시범위."""
        key = (str(data_dir), _mua._naive(now).strftime("%Y-%m-%d"))
        if key not in _MU_CACHE:
            _MU_CACHE[key] = _orig_load_mu_1m(now, data_dir)
        s = _MU_CACHE[key]
        if s is None:
            return None
        return s[s.index + pd.Timedelta(minutes=1) <= _mua._naive(now)]

    monkeypatch.setattr(_xs, "mean_cttr", _mean_cttr_replay)
    monkeypatch.setattr(_mua, "load_mu_1m", _load_mu_1m_replay)
    if _CUKILL:
        monkeypatch.setattr(config, "MU_CATCHUP_KILL", True)
'''

# ── ③ 실제 1초 틱을 시세로 (ETF) ──────────────────────────────────────────
TICK_PX = '''    def _px_raw(sym, now):
        if _TICKPX:
            _r = _tick_row(sym, now)
            if _r is not None:
                return float(_r["price"])
        d = etf[sym]
'''

# ETF 호가: 1초 스냅샷의 최우선 호가. 없으면 기존 1틱 스프레드 모형.
BOOK = '''    def _book(sym, now):
        """(bid1, ask1). 1초 스냅샷이 있으면 실제 최우선호가, 없으면 기존 모형(시세, 시세+5)."""
        if _TICKPX:
            r = _tick_row(sym, now)
            if r is not None and pd.notna(r.get("best_bid")) and pd.notna(r.get("best_ask")):
                b, a = float(r["best_bid"]), float(r["best_ask"])
                if b > 0 and a > 0:
                    return b, a
        p = px(sym, now)
        return (None, None) if p is None else (float(p), float(p) + ETF_TICK)

    BOOKQ = {}

    def _set_book(sym, now):
        b, a = _book(sym, now)
        if b is not None:
            BOOKQ[sym] = (b, a)
        return b

'''

# ── ④ 모드/토글을 env 로 ─────────────────────────────────────────────────
MODE_BLOCK = '''    if _MODE:
        # ── 2026-10-30 ARM3: 전략모드·MU 토글을 env 로 직접 고른다 ──────────
        _MODE_MAP = {"CHOP": strategy_mode.MODE_CHOP, "E": strategy_mode.MODE_E,
                     "P3": strategy_mode.MODE_P3, "N1": strategy_mode.MODE_N1}
        if _MODE in ("CHOP", "E"):
            from app.trading.macd2 import e_strategy as _es
            monkeypatch.setattr(config, "E_RS_TABLE_PATH", os.environ["RS_TABLE"])
            if hasattr(_es._table_from_path, "_cache"):
                del _es._table_from_path._cache
            if os.environ.get("WK_WATCH_RAW", "1") == "1":
                monkeypatch.setattr(worker, "_e_watch_price", lambda market_data, df_1m: _hy_px(clock["now"]))
        strategy_mode.apply(state, _MODE_MAP[_MODE])
        # 현행 운영 기준선 = ECHOPN (고정 ±1.5% 청산 OFF). env 로만 되돌린다.
        state.fixed_exit_15_enabled = os.environ.get("WK_FIX15", "0") == "1"
        state.mu_assist_enabled = bool(_MUA)
        state.mu_assist_xs_enabled = bool(_XS)
    elif VAR in ("EPROD", "ECHOP", "ECHOPB", "ECHOPC", "PAR", "PAR70", "PAR50"):
'''

# ── ⑤ 진입 게이트 전수 추적 + ARM3 보호선 ───────────────────────────────
ARM_BLOCK = '''    STATE["s"] = state

    # ── 2026-10-30 ARM3 ① 진입 게이트 전수 추적 (기록 전용, 판정 무영향) ──────
    GATELOG = []
    from app.trading.macd2 import exec_strength as _xsm
    from app.trading.macd2 import mu_assist as _muam
    from app.trading.macd2 import mu_catchup as _mucu
    from app.trading.macd2 import teg_gate as _tegm
    from app.trading.macd2 import time_window_3slot as _t3m
    from app.trading.macd2 import time_window_filter as _twfm

    def _jsonable(v):
        if isinstance(v, (int, float, bool, str, type(None))):
            return v
        if isinstance(v, (list, tuple, set, frozenset)):
            return [_jsonable(x) for x in v]
        if isinstance(v, dict):
            return {str(k): _jsonable(x) for k, x in v.items()}
        return str(v)

    def _glog(ev, **kw):
        GATELOG.append(dict(ev=ev, t=clock["now"].isoformat(), **{k: _jsonable(v) for k, v in kw.items()}))

    def _wrap(mod, name, rec):
        orig = getattr(mod, name)

        def w(*a, **k):
            r = orig(*a, **k)
            try:
                rec(r, a, k)
            except Exception:
                pass
            return r
        monkeypatch.setattr(mod, name, w)

    _wrap(_twfm, "evaluate_time_window_entry", lambda r, a, k: _glog(
        "TW", dir=getattr(a[1] if len(a) > 1 else k.get("flag_direction"), "value", None),
        flag_bar=str(a[2] if len(a) > 2 else k.get("flag_bar_dt")), approved=bool(r.approved),
        decision=r.decision, block=r.block_reason, skip_conf=bool(k.get("skip_confirmation_check")),
        metrics=dict(r.metrics or {})))
    _wrap(_twfm, "evaluate_tw2_extra_vetoes", lambda r, a, k: _glog(
        "TW2VETO", dir=getattr(a[1] if len(a) > 1 else None, "value", None), vetoed=bool(r[0]), reason=r[1]))
    _wrap(_tegm, "evaluate_teg", lambda r, a, k: _glog(
        "TEG", dir=getattr(a[1] if len(a) > 1 else None, "value", None),
        approved=bool(r.approved), reasons=list(r.reject_reasons or [])))
    _wrap(_t3m, "resolve_slot", lambda r, a, k: _glog(
        "SLOT", dir=getattr(k.get("direction"), "value", None), allowed=bool(r.slot_allowed),
        slot=r.slot_number, session=r.session, reject=r.reject_reason,
        need_quality=bool(r.requires_quality_gate), need_teg=bool(r.requires_teg_gate),
        used=k.get("slots_used_today"), am=k.get("morning_count"), pm=k.get("afternoon_count"),
        cap=k.get("daily_cap")))
    _wrap(_t3m, "evaluate_trend_quality", lambda r, a, k: _glog(
        "QUALITY", passed=int(r.passed_count), approved=bool(r.approved)))
    _wrap(_xsm, "evaluate", lambda r, a, k: _glog(
        "XSTRENGTH", dir=getattr(a[0] if a else None, "value", None), state=r.get("state"), cttr=r.get("cttr")))
    _wrap(_muam, "entry_status_for", lambda r, a, k: _glog("MU_ENTRY_STATUS", sid=a[0] if a else None, status=r))
    _wrap(_muam, "entry_veto_reason", lambda r, a, k: _glog("MU_ENTRY_VETO", sid=a[0] if a else None, veto=r))
    _wrap(_mucu, "evaluate", lambda r, a, k: _glog("MU_CATCHUP", res=r))
    _wrap(worker, "_mu_xs_strong", lambda r, a, k: _glog(
        "XS_STRONG", sid=(a[1] if len(a) > 1 else None), strong=bool(r), cttr=(r or {}).get("cttr")))
    _wrap(worker, "_mu_xs_teg_override", lambda r, a, k: _glog("XS_TEG_OVERRIDE", applied=bool(r)))
    def _rec_decision(r, a, k):
        """T+3 판정 기록 + 신호 ⑥(반대 플래그 T+3 확정 전) 상태 갱신.

        `TIME_WINDOW_PENDING_CONFIRMATION` 이면 그 플래그는 아직 T+3 확정 전이다.
        같은 signal_id 로 그 밖의 판정이 오면(승인/거절) 확정된 것이므로 지운다."""
        dec, sid = a[1], (a[2] if len(a) > 2 else "")
        _glog("DECISION", sid=sid, approved=bool(dec.approved), decision=dec.decision,
              block=dec.block_reason, metrics=dict(dec.metrics or {}))
        base = str(sid or "").split(":")[0]
        if not base:
            return
        d = "UP_RED" if base.endswith("UP_RED") else "DOWN_BLUE"
        if str(dec.block_reason or "") == config.TW_PENDING_CONFIRMATION:
            OPP_PENDING[base] = d
        else:
            OPP_PENDING.pop(base, None)
        if bool(dec.approved):
            OPP_APPROVED[base] = d
    _wrap(worker, "_persist_tw2_3slot_decision", _rec_decision)

    # ── ARM3 ② +3% 틱 수익보호선 ──────────────────────────────────────────
    #   SOFT(B)/MEDIUM(C) : 2026-10-30. +3% 도달 **즉시** 보호 활성.
    #   W1 / W2           : 2026-10-31 후속. +3% 도달은 **ARM 후보**일 뿐이고,
    #                       추세약화 신호가 1개(W1) / 2개(W2) 확인될 때만 보호를 켠다.
    # 기존 청산(TP/SL/C1/B3/H50/반대신호/시간/15:00)은 한 줄도 바꾸지 않는다.
    # 같은 tick 에 기존 청산이 발동하면 그쪽이 이긴다 — 이 블록은 기존 위험관리가
    # 아무것도 하지 않은 tick 에서만 판정한다("먼저 발생한 것 적용").
    ARM = {"epoch": None, "peak": 0.0, "line": None, "prev_t": None,
           "cand": False, "active": False, "mu_confirm": False, "sigs": ()}
    ARM3LOG = []
    ARM_REASON = "TICK_ARM3_STOP"
    #: 반대방향 플래그가 T+3 확정 전인지 — 게이트 추적 wrapper 가 갱신한다(신호 ⑥).
    OPP_PENDING = {}
    #: T+3 확정 + 진입게이트까지 **통과한** 플래그 (청산 유예의 강한 붕괴 ④).
    OPP_APPROVED = {}

    if _ARM in _ARM_LADDER:
        from app.trading.macd2 import major_flag_filter as _mff
        from app.trading.macd2 import mu_assist as _muw
        from app.trading.macd2 import order_executor as _oex
        from app.trading.macd2 import signal_engine as _sew
        from app.trading.macd2 import time_window_filter as _twfw
        from app.trading.macd2.models import Direction as _Dir
        from app.trading.macd2.models import SignalState as _SS
        _NEED = _ARM_NEED.get(_ARM, 0)
        _o_rm = worker._advance_held_position_risk_management
        _WCACHE = {"min": None, "bars": None, "mu": None}

        def _held_dir(sym):
            return _Dir.UP_RED if sym == config.LONG_SYMBOL else _Dir.DOWN_BLUE

        def _bars_now(now):
            """판정시점까지 **완성된** 3분봉만 (production resample 그대로). 분 단위 캐시."""
            m = now.replace(second=0, microsecond=0)
            if _WCACHE["min"] != m:
                _WCACHE["min"] = m
                _WCACHE["mu"] = None
                try:
                    _WCACHE["bars"] = _mff._prepare_bars(
                        _sew.resample_completed_3m(svc.get_history_df(), now=now))
                except Exception:
                    _WCACHE["bars"] = None
            return _WCACHE["bars"]

        def _mu_pattern(now, dirn):
            """롤링 MU 패턴. 앵커를 now-5분에 두면 production evaluate_pattern 의 T+2 창이
            정확히 '지금'이 된다 — 끝난 1분봉만 보므로 미래 데이터가 들어가지 않는다."""
            if _WCACHE["mu"] is None:
                try:
                    ser = _muw.load_mu_1m(now, config.MU_ASSIST_DATA_DIR or None)
                    anchor = now.replace(second=0, microsecond=0) - timedelta(minutes=5)
                    _WCACHE["mu"] = str(_muw.evaluate_pattern(ser, dirn, anchor).get("mu_pattern_state"))
                except Exception:
                    _WCACHE["mu"] = _muw.PATTERN_UNAVAILABLE
            return _WCACHE["mu"]

        def _weak_signals(now, sym, cur_net):
            """추세약화 신호 6종. 전부 판정시점까지 확보된 데이터만 쓴다. 판정 불가는 False."""
            dirn = _held_dir(sym)
            sgn = 1.0 if dirn == _Dir.UP_RED else -1.0
            work = _bars_now(now)
            out = {}
            # ① HY 3분 MACD gap 이 직전 봉 대비 축소 (보유방향 부호)
            g_now = g_prev = None
            try:
                ser = _twfw._gap_series(work) if work is not None else None
                if ser is not None and len(ser) >= 2:
                    g_now = float(ser["gap"].iloc[-1]) * sgn
                    g_prev = float(ser["gap"].iloc[-2]) * sgn
            except Exception:
                pass
            out["s1_gap_shrink"] = bool(g_now is not None and g_prev is not None and g_now < g_prev)
            # ② MU 가 CONFIRM 을 봤다가 STALL/DIVERGE 로 약화
            pat = _mu_pattern(now, dirn)
            if pat == _muw.PATTERN_CONFIRM:
                ARM["mu_confirm"] = True
            out["s2_mu_weak"] = bool(ARM["mu_confirm"]
                                     and pat in (_muw.PATTERN_STALL, _muw.PATTERN_DIVERGE))
            # ③ 체결강도가 보유방향에서 약화 (production 정의 그대로, 기준 100 고정)
            try:
                cs = _xs.evaluate(dirn, now)
                out["s3_cttr_weak"] = bool(cs["state"] == _xs.MISMATCH)
            except Exception:
                out["s3_cttr_weak"] = False
            # ④ 최근 틱 고점 대비 0.5%p 이상 반납
            out["s4_giveback"] = bool(cur_net is not None and (ARM["peak"] - cur_net) >= 0.5)
            # ⑤ 1분봉 **또는** 3분봉이 VWAP 방향 우위를 잃음 (production _session_vwap 재사용)
            lost = False
            try:
                if work is not None and len(work):
                    vw = _mff._session_vwap(work)
                    v = float(vw.iloc[-1]) if len(vw) and pd.notna(vw.iloc[-1]) else None
                    if v and v > 0:
                        c3 = float(work["close"].iloc[-1])
                        lost = (c3 < v) if dirn == _Dir.UP_RED else (c3 > v)
                        if not lost:
                            d1 = svc.get_history_df()
                            if d1 is not None and len(d1):
                                c1 = float(d1["close"].iloc[-1])
                                lost = (c1 < v) if dirn == _Dir.UP_RED else (c1 > v)
            except Exception:
                pass
            out["s5_vwap_lost"] = bool(lost)
            # ⑥ 반대방향 플래그가 떴지만 아직 T+3 확정 전
            opp = "DOWN_BLUE" if dirn == _Dir.UP_RED else "UP_RED"
            out["s6_opp_pending"] = any(v == opp for v in OPP_PENDING.values())
            return out

        def _arm_rm(*, broker, state, market_data, now, quotes, pos, result):
            fired = _o_rm(broker=broker, state=state, market_data=market_data, now=now,
                          quotes=quotes, pos=pos, result=result)
            ep = int(getattr(state, "position_epoch", 0) or 0)
            if ARM["epoch"] != ep:
                ARM.update(epoch=ep, peak=0.0, line=None, prev_t=None,
                           cand=False, active=False, mu_confirm=False, sigs=())
            if fired or state.position is None or int(state.position.quantity or 0) <= 0:
                return fired
            sym, avg, qty = pos.symbol, float(pos.avg_price), int(pos.quantity)
            t0 = ARM["prev_t"] or (now - timedelta(seconds=_TICK_SEC))
            ARM["prev_t"] = now
            # 틱 최고수익: (직전 tick, 지금] 의 실제 1초 스냅샷 전부. 없으면 지금 시세 하나.
            w = _tick_window(sym, t0, now)
            if w is not None:
                hi = float(w["price"].max())
                last = float(w["price"].iloc[-1])
                lo_bid = float(w["best_bid"].min()) if w["best_bid"].notna().any() else None
            else:
                cur = quotes.get(sym) or px(sym, now)
                hi = last = (float(cur) if cur else None)
                lo_bid = BOOKQ.get(sym, (None, None))[0]
            if hi is not None:
                ARM["peak"] = max(ARM["peak"], worker._net_return_pct(sym, avg, hi, qty))
            cur_net = worker._net_return_pct(sym, avg, last, qty) if last is not None else None
            # ── 활성화 판정 ─────────────────────────────────────────────────
            if not ARM["active"]:
                if _arm_line(ARM["peak"]) is None:
                    return fired                       # 아직 +3% 미도달
                if not ARM["cand"]:
                    ARM["cand"] = True
                    ARM3LOG.append(dict(k="CAND", t=now.isoformat(), sym=sym,
                                        peak=round(ARM["peak"], 4), epoch=ep))
                if _NEED <= 0:
                    ARM["active"], ARM["sigs"] = True, ()
                else:
                    sg = _weak_signals(now, sym, cur_net)
                    hits = tuple(k for k, v in sg.items() if v)
                    if len(hits) < _NEED:
                        return fired                   # 추세약화 미확인 — 보호를 켜지 않는다
                    ARM["active"], ARM["sigs"] = True, hits
                    ARM3LOG.append(dict(k="ACTIVE", t=now.isoformat(), sym=sym,
                                        peak=round(ARM["peak"], 4), need=_NEED,
                                        sigs=list(hits), all=sg,
                                        cur_net=(round(cur_net, 4) if cur_net is not None else None),
                                        epoch=ep))
            # ── 보호선 갱신 (한 번 올라가면 내려가지 않는다) ─────────────────
            line = _arm_line(ARM["peak"])
            if line is not None and (ARM["line"] is None or line > ARM["line"]):
                ARM["line"] = line
                ARM3LOG.append(dict(k="ARM", t=now.isoformat(), sym=sym, peak=round(ARM["peak"], 4),
                                    line=ARM["line"], sigs=list(ARM["sigs"]), epoch=ep))
            if ARM["line"] is None or lo_bid is None:
                return fired
            bid_net = worker._net_return_pct(sym, avg, float(lo_bid), qty)
            if bid_net > ARM["line"]:
                return fired
            ARM3LOG.append(dict(k="BREACH", t=now.isoformat(), sym=sym, peak=round(ARM["peak"], 4),
                                line=ARM["line"], bid=float(lo_bid), bid_net=round(bid_net, 4),
                                sigs=list(ARM["sigs"]), epoch=ep))
            out = _oex.execute_exit(broker=broker, symbol=sym, quantity=qty,
                                    exit_reason=ARM_REASON, entry_price=avg,
                                    reconcile_retries=worker.ORDER_FILL_RECONCILE_RETRIES,
                                    reconcile_delay_sec=worker.ORDER_FILL_RECONCILE_DELAY_SEC)
            worker._apply_exit_outcome(state, out)
            if out.final_state == _SS.EXECUTED:
                state.time_window_position_active = False
                ARM3LOG.append(dict(k="EXIT", t=now.isoformat(), sym=sym, qty=qty,
                                    px=float(getattr(out.sell_result, "executed_price", 0.0) or 0.0),
                                    epoch=ep))
            result.actions.append(f"{ARM_REASON}:{sym}")
            return True
        monkeypatch.setattr(worker, "_advance_held_position_risk_management", _arm_rm)

    # ── 2026-10-31 ④ ARM 3% ONLY ─────────────────────────────────────────
    # 바꾸는 것은 **arm 시작점 하나**다:
    #   N1_TREND_TP1            0.035 -> _TARM/100   (매도비중 0.0 = arm 전용)
    #   MORNING_TRAILING_TRIGGER 0.035 -> _TARM/100
    # 보호선(N1_MORNING_TRAILING_STOP 1.5%)·TP2·손절·C1·B3·H50 은 그대로다.
    # tpm 은 import 시점에 퍼센트 상수를 굳히므로 그 모듈 상수도 같이 바꾼다.
    if _TARM > 0:
        monkeypatch.setattr(config, "N1_TREND_TP1", _TARM / 100.0)
        monkeypatch.setattr(config, "MORNING_TRAILING_TRIGGER", _TARM / 100.0)
        monkeypatch.setattr(tpm, "MORNING_TRAILING_TRIGGER_PCT", _TARM)

    # ── 2026-10-31 ③ 오후 XS VWAP 근접 override ───────────────────────────
    # MU ENTRY_CONFIRM 인 **오후** 후보가 **VWAP 사유 하나로만** 거절될 때,
    # 그 불리폭이 _VWOV% 이내면 그 거절만 푼다. 체결강도는 조건에서 제외한다.
    # 다른 TEG 사유(EMA stack / gap / spread / 시간성)가 하나라도 있으면 그대로 거절.
    # 슬롯·하루한도·시간창·MU ENTRY_BAD·CHOP MC·TP/SL 은 손대지 않는다.
    VWOVLOG = []

    if _VWOV > 0:
        from app.trading.macd2 import major_flag_filter as _mffv
        from app.trading.macd2 import mu_assist as _muv
        from app.trading.macd2 import signal_engine as _sev
        from app.trading.macd2 import teg_gate as _tegv
        from app.trading.macd2 import time_window_filter as _twfv
        from app.trading.macd2.models import Direction as _Dv

        _VWAP_ONLY = {config.TW2_REJECT_VWAP_VETO, _tegv.COND_VWAP}

        def _vw_dev(bars_3m, direction):
            """후보 방향 기준 VWAP 편차(%). 음수면 불리한 쪽. production _session_vwap 재사용."""
            try:
                work = _mffv._prepare_bars(bars_3m)
                if work is None or not len(work):
                    return None
                vw = _mffv._session_vwap(work)
                if not len(vw) or pd.isna(vw.iloc[-1]) or float(vw.iloc[-1]) <= 0:
                    return None
                sgn = 1.0 if direction == _Dv.UP_RED else -1.0
                return (float(work["close"].iloc[-1]) / float(vw.iloc[-1]) - 1.0) * 100.0 * sgn
            except Exception:
                return None

        def _vw_mu_confirm(now, direction):
            """롤링 MU ENTRY_CONFIRM (= MU CONFIRM ∧ HY gap EXPAND) — 끝난 1분봉만 본다."""
            try:
                ser = _muv.load_mu_1m(now, config.MU_ASSIST_DATA_DIR or None)
                anchor = now.replace(second=0, microsecond=0) - timedelta(minutes=5)
                return str(_muv.evaluate_pattern(ser, direction, anchor).get(
                    "mu_pattern_state")) == _muv.PATTERN_CONFIRM
            except Exception:
                return False

        def _vw_ok(bars_3m, direction, now, reasons, where):
            """이 거절을 풀어도 되는가. 체결강도는 보지 않는다(기록만)."""
            rs = set(reasons or ())
            if not rs or not (rs <= _VWAP_ONLY):
                return False
            if now.astimezone(config.KST).time() < config.TW2_3SLOT_MORNING_WINDOW_END:
                return False                      # 오전은 대상 아님
            if not _vw_mu_confirm(now, direction):
                return False
            dev = _vw_dev(bars_3m, direction)
            ok = bool(dev is not None and -_VWOV <= dev < 0)
            try:
                cs = _xs.evaluate(direction, now)
            except Exception:
                cs = {}
            VWOVLOG.append(dict(t=now.isoformat(), where=where, dir=getattr(direction, "value", None),
                                reasons=sorted(rs), dev=(round(dev, 4) if dev is not None else None),
                                lim=_VWOV, mu_confirm=True, cttr=cs.get("cttr"),
                                cttr_state=cs.get("state"), applied=ok))
            return ok

        _o_veto_v = _twfv.evaluate_tw2_extra_vetoes

        def _veto_v(bars_3m, flag_direction, flag_bar_dt, decision_at):
            vetoed, reason = _o_veto_v(bars_3m, flag_direction, flag_bar_dt, decision_at)
            if vetoed and _vw_ok(bars_3m, flag_direction, decision_at, {reason}, "TW2_VETO"):
                return False, None
            return vetoed, reason
        monkeypatch.setattr(_twfv, "evaluate_tw2_extra_vetoes", _veto_v)

        _o_teg_v = _tegv.evaluate_teg

        def _teg_v(bars_3m, direction, flag_bar_dt, now, *a, **k):
            dec = _o_teg_v(bars_3m, direction, flag_bar_dt, now, *a, **k)
            if (not dec.approved) and _vw_ok(bars_3m, direction, now, dec.reject_reasons, "TEG"):
                return dataclasses.replace(dec, approved=True, reject_reasons=())
            return dec
        monkeypatch.setattr(_tegv, "evaluate_teg", _teg_v)

    # ── 2026-10-31 ② 청산 유예 ONE_BAR_HOLD / TWO_BAR_HOLD ────────────────
    # "1~2봉짜리 일시 휩쏘"만 골라 청산을 1~2개 완성 3분봉 늦춘다.
    # 무조건 늦추지 않는다 — 아래 **강한 붕괴** 중 하나라도 있으면 즉시 청산한다:
    #   ① MU 반대방향 ENTRY_CONFIRM(롤링)   ② HY 3분 MACD gap 이 반대방향으로 확대
    #   ③ 구조/박스 확정이탈(B3_SL)·hard stop  ④ 반대 플래그가 T+3 확정 + 진입게이트 통과
    # production 청산 규칙은 한 줄도 바꾸지 않는다. 유예 중에도 다른 청산은 그대로 돌고,
    # 유예가 끝나거나 강한 붕괴가 뜨면 **원래 사유 그대로** 집행한다.
    HOLDLOG = []
    DEFER = {"epoch": None, "reason": None, "t0": None, "until": None,
             "net0": None, "bars": 0, "sym": None}

    if _HOLD_BARS:
        from app.trading.macd2 import order_executor as _oexh
        from app.trading.macd2 import mu_assist as _muh
        from app.trading.macd2 import signal_engine as _seh
        from app.trading.macd2 import major_flag_filter as _mffh
        from app.trading.macd2 import time_window_filter as _twfh
        from app.trading.macd2.models import Direction as _Dh
        _o_exec_exit = _oexh.execute_exit
        _HCACHE = {"min": None, "bars": None, "mu": None}

        def _h_bars(now):
            m = now.replace(second=0, microsecond=0)
            if _HCACHE["min"] != m:
                _HCACHE["min"] = m
                _HCACHE["mu"] = None
                try:
                    _HCACHE["bars"] = _mffh._prepare_bars(
                        _seh.resample_completed_3m(svc.get_history_df(), now=now))
                except Exception:
                    _HCACHE["bars"] = None
            return _HCACHE["bars"]

        def _h_mu_opp(now, held):
            """반대방향 MU 롤링 패턴이 CONFIRM 인가 (강한 붕괴 ①)."""
            if _HCACHE["mu"] is None:
                opp = _Dh.DOWN_BLUE if held == _Dh.UP_RED else _Dh.UP_RED
                try:
                    ser = _muh.load_mu_1m(now, config.MU_ASSIST_DATA_DIR or None)
                    anchor = now.replace(second=0, microsecond=0) - timedelta(minutes=5)
                    _HCACHE["mu"] = str(_muh.evaluate_pattern(ser, opp, anchor).get("mu_pattern_state"))
                except Exception:
                    _HCACHE["mu"] = _muh.PATTERN_UNAVAILABLE
            return _HCACHE["mu"] == _muh.PATTERN_CONFIRM

        def _h_gap_opp_expand(now, held):
            """HY 3분 MACD gap 이 **반대방향으로 확대**되는가 (강한 붕괴 ②)."""
            sgn = 1.0 if held == _Dh.UP_RED else -1.0
            try:
                ser = _twfh._gap_series(_h_bars(now))
                if ser is None or len(ser) < 2:
                    return False
                g0 = float(ser["gap"].iloc[-1]) * sgn
                g1 = float(ser["gap"].iloc[-2]) * sgn
                return bool(g0 < 0 and g0 < g1)       # 반대부호 ∧ 더 벌어짐
            except Exception:
                return False

        def _h_opp_entry_approved(held):
            """반대 플래그가 T+3 확정 + 진입게이트 통과했는가 (강한 붕괴 ④)."""
            opp = "DOWN_BLUE" if held == _Dh.UP_RED else "UP_RED"
            return any(v == opp for v in OPP_APPROVED.values())

        def _h_breakdown(now, held, reason):
            if any(k in reason for k in _HOLD_NEVER):
                return "HARD"
            if _h_opp_entry_approved(held):
                return "OPP_ENTRY_APPROVED"
            if _h_gap_opp_expand(now, held):
                return "GAP_OPP_EXPAND"
            if _h_mu_opp(now, held):
                return "MU_OPP_CONFIRM"
            return None

        def _exec_exit_defer(*, broker, symbol, quantity, exit_reason, entry_price, **kw):
            now = clock["now"]
            st = STATE.get("s")
            pos = getattr(st, "position", None) if st is not None else None
            ep = int(getattr(st, "position_epoch", 0) or 0) if st is not None else 0
            reason = str(exit_reason or "")
            held = _Dh.UP_RED if symbol == config.LONG_SYMBOL else _Dh.DOWN_BLUE
            avg = float(entry_price or (pos.avg_price if pos else 0.0))
            bid = (BOOKQ.get(symbol) or (None, None))[0] or px(symbol, now)
            net = worker._net_return_pct(symbol, avg, float(bid), int(quantity)) if bid else None

            if DEFER["epoch"] != ep:
                DEFER.update(epoch=ep, reason=None, t0=None, until=None, net0=None, bars=0, sym=None)

            target = any(k in reason for k in _HOLD_TARGET) and not any(k in reason for k in _HOLD_NEVER)
            if not target:
                return _o_exec_exit(broker=broker, symbol=symbol, quantity=quantity,
                                    exit_reason=exit_reason, entry_price=entry_price, **kw)

            bd = _h_breakdown(now, held, reason)
            if bd:
                HOLDLOG.append(dict(k="BREAKDOWN", t=now.isoformat(), sym=symbol, reason=reason,
                                    why=bd, net=(round(net, 4) if net is not None else None), epoch=ep))
                return _o_exec_exit(broker=broker, symbol=symbol, quantity=quantity,
                                    exit_reason=exit_reason, entry_price=entry_price, **kw)

            if DEFER["reason"] is None:
                b0, b1 = worker.forming_bar_window(now)
                DEFER.update(reason=reason, t0=now.isoformat(), bars=0, sym=symbol,
                             net0=net, until=b1 + timedelta(minutes=3 * (_HOLD_BARS - 1)))
                HOLDLOG.append(dict(k="DEFER", t=now.isoformat(), sym=symbol, reason=reason,
                                    until=DEFER["until"].isoformat(),
                                    net0=(round(net, 4) if net is not None else None), epoch=ep))
                return _oexh.ExecutionOutcome("", _Dh.HOLD, symbol, _SSh.DETECTED,
                                              block_reason="RESEARCH_HOLD_DEFER")
            if now < DEFER["until"]:
                return _oexh.ExecutionOutcome("", _Dh.HOLD, symbol, _SSh.DETECTED,
                                              block_reason="RESEARCH_HOLD_DEFER")
            # 유예창 종료 — 원래 방향을 회복했으면 HOLD(청산 취소), 아니면 원래 사유로 청산
            rec = bool(net is not None and DEFER["net0"] is not None and net > DEFER["net0"])
            HOLDLOG.append(dict(k=("RECOVER" if rec else "EXIT_AFTER_DEFER"), t=now.isoformat(),
                                sym=symbol, reason=reason,
                                net0=(round(DEFER["net0"], 4) if DEFER["net0"] is not None else None),
                                net=(round(net, 4) if net is not None else None), epoch=ep))
            DEFER.update(reason=None, t0=None, until=None, net0=None, sym=None)
            if rec:
                return _oexh.ExecutionOutcome("", _Dh.HOLD, symbol, _SSh.DETECTED,
                                              block_reason="RESEARCH_HOLD_RECOVERED")
            return _o_exec_exit(broker=broker, symbol=symbol, quantity=quantity,
                                exit_reason=exit_reason, entry_price=entry_price, **kw)

        from app.trading.macd2.models import SignalState as _SSh
        monkeypatch.setattr(_oexh, "execute_exit", _exec_exit_defer)
'''

PATCHES = [
    # ① 상수 블록은 _d() 정의 바로 앞 (timedelta / os / pd / Path 가 이미 import 된 지점)
    ("def _d(s):", HEAD_CONST.strip() + "\n\n\n" + "def _d(s):"),
    # ② MU/체결강도 재생 피드
    ('    monkeypatch.setattr(worker, "_git_sha", lambda: "replay")\n', MU_FEED),
    # ③ ETF 시세를 실제 1초 틱으로
    ("    def _px_raw(sym, now):\n        d = etf[sym]\n", TICK_PX),
    # ③-b 하이닉스 E 감시가도 실제 1초 틱으로
    ("    def _hy_px(now):\n        ix = idx.get(config.WATCH_SYMBOL)\n",
     '    def _hy_px(now):\n'
     '        if _TICKPX:\n'
     '            _r = _tick_row(config.WATCH_SYMBOL, now)\n'
     '            if _r is not None:\n'
     '                return float(_r["price"])\n'
     '        ix = idx.get(config.WATCH_SYMBOL)\n'),
    # ③-c 호가 헬퍼 + 브로커 호가 격자
    ("    svc.refresh_quotes = lambda *a, **k: None\n",
     "    svc.refresh_quotes = lambda *a, **k: None\n" + BOOK),
    # ③-d 루프에서 브로커 호가를 실제 최우선호가로
    ("        for sym in (config.LONG_SYMBOL, config.INVERSE_SYMBOL):\n"
     "            p = px(sym, t + _FILL_LAG)\n"
     "            if p is not None:\n"
     "                broker.set_quote(sym, p)\n",
     "        for sym in (config.LONG_SYMBOL, config.INVERSE_SYMBOL):\n"
     "            p = _set_book(sym, t + _FILL_LAG)\n"
     "            if p is not None:\n"
     "                broker.set_quote(sym, p)\n"),
    # ③-e 브로커 매수호가 = 실제 ask1 (없으면 기존 시세+1틱)
    ('    def _ask(self, symbol):\n'
     '        q = self._quotes.get(symbol)\n'
     '        return None if q is None else float(q) + ETF_TICK\n',
     '    #: 실제 1초 스냅샷 최우선호가가 있으면 그것을 쓴다 (ARM3 하네스가 채운다).\n'
     '    book = None\n\n'
     '    def _ask(self, symbol):\n'
     '        bk = (self.book or {}).get(symbol) if self.book is not None else None\n'
     '        if bk and bk[1]:\n'
     '            return float(bk[1])\n'
     '        q = self._quotes.get(symbol)\n'
     '        return None if q is None else float(q) + ETF_TICK\n'),
    # ④ 모드/토글 env 분기
    ('    if VAR in ("EPROD", "ECHOP", "ECHOPB", "ECHOPC", "PAR", "PAR70", "PAR50"):\n', MODE_BLOCK),
    # ⑤ 게이트 추적 + ARM3
    ('    STATE["s"] = state\n', ARM_BLOCK),
    # ⑦ P3-TREND-BYPASS 를 env 로도 (CHOP 진입 + N1 runner 승격 연구)
    ('            if VAR == "E" and regime == _cr.REGIME_CHOP and snap.ok:',
     '            if (VAR == "E" or _TRBP) and regime == _cr.REGIME_CHOP and snap.ok:'),
    # ⑥ 산출에 새 기록 추가
    ("p70r=P70RLOG, par=dict(",
     "gates=GATELOG, arm3=ARM3LOG, hold=HOLDLOG, hold_kind=_HOLD, vwov=VWOVLOG, vwov_lim=_VWOV, trail_arm=_TARM, trend_bypass=bool(_TRBP), "
     "arm=dict(kind=_ARM, mode=_MODE, mua=bool(_MUA), xs=bool(_XS), cukill=bool(_CUKILL), tickpx=bool(_TICKPX)), "
     "p70r=P70RLOG, par=dict("),
]

#: 브로커에 호가 격자를 물린다 (RealFillBroker.book ← BOOKQ).
BIND_BOOK = ("    broker = Broker(cash=50_000_000.0, quotes={config.LONG_SYMBOL: 10_000.0, config.INVERSE_SYMBOL: 6_000.0})\n",
             "    broker = Broker(cash=50_000_000.0, quotes={config.LONG_SYMBOL: 10_000.0, config.INVERSE_SYMBOL: 6_000.0})\n"
             "    broker.book = BOOKQ\n"
             "    if _TICKPX:\n"
             "        _o_sell_mkt = broker.sell_market\n\n"
             "        def _sell_mkt(symbol, qty, client_order_id):\n"
             '            """매도 체결가 = 실제 최우선 매수호가(bid1). 없으면 기존 모형(시세)."""\n'
             "            bk = BOOKQ.get(symbol)\n"
             "            if bk and bk[0]:\n"
             "                broker.set_quote(symbol, float(bk[0]))\n"
             "            return _o_sell_mkt(symbol, qty, client_order_id)\n"
             "        broker.sell_market = _sell_mkt\n")


def main() -> None:
    text = SRC.read_text(encoding="utf-8")
    for old, new in PATCHES + [BIND_BOOK]:
        n = text.count(old)
        if n != 1:
            raise SystemExit(f"!! 패치 대상이 {n}개 (1개여야 함): {old[:80]!r}")
        text = text.replace(old, new, 1)
    head = ('"""[2026-10-30 ARM3(+3% 틱 보호선) · MU Assist/XS · 실틱 재생 연구 사본]\\n'
            '원본: research_20261029_n1_morning_stop_p70r/harness/test_zz_par.py\\n'
            'production 무수정. 덧붙인 것만 있다 — 자세한 내용은 scripts/h0_make.py 참조.\\n\\n'
            '── 이하 원본 docstring ──\\n')
    text = head + text[len('"""'):]
    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_text(text, encoding="utf-8")
    print(f"-> {DST}  ({len(text):,}자, 패치 {len(PATCHES) + 1}건)")


if __name__ == "__main__":
    main()
