"""MACD2 configuration — docs/MACD2_LOGIC.md confirmed defaults.

Strategy-fixed values (never overridden per-request; changing them is a
requirements change, not a runtime setting) are the module-level constants
below. Values the UI/user may change at runtime (mode, budget) are read from
RuntimeState, not from here — this module only supplies their defaults.
"""
from __future__ import annotations

import os
from datetime import time, timedelta, timezone

# KST is a fixed UTC+9 offset with no DST — safe as a plain timezone constant.
KST = timezone(timedelta(hours=9))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default

# ── Symbols (strategy-fixed) ────────────────────────────────────────────────
WATCH_SYMBOL = "000660"  # SK하이닉스 — signal source only, never traded directly
LONG_SYMBOL = "0193T0"  # KODEX 레버리지 — bought on UP_RED
INVERSE_SYMBOL = "0197X0"  # SOL 인버스2X — bought on DOWN_BLUE
TRADE_SYMBOLS = (LONG_SYMBOL, INVERSE_SYMBOL)

# ── Budget (UI-overridable; this is only the default) ──────────────────────
DEFAULT_BUDGET = 10_000_000.0

# 2026-08-14: user decision to run MU_MACD only for now -- MACD2 auto-trading
# was hard-disabled here (start()/_auto_recover_worker() both refused
# immediately, regardless of any stale persisted state.auto_trade_on=True
# left over from before this flag existed) so a redeploy/idle-sleep restart
# could never silently bring it back.
#
# 2026-08-15: user decision to reactivate MACD2 (alongside today's 시간대별
# 최적거래 필터 button/toggle). MACD2 and MU_MACD trade the identical two
# ETFs in the same KIS account, so app.trading.strategy_ownership now also
# arbitrates between them directly (MU_MACD added as a third claimant,
# 2026-08-15) -- see other_strategy_active() below and
# app/trading/mu_macd/service.py's own matching gate. Set
# MACD2_AUTO_TRADE_HARD_DISABLED=true to hard-disable again if needed.
AUTO_TRADE_HARD_DISABLED = _env_bool("MACD2_AUTO_TRADE_HARD_DISABLED", False)

STRATEGY_NAME = "MACD2"
# 2026-07-27 KIS-parity fix: order authority moved OFF the forming/provisional
# bar and onto the confirmed, completed-3m-bar MACD(12,26,9) crossover — the
# same thing KIS itself charts a flag on. SIGNAL_RULE is now that confirmed
# rule; CONFIRMED_SIGNAL_RULE is kept as an alias (same value) since it is
# still referenced by the UI/tests under its original name. The forming bar
# and Signed-B remain shadow/candidate-only display (PROVISIONAL_SHADOW_RULE),
# never written to the signal ledger and never given order/stat authority.
STRATEGY_VERSION = "20260731_KIS_MACD_COLOR_FLAG_V1"
SIGNAL_RULE = "KIS_MACD_COLOR_FLAG_CONFIRMED"
CONFIRMED_SIGNAL_RULE = SIGNAL_RULE
PROVISIONAL_SHADOW_RULE = "MACD_FORMING_CANDIDATE_SHADOW"
LEGACY_SIGNAL_RULE = "SIGNED_B_LEGACY"

# Order-sizing safety margin (docs §9: "수수료·호가 변동을 고려한 안전 여유") is no
# longer a fixed placeholder ratio here — docs/MACD2_LOGIC.md §21 flagged the old
# ORDER_SAFETY_MARGIN_PCT=0.5 constant as an unconfirmed placeholder. It is now
# computed per-order from real inputs (buy fee rate from config.yaml
# trading_cost + KRX tick size for the order price) by
# order_executor.compute_order_safety_margin_pct(); see that function's
# docstring and docs/MACD2_LOGIC.md §9/§21 for the rationale.

# ── MACD (strategy-fixed) ───────────────────────────────────────────────────
EMA_FAST = 12
EMA_SLOW = 26
EMA_SIGNAL = 9
# Old A-F `signals_B`: first eligible bar index is 26 → len(bars) must be > 26.
SIGNAL_MIN_BAR_INDEX = 26

# ── Warm-up (strategy-fixed) ────────────────────────────────────────────────
WARMUP_3M_BARS_MIN = 100
WARMUP_1M_BARS_MIN = WARMUP_3M_BARS_MIN * 3  # >=300

# ── Risk / exit (strategy-fixed) ────────────────────────────────────────────
STOP_LOSS_NET_PCT = -1.5
# 2026-08-05: PROFIT_LOCK_ACTIVATE_NET_PCT/GIVEBACK_PP/EXIT_ENABLED and
# EXIT_PROFIT_LOCK below are the OLD net-return-giveback Profit Lock —
# risk_exit.py's own update_profit_lock_tracker()/evaluate_position_exits()
# still define/test it as a pure function, but worker.py's live tick no
# longer calls it at all (replaced by the MACD-convergence Profit Lock
# further down — see PROFIT_LOCK_DEFAULT_ENABLED et al.). Left in place only
# for risk_exit.py's own existing unit tests; never reachable from a live run.
PROFIT_LOCK_ACTIVATE_NET_PCT = 1.5
PROFIT_LOCK_GIVEBACK_PP = 0.8
PROFIT_LOCK_EXIT_ENABLED = False

EXIT_STOP_LOSS = "STOP_LOSS"
EXIT_PROFIT_LOCK = "PROFIT_LOCK"
EXIT_OPPOSITE_SIGNAL = "OPPOSITE_SIGNAL"
EXIT_FORCED_LIQUIDATION = "FORCED_LIQUIDATION"
EXIT_USER_LIQUIDATION = "USER_LIQUIDATION"  # UI "자동매매 중지 및 일괄매도" 버튼
EXIT_MANUAL_LIQUIDATION = "MANUAL_LIQUIDATION"  # UI "수동 전량매도" 버튼 (자동매매는 계속 유지)

# ── Profit Lock — MACD convergence early exit (2026-08-05 spec) ───────────
# EXIT LOGIC ONLY (never affects entries/MAJOR filter/Stop Loss/forced
# liquidation/opposite-flag switching — docs §10 priority: FORCED_LIQUIDATION
# > STOP_LOSS > OPPOSITE_SIGNAL > PROFIT_LOCK_MACD_CONVERGENCE > QUICK_PROFIT).
# 2026-08-05 (사용자 요청 — 모든 필터 기본값 OFF): Default OFF; mutually
# exclusive with QUICK_PROFIT_FILTER_DEFAULT's toggle — the UI/service block
# turning either on while the other is already on.
# Evaluated once per newly-completed WATCH_SYMBOL(000660) 3-minute bar while a
# position is held, off the SAME confirmed MACD(12,26,9)/Signal already
# computed for flag generation (never a second MACD calc, never the forming
# bar). All 5 conditions below must hold on that bar for the full-quantity
# exit to fire:
#   1) actual ETF net return (TradeCostEngine basis, same as STOP_LOSS_NET_PCT)
#      >= PROFIT_LOCK_MIN_NET_RETURN_PCT
#   2) >= PROFIT_LOCK_MIN_BARS_SINCE_ENTRY completed WATCH_SYMBOL bars have
#      elapsed since entry (the entry bar itself never counts)
#   3) the held-direction support_gap has contracted for
#      PROFIT_LOCK_MIN_CONSECUTIVE_CONTRACTIONS consecutive completed bars
#      (0193T0 held: support_gap = MACD - Signal; 0197X0 held: support_gap =
#      Signal - MACD — a support_gap <= 0 defers entirely to the
#      OPPOSITE_SIGNAL priority above instead)
#   4) current support_gap / max support_gap since entry <=
#      PROFIT_LOCK_MAX_GAP_RATIO
#   5) actual ETF return has given back >= PROFIT_LOCK_MIN_DRAWDOWN_PP
#      percentage points from its peak since entry
PROFIT_LOCK_DEFAULT_ENABLED = _env_bool("MACD2_PROFIT_LOCK_DEFAULT_ENABLED", False)
PROFIT_LOCK_MIN_NET_RETURN_PCT = 1.0
PROFIT_LOCK_MIN_BARS_SINCE_ENTRY = 3
PROFIT_LOCK_MIN_CONSECUTIVE_CONTRACTIONS = 2
PROFIT_LOCK_MAX_GAP_RATIO = 0.25
PROFIT_LOCK_MIN_DRAWDOWN_PP = 0.25
EXIT_PROFIT_LOCK_MACD_CONVERGENCE = "PROFIT_LOCK_MACD_CONVERGENCE"

# ── Session timing (strategy-fixed, KST) ───────────────────────────────────
SESSION_OPEN = time(9, 0)
NEW_ENTRY_CUTOFF = time(14, 55)
FORCE_LIQUIDATE_AT = time(15, 0)

# ── 09:03 예약 매수 (2026-08-06) — 개장 직후에는 아직 데이터가 부족해 MACD가
# 이른 시간대 플래그를 잘 못 잡는 문제 대응용 사용자 예약 매수. 오늘 이 시각
# 이후 첫 tick에 자동 발동, SCHEDULED_ENTRY_FIRE_WINDOW_SEC 안에 체결되지
# 못하면 그날은 놓친 것으로 처리(다음날 다시 예약해야 함).
SCHEDULED_ENTRY_TIME = time(9, 3)
SCHEDULED_ENTRY_FIRE_WINDOW_SEC = 180.0

# 2026-08-07 (사용자 요청 — 실제 사고: 예약매수 후 09:20에야 진짜 플래그로 체결됨.
# 근본 원인은 arm_scheduled_entry가 run_once 밖에서 state를 직접 저장해, 예약
# 이후 첫 tick의 _apply_day_rollover가 "어제 값"으로 오인해 armed_direction을
# 지워버리는 경쟁 상태였음 — 그 부분은 _apply_day_rollover 자체를 고쳐 해결).
# 이 상수는 별개의 추가 보호 기능: 예약매수가 실제 체결된 뒤, 개장 직후 MACD가
# 아직 불안정해 반대 방향 확정 플래그가 바로 뜨더라도(진짜 반전이 아니라 노이즈일
# 가능성이 높음) 이 시각까지는 청산하지 않고 보유를 유지한다. 이 시각 이후로는
# 같은 방향 플래그는 그대로 보유, 반대 방향 플래그는 기존 반대신호청산 규칙을
# 정상 적용한다. STOP_LOSS/PROFIT_LOCK/QUICK_PROFIT/15:00 강제청산은 이 보호와
# 무관하게 항상 그대로 작동한다(오직 확정 반대 플래그로 인한 청산/스위치만 보호).
SCHEDULED_ENTRY_PROTECTION_UNTIL = time(9, 10)
SCHEDULED_ENTRY_PROTECTION_ACTIVE = "SCHEDULED_ENTRY_PROTECTION_ACTIVE"

# ── PRE15+TW 프리마켓 승계 (2026-08-24, 사용자 요청 — 60영업일 백테스트로
# 검증: scripts/premarket_carryover_backtest.py의 run_pre15_tw, 기존 TW2
# baseline 대비 총수익 +12.7%p/복리 +26.5%p 개선, MDD 변화 없음). 08:45:00
# -08:59:59에 확정된 마지막 RED/BLUE 플래그가 SCHEDULED_ENTRY_TIME(09:03)까지
# 반대 플래그 없이 유지되면, TW2의 evaluate_time_window_entry/
# evaluate_tw2_extra_vetoes(품질점수/VWAP/과다교차 veto)를 전혀 거치지 않고
# 09:03에 즉시 진입한다 -- 검증된 백테스트 조건 그대로, 별도 quality 조건을
# 추가하지 않는다(사용자 명시 요청). TW2 전용(time_window_2_filter_enabled),
# TW1에는 적용하지 않는다. 09:00~09:03 사이의 취소 판정 및 발동 시각은 기존
# SCHEDULED_ENTRY_TIME/FIRE_WINDOW_SEC을 그대로 재사용한다(별도 상수 불필요).
PREMARKET_CARRY_WINDOW_START = time(8, 45)

# ── Worker (strategy-fixed) ─────────────────────────────────────────────────
WORKER_INTERVAL_SEC = 5.0
WORKER_TICK_MEAN_MAX_SEC = 5.5
WORKER_TICK_P95_MAX_SEC = 7.0
WORKER_TICK_MAX_SEC = 10.0
SIGNAL_TO_ORDER_REQUEST_MAX_SEC = 5.0
WORKER_STALL_AGE_SEC = 15.0
# 2026-08-20 fix: the quote-updater background thread (market_data.py) can
# get stuck inside a single hanging refresh_quotes() call forever (e.g. a
# KIS call that never raises, never returns) while still reporting
# is_alive()=True -- the exact same "alive but not producing" failure mode
# already fixed for the main Worker thread's start(), just for this OTHER
# background thread. quote_updater_alive() alone cannot detect this; see
# Macd2Worker._run_loop's staleness-based force-restart.
QUOTE_UPDATER_STALL_AGE_SEC = 30.0

# 2026-08-24 fix (real incident: Render memory 20%->60% over ~2h under
# sustained KIS mock-mode rate limiting): forcing a stop+restart every single
# QUOTE_UPDATER_STALL_AGE_SEC (30s) without ever confirming the OLD thread
# actually died orphaned a new thread on top of the old one every cycle for
# as long as contention lasted -- a single KIS retry chain can legitimately
# take up to ~120s under sustained rate limiting (see
# kis_client._get_with_rate_limit_retry: 8 attempts x 5s mock delay per
# symbol), well past both the 30s cooldown and the join_timeout used to stop
# it. Below this age, the worker now only replaces the thread once
# MarketDataService.stop_quote_updater() confirms it actually died --
# otherwise it just waits for the next cycle. Only once staleness exceeds
# THIS much longer bound (comfortably past any plausible legitimate retry
# chain) does it force a replacement regardless of confirmed-stop, accepting
# one orphan as the lesser evil -- preserving the 2026-08-20 fix's guarantee
# that a genuinely permanently-hung thread (e.g. a KIS call that never
# returns at all) is still eventually replaced instead of freezing quotes
# forever.
QUOTE_UPDATER_FORCE_REPLACE_AGE_SEC = 300.0

# 2026-08-04 fix: a fresh process (Render free-tier idle-sleep, redeploy, or
# crash — docs/deploy_render.md: ephemeral filesystem, in-process Worker
# singleton) previously left auto_trade_on=True permanently WORKER_STALLED
# with no automatic recovery — 0 flags/orders for however long nobody
# noticed and clicked "자동매매 시작" again. get_snapshot() now retries
# start() automatically (MOCK mode only — see service._auto_recover_worker)
# whenever it finds auto_trade_on=True but no live worker thread, at most
# once per this cooldown so a persistently-failing bootstrap does not
# hammer KIS on every UI auto-refresh tick.
WORKER_AUTO_RECOVER_COOLDOWN_SEC = 30.0

# ── Market data validity (strategy-fixed) ──────────────────────────────────
QUOTE_MAX_AGE_SEC = 10.0

# 2026-08-24 fix (real incident: LONG/INVERSE ETF quotes -- the two symbols
# order dispatch actually depends on -- sat 16-19s stale all morning, once
# 81 minutes late). Root cause: KIS mock mode's single process-wide
# ~1-req/1.1s throttle (kis_client._throttle) is shared by every mock-mode
# bot in this process (MU_MACD auto_trade_on=True the same morning), not
# just MACD2. WATCH_SYMBOL(000660) was consuming 1 of every 3 quote-updater
# calls for a value that (a) order dispatch never reads (see worker.py
# _required_quote_symbols) and (b) doesn't need sub-10s freshness -- it's a
# diagnostic display price only. Fetching it far less often frees up that
# share of the shared mock-mode budget for the two symbols that actually
# gate orders.
WATCH_SYMBOL_QUOTE_REFRESH_EVERY_N_CYCLES = 8
PENDING_SIGNAL_RETRY_SEC = 30.0
FLAT_POSITION_RECONCILE_INTERVAL_SEC = 30.0

# 2026-07-27 QUOTE_STALE 처리 수정: confirmed 신호가 quote stale로 막히면
# 그 자리에서(같은 tick 안에서) 강제 재조회 후 최대 이 횟수만큼, 이 간격으로
# 재검증한다. 신호 확정(detected_at) 후 이 시간을 넘기면 더 이상 뒤늦게
# 주문하지 않고 MISSED_SIGNAL_QUOTE_STALE로 종료·기록한다.
QUOTE_STALE_RETRY_MAX_ATTEMPTS = 3
QUOTE_STALE_RETRY_INTERVAL_SEC = 1.0
QUOTE_STALE_MAX_WAIT_SEC = 15.0
MISSED_SIGNAL_QUOTE_STALE = "MISSED_SIGNAL_QUOTE_STALE"

# 2026-07-27 momentary-crossing fix: a single-tick provisional forming-bar
# crossover is only a CANDIDATE, never an order — it is confirmed as a
# Primary onset only once the SAME direction is still present on a LATER
# fresh quote tick at least this many seconds after the first sighting.
# (candidate/shadow display only since the 2026-07-27 KIS-parity fix — never
# order/stat authority any more.)
PROVISIONAL_CONFIRM_MIN_GAP_SEC = 0.0

# 주문 성공 응답만으로 체결로 간주하지 않고, 주문번호로 실제 체결/잔고를
# 재조회해 확인하는 최대 대기시간·간격 (docs 2026-07-27 체결확인 fix).
ORDER_FILL_POLL_MAX_SEC = 60.0
ORDER_FILL_POLL_INTERVAL_SEC = 1.0

# KIS 1분봉(history)과 실시간 quote의 단위·시각 불일치 감지 허용범위 (docs
# 2026-07-27 fix) — 정상 범위를 벗어나면 주문을 차단한다. 10배/0.1배 스케일
# 오차는 market_data._normalize_quote_price()가 이미 보정하므로, 여기서는
# 그 보정 이후에도 설명되지 않는 큰 괴리만 잡아낸다.
QUOTE_HISTORY_PRICE_RATIO_MIN = 0.5
QUOTE_HISTORY_PRICE_RATIO_MAX = 2.0
# 정규장 중 1분봉 history의 최신 시각이 이보다 오래되면(당일 데이터가 갱신되지
# 않는 상태) 시각 불일치로 간주한다.
HISTORY_STALE_MAX_SEC = 180.0

# 전일 warm-up 조회(주식일별분봉조회) 중 KIS 서버 일시 오류(500 등)를 "해당
# 날짜에 데이터 없음(휴장일)"으로 오인해 더 이전 날짜로 잘못 넘어가면 EMA
# seed가 실제 KIS 차트와 달라진다 (2026-07-27 3플래그 재현 검증에서 발견 —
# 정상 거래일 20260724 조회가 500으로 실패해 20260723으로 잘못 대체됨). 응답이
# 명시적 오류를 동반한 빈 결과일 때만 재시도하고, 오류 없는 빈 결과(진짜 휴장일)
# 는 즉시 다음 날짜로 넘어간다.
PRIOR_DAY_FETCH_RETRIES = 5
PRIOR_DAY_FETCH_RETRY_DELAY_SEC = 2.0

# 2026-08-10 fix: a page whose retries are ALL exhausted on a genuine error
# (not a legitimate empty/no-more-data response) now backs off past that one
# stuck hour1 boundary and keeps walking, instead of silently truncating the
# rest of the day (real incident: 000660's inquire-time-itemchartprice
# intermittently 500s at one specific early-morning boundary while other
# symbols' requests succeed, permanently amputating everything earlier than
# that boundary for the whole session). Capped separately from KIS_MAX_PAGES
# so a genuinely fully-down endpoint still gives up quickly rather than
# burning the entire page budget in retries.
MAX_CONSECUTIVE_PAGE_ERROR_SKIPS = 2

# 백워드 페이징(주식일별분봉조회/inquire-time-itemchartprice)에서 연속 요청을
# 텀 없이 쏘면 KIS 초당 거래건수 제한에 걸려 일부 페이지가 오류 없이 조용히
# 빈 결과로 돌아온다 (2026-07-27 발견 — page당 실수신 30건인데 지연 없이
# 여러 페이지를 연속 요청하면 중간 페이지가 누락됨). 페이지 사이에 짧은
# 페이싱을 둔다.
KIS_PAGE_FETCH_PACING_SEC = 0.4

# ── NXT(넥스트레이드) 통합 체결가 연속 시계열 (2026-08-20) ──────────────────
# KIS 실제 차트는 정규장(09:00-15:30, KRX 단독)만이 아니라 NXT 거래소를 포함한
# 통합 체결가로 MACD를 계산한다(차트 좌측 시간축에 전일 저녁 데이터가 표시됨).
# FID_COND_MRKT_DIV_CODE="NX"는 이 통합 체결가를 08:00(프리마켓)~20:00(시간외)
# 범위로 반환하며, 정규장 구간도 KRX 단독(J)과 다른 실제 값을 낼 수 있음이
# 실측으로 확인됐다(08/20 09:01 J=1,612,500 vs NX=1,612,000 등 31건 중 11건
# 불일치) — 즉 NX는 J의 상위집합이 아니라 KIS 차트가 실제로 쓰는 별도의
# 통합 소스이므로, 병합 우선순위 없이 NX 하나를 하이닉스(WATCH_SYMBOL) 1분봉의
# 유일한 소스로 사용한다.
NXT_MARKET_DIV_CODE = "NX"

# ── Feature flags (strategy-fixed per docs; not user-configurable) ────────
CONTINUATION_REENTRY_ENABLED = False
OPENING_PROBE_ENABLED = False

# ── Optional Hybrid MAJOR_FLAG filter (order gate only; confirmed flags unchanged) ──
# 2026-08-05 (사용자 요청 — 모든 필터 기본값 OFF): UI toggle defaults OFF. Env
# MACD2_MAJOR_FILTER_DEFAULT may override the cold-start default; runtime
# state / UI command still wins after start.
MAJOR_FILTER_VERSION = "MAJOR_FILTER_HYBRID_V6_JULY_FREQ_PROFIT"
MAJOR_FILTER_DEFAULT = _env_bool("MACD2_MAJOR_FILTER_DEFAULT", False)

MAJOR_ENTRY_SCORE_MIN = _env_float("MACD2_MAJOR_ENTRY_SCORE_MIN", 65.0)
MAJOR_REVERSAL_SCORE_MIN = _env_float("MACD2_MAJOR_REVERSAL_SCORE_MIN", 75.0)
MAJOR_FAST_REVERSAL_SCORE_MIN = _env_float("MACD2_MAJOR_FAST_REVERSAL_SCORE_MIN", 82.0)
MAJOR_STRONG_START = time(10, 30)

# Hybrid component tiers (Hybrid V1 — order gate only)
MAJOR_HIST_IMPULSE_T1 = 0.10  # 10 pts
MAJOR_HIST_IMPULSE_T2 = 0.15  # 18 pts
MAJOR_HIST_IMPULSE_T3 = 0.22  # 25 pts
MAJOR_PRICE_IMPULSE_T1 = 0.35  # 15 pts (also price-confirm floor)
MAJOR_PRICE_IMPULSE_T2 = 0.55  # 25 pts
MAJOR_BODY_ATR_T1 = 0.25  # 5 pts
MAJOR_BODY_ATR_T2 = 0.40  # 10 pts
MAJOR_VOLUME_RATIO_T1 = 1.00  # 5 pts
MAJOR_VOLUME_RATIO_T2 = 1.10  # 10 pts
MAJOR_VOLUME_RATIO_T3 = 1.20  # 15 pts
# Legacy single-threshold aliases (tests / older docs may still reference)
MAJOR_HIST_IMPULSE_ATR_MIN = MAJOR_HIST_IMPULSE_T3
MAJOR_PRICE_IMPULSE_ATR_MIN = MAJOR_PRICE_IMPULSE_T1
MAJOR_BODY_ATR_MIN = MAJOR_BODY_ATR_T2
MAJOR_VOLUME_RATIO_MIN = MAJOR_VOLUME_RATIO_T3
MAJOR_SIDEWAYS_EMA_SPREAD_MAX = 0.0007
MAJOR_SIDEWAYS_RANGE_MAX = 0.006
MAJOR_RANGE_BREAKOUT_LOOKBACK = 4
MAJOR_RECENT_RANGE_LOOKBACK = 8
MAJOR_VOLUME_LOOKBACK = 20
MAJOR_ATR_PERIOD = 14
MAJOR_EMA_FAST = 10
MAJOR_EMA_SLOW = 20
MAJOR_MIN_COMPLETED_BARS = 26

MAJOR_MAX_DAILY_ENTRIES = _env_int("MACD2_MAJOR_MAX_DAILY_ENTRIES", 4)
MAJOR_MIN_HOLD_MIN = _env_int("MACD2_MAJOR_MIN_HOLD_MIN", 9)
MAJOR_FAST_REVERSAL_WINDOW_MIN = _env_int("MACD2_MAJOR_FAST_REVERSAL_WINDOW_MIN", 15)
MAJOR_SAME_DIRECTION_REENTRY_MIN = _env_int("MACD2_MAJOR_SAME_DIRECTION_REENTRY_MIN", 18)

# Ledger / UI decision labels (filter gate only — not strategy_version)
MAJOR_APPROVED = "MAJOR_APPROVED"
MAJOR_SCORE_BELOW_THRESHOLD = "MAJOR_SCORE_BELOW_THRESHOLD"
MAJOR_PRICE_CONFIRMATION_FAILED = "MAJOR_PRICE_CONFIRMATION_FAILED"
MAJOR_SIDEWAYS_BLOCK = "MAJOR_SIDEWAYS_BLOCK"
MAJOR_STRONG_PROFILE_FAILED = "MAJOR_STRONG_PROFILE_FAILED"
FILTER_DATA_INSUFFICIENT = "FILTER_DATA_INSUFFICIENT"
FILTER_INPUT_NOT_CROSSOVER = "FILTER_INPUT_NOT_CROSSOVER"
SAME_DIRECTION_POSITION_HELD = "SAME_DIRECTION_POSITION_HELD"
MAJOR_DAILY_ENTRY_LIMIT = "MAJOR_DAILY_ENTRY_LIMIT"
MAJOR_SAME_DIRECTION_COOLDOWN = "MAJOR_SAME_DIRECTION_COOLDOWN"
MAJOR_MIN_HOLD_BLOCK = "MAJOR_MIN_HOLD_BLOCK"
FILTERED_OUT = "FILTERED_OUT"

# ── Optional 추세전환장(sideways/whipsaw) entry filter — order gate only ────
# 2026-08-04 v2 (tight): re-derived from the last 20 real trading days.
# Classified each day by confirmed-flag count (natural gap at 3 vs 5+) into
# 13 "확실한 추세" days (<=3 flags/day, mostly profitable even unfiltered)
# and 7 "추세전환장" days (>=5 flags/day: 07/15,16,20,21,22,23,08/03; often
# choppy/whipsaw and prone to big losses when every flag trades). Pooling
# all 55 real trades from just those 7 days (all-entries baseline + the
# Quick-Profit +1.5% take-profit exit) showed the INVERSE of the v1
# relationship: LOW major_flag_filter score predicted the winners on these
# choppy days, not high score (e.g. score 30-45 netted +1.08M across 11
# trades, score 60-90 netted -850K across 26 trades). Requiring
# breakout==False on top removed one more clean outlier loss for free
# (zero winners cost). Final: score < SIDEWAYS_ENTRY_SCORE_MAX AND
# breakout == False -> 16/55 trades kept (~2.3/day), 12W/4L (75% win
# rate), net +1.81M vs +0.81M unfiltered — the "타이트한" variant (fewer
# trades than the 3-4/day target, prioritizing win quality). Still reuses
# the SAME hist/price-impulse/body/volume/EMA/breakout metrics MAJOR_FLAG
# already computes (major_flag_filter.compute_component_scores/
# score_for_direction) — only the threshold combination is new. Off by
# default (opt-in toggle). When ON it takes priority over
# major_filter_enabled — the two gates are never both active at once
# (worker._judge_entry_gate).
#
# 2026-08-07 v3 (time-aware): re-validated on an expanded 10-day 추세전환장
# set (added 06/24, 08/04, 08/05 to the 7 above) by bucketing every
# confirmed flag's outcome into 09:00-11:00 / 11:00-14:00 / 14:00-15:30.
# The score<45-and-not-breakout gate still wins net P&L INSIDE 11:00-14:00
# (that bucket alone: win_rate 50%, mean score of winners 44.2 vs losers
# 58.8 — the inverted low-score-wins relationship above is tightest here),
# but a full tick-by-tick replay of all 10 days showed removing the gate
# entirely OUTSIDE that window (every confirmed flag enters in 09:00-11:00
# and 14:00-15:30; 11:00-14:00 unchanged) beats BOTH the score<45 gate
# applied all day (avg net/day +291,071) and a "require HIGH score outside
# 11:00-14:00" variant (+85,348 — the naive "확실한 추세엔 강한 플래그"
# idea; REJECTED because the low-score-wins relationship is not actually
# 11:00-14:00-specific, so requiring a high score outside it just selects
# worse trades). The no-gate-outside-window variant nets +317,978/day at
# ~4 trades/day vs ~2/day for the other two.
#
# 2026-08-07 v5 (사용자 요청 — 시간대별 로직 재설계): replayed 4 candidate
# entry-gate designs tick-by-tick through the REAL worker.run_once() over
# the most recent real trading week (08/03-08/07, Fri partial to ~14:58):
# (A) no filter at all, (B) the v3/v4 design just above (unconditional
# outside 11:00-14:00 + PRIMARY_TREND pullback check all day), (C) 09:00-
# 11:00 PRIMARY_TREND-pullback-only + the SAME score<45-and-not-breakout
# gate extended from 11:00 all the way through end of day (no more
# unconditional-outside-window branch at all), (D) same as C but with an
# even stricter 14:00+ threshold (score<30). Results: A=+2.95% cum
# (36% win rate, 89 trades), B=+12.59% (57%, 29 trades), C=+13.87% (67%,
# 24 trades, ZERO days left an open position at cutoff), D=+12.61% (64%,
# 22 trades) — D's extra afternoon strictness bought nothing over C, so
# (C) is adopted: PRIMARY_TREND pullback is now checked ONLY in the
# 09:00-11:00 window (not all day like v3/v4), and every confirmed flag at
# or after SIDEWAYS_TIME_GATE_START gets the unchanged score<45-and-not-
# breakout gate with NO unconditional-approval window anymore (there is no
# more "outside the gate" case in the score-based sense — 14:00-15:30 is
# now gated the same as 11:00-14:00 always was). See
# sideways_filter.evaluate_sideways_flag's docstring for the exact branch
# logic. Sample caveat: only 5 real trading days (~48 confirmed flags total)
# backed this comparison — re-validate after a few more weeks of live data.
SIDEWAYS_FILTER_DEFAULT = _env_bool("MACD2_SIDEWAYS_FILTER_DEFAULT", False)
SIDEWAYS_FILTER_VERSION = "SIDEWAYS_FILTER_V5_MORNING_TREND_ALLDAY_SCORE_GATE_20260807"
SIDEWAYS_ENTRY_SCORE_MAX = _env_float("MACD2_SIDEWAYS_ENTRY_SCORE_MAX", 45.0)
# 09:00-11:00 (morning, before this): PRIMARY_TREND-pullback-only gate.
# At/after this time (11:00 through end of day): the score+breakout gate.
# There is no longer a separate end boundary -- the score gate now runs
# all the way to NEW_ENTRY_CUTOFF (14:55), which already caps real entries.
SIDEWAYS_TIME_GATE_START = time(11, 0)

SIDEWAYS_APPROVED = "SIDEWAYS_APPROVED"
SIDEWAYS_SCORE_ABOVE_THRESHOLD = "SIDEWAYS_SCORE_ABOVE_THRESHOLD"
SIDEWAYS_BREAKOUT_BLOCKED = "SIDEWAYS_BREAKOUT_BLOCKED"
# 09:00-11:00 approval -- either the flag AGREES with today's PRIMARY_TREND,
# or PRIMARY_TREND is still RANGE (not enough votes yet to call a trend).
SIDEWAYS_MORNING_TREND_APPROVED = "SIDEWAYS_MORNING_TREND_APPROVED"

# 2026-08-07: while sideways_filter_enabled(추세전환장 거래) is ON, a confirmed
# flag in the 09:00-11:00 morning window that runs AGAINST today's dominant
# PRIMARY_TREND is rejected as a pullback (see
# sideways_filter.evaluate_primary_trend_pullback) -- the held position still
# gets liquidated by the caller (sell-only/no-re-entry), it just doesn't flip
# into the counter-trend ETF. v5 (above) confines this check to the morning
# window only; from 11:00 onward the score+breakout gate is the sole
# authority (re-validated as the better combination for that window).
SIDEWAYS_PRIMARY_TREND_PULLBACK_BLOCKED = "SIDEWAYS_PRIMARY_TREND_PULLBACK_BLOCKED"

# ── Optional Trend Persistence entry filter (order authority gate only) ───
# 2026-08-07: reuses hynix_big_trend_engine.compute_trend_persistence_score
# (VWAP dwell + EMA5/10/20 stack + HH/HL or LH/LL structure, 0-100) to gate a
# NEW BUY only — mutually exclusive with sideways_filter_enabled/
# major_filter_enabled (worker._judge_entry_gate priority chain), OFF by
# default. Threshold validated via a 3-week read-only backtest sweep of
# 50/55/60/65/70 (scripts/backtest_trend_persistence_3week.py, 15 trading
# days 2026-07-20~2026-08-07): net_pnl/win_rate/profit_factor rose and
# max_drawdown fell monotonically across that whole range, so 70 — the top
# of the swept range — dominates every metric (net 1,952,444 / win rate 50%
# / MDD 182,982 / profit_factor 6.58 vs e.g. 50's 936,700 / 32.35% /
# 1,723,469 / 1.39), beating even the V6 major-flag filter on every metric.
TREND_PERSISTENCE_FILTER_DEFAULT = _env_bool("MACD2_TREND_PERSISTENCE_FILTER_DEFAULT", False)
TREND_PERSISTENCE_FILTER_VERSION = "TREND_PERSISTENCE_FILTER_V1_20260807"
TREND_PERSISTENCE_SCORE_MIN = _env_float("MACD2_TREND_PERSISTENCE_SCORE_MIN", 70.0)

TREND_PERSISTENCE_APPROVED = "TREND_PERSISTENCE_APPROVED"
TREND_PERSISTENCE_BELOW_THRESHOLD = "TREND_PERSISTENCE_BELOW_THRESHOLD"

# ── "2% 3회진입" filter — Optional Daily Single-Entry filter (order
# authority gate only) ─────────────────────────────────────────────────────
# 2026-08-10 v3 (사용자 요청 — 하루 전체 확정 플래그를 계속 평가하고, 신규진입
# 3회 캡만 유지하되 4번째 이후를 자동 차단하지 않음): v2's pure sequence-only
# cap (SINGLE_ENTRY_FILTER_V2, seq<=3 unconditional) is replaced with a
# SCORE, so a low-quality 1st/2nd/3rd flag can be skipped and a
# high-quality 4th+ flag can still enter, as long as fewer than
# SINGLE_ENTRY_MAX_DAILY_ENTRIES fills have happened so far today:
#   score = major_flag_filter's existing 0-100 component score
#         + seq bonus (SINGLE_ENTRY_SEQ_BONUS_1/_2/_3, 0 for 4th+)
#         + gap-expansion / EMA10-slope / 15m-price-slope bonuses (each
#           direction-aligned, computed AT the confirming bar only)
#         - overheat penalty (price_impulse_atr >= SINGLE_ENTRY_OVERHEAT_
#           THRESHOLD in the flag's own direction)
#   approved = (daily fill count < SINGLE_ENTRY_MAX_DAILY_ENTRIES)
#              and (score >= SINGLE_ENTRY_SCORE_MIN)
# 25-trading-day (2026-07-03~2026-08-07) READ-ONLY replay comparison,
# BOTH variants driven through the REAL worker.run_once()/order_executor/
# TradeCostEngine tick-by-tick (v2 reproduced via an in-memory-only
# monkeypatch of this same evaluate_single_entry, never touching this file
# — same order-gate dispatch, same Stop Loss/Quick Profit/Opposite-Signal/
# cost model as v3): v2 baseline = 75 trades (3.00/day), 53.3% win rate,
# 50.7% quick-profit-exit rate, net 1,737,014, PF 1.25, MDD 1,282,201,
# 16.0% of entries saw an opposite flag within 15min. This v3 score design
# (threshold=42) = 73 trades (2.92/day), 54.8% win rate, 53.4%
# quick-profit-exit rate, net 2,658,576 (+53% vs v2), PF 1.43, MDD
# 1,445,338 (+12.7% vs v2, the one metric that got worse), 13.7% opposite-
# flag-within-15min. v3 beats v2 on win rate/quick-profit rate/Net/PF; MDD
# is the tradeoff. The 4th+ leniency genuinely fires (7 of 73 v3 trades
# were seq>=4) but on THIS sample those 4th+ entries underperformed the
# 1st-3rd badly (28.6% win rate, avg net -37,921 vs 1st-3rd's 57.6%/+44,303)
# — kept anyway per explicit user spec ("4번째 이후 플래그라고 해서 자동
# 차단하지 않는다"), but re-validate this specific claim after more days
# of live data before leaning on it.
# near-zero BLUE (abs(macd) < SINGLE_ENTRY_NEAR_ZERO_MACD_THRESHOLD) is
# diagnostic-only (state.last_single_entry_near_zero_blue) — NOT added to
# the score; a 20-25 day sweep of 1000/1500/2000/2500/3000 found the
# unconditional near-zero BLUE cohort has a LOWER (not higher) +2% hit rate
# than the rest, so no bonus is applied pending more data.
#
# Mutually exclusive with sideways_filter_enabled/major_filter_enabled/
# trend_persistence_filter_enabled (worker._judge_entry_gate priority
# chain, lowest priority of the four), OFF by default. Exit management
# (Stop Loss/Profit Lock/Quick Profit/Forced Liquidation) and the flag
# generation logic itself (signal_engine) are both completely untouched —
# this gate only decides which confirmed crossovers get order authority.
SINGLE_ENTRY_FILTER_DEFAULT = _env_bool("MACD2_SINGLE_ENTRY_FILTER_DEFAULT", False)
SINGLE_ENTRY_FILTER_VERSION = "SINGLE_ENTRY_FILTER_V3_20260810"
SINGLE_ENTRY_MAX_DAILY_ENTRIES = _env_int("MACD2_SINGLE_ENTRY_MAX_DAILY_ENTRIES", 3)
SINGLE_ENTRY_SCORE_MIN = _env_float("MACD2_SINGLE_ENTRY_SCORE_MIN", 42.0)
SINGLE_ENTRY_SEQ_BONUS_1 = _env_float("MACD2_SINGLE_ENTRY_SEQ_BONUS_1", 25.0)
SINGLE_ENTRY_SEQ_BONUS_2 = _env_float("MACD2_SINGLE_ENTRY_SEQ_BONUS_2", 15.0)
SINGLE_ENTRY_SEQ_BONUS_3 = _env_float("MACD2_SINGLE_ENTRY_SEQ_BONUS_3", 8.0)
SINGLE_ENTRY_GAP_EXPANSION_BONUS = _env_float("MACD2_SINGLE_ENTRY_GAP_EXPANSION_BONUS", 2.0)
SINGLE_ENTRY_EMA10_SLOPE_BONUS = _env_float("MACD2_SINGLE_ENTRY_EMA10_SLOPE_BONUS", 2.0)
SINGLE_ENTRY_PRICE_SLOPE_15M_BONUS = _env_float("MACD2_SINGLE_ENTRY_PRICE_SLOPE_15M_BONUS", 2.0)
SINGLE_ENTRY_OVERHEAT_THRESHOLD = _env_float("MACD2_SINGLE_ENTRY_OVERHEAT_THRESHOLD", 0.5)
SINGLE_ENTRY_OVERHEAT_PENALTY = _env_float("MACD2_SINGLE_ENTRY_OVERHEAT_PENALTY", 10.0)
SINGLE_ENTRY_NEAR_ZERO_MACD_THRESHOLD = _env_float("MACD2_SINGLE_ENTRY_NEAR_ZERO_MACD_THRESHOLD", 3000.0)

SINGLE_ENTRY_APPROVED = "SINGLE_ENTRY_APPROVED"
SINGLE_ENTRY_DAILY_LIMIT_REACHED = "SINGLE_ENTRY_DAILY_LIMIT_REACHED"
SINGLE_ENTRY_SCORE_BELOW_THRESHOLD = "SINGLE_ENTRY_SCORE_BELOW_THRESHOLD"

# ── Optional Quick-Profit take-profit filter — EXIT LOGIC ONLY ─────────────
# 2026-08-04: standalone toggle, completely independent of BOTH
# major_filter_enabled and sideways_filter_enabled — it never affects which
# entries are placed (worker._judge_entry_gate/order_executor untouched),
# only what happens to an ALREADY-held position. Works underneath any entry
# mode (일반거래 / 강한 플래그 거래 / 추세전환장 모두), taking priority over the
# normal exit chain the moment it fires (checked in worker.py right after
# STOP_LOSS/OPPOSITE_SIGNAL/PROFIT_LOCK, so those are never preempted by it).
# OFF: entirely unchanged existing exit behavior — this toggle adds nothing
# when OFF, and turning it back OFF simply returns to holding until the next
# flag/Stop Loss/forced liquidation, exactly as before this toggle existed.
#
# 2026-08-05 (사용자 요청 — 기준치 변경 및 즉시-판정 재설계): 문턱을 1.5%->2.0%로
# 올리고, "1분 고점 기억" 근사(구 _update_quick_profit_minute_high, 진행 중인
# 분이 바뀌는 순간 그 이전 분의 고점 기억을 잃는 허점이 있었음)를 완전히 없앴다.
# 이제 매 tick의 실시간 quote(진행 중인/미확정 1분봉이든 상관없이) 하나만 보고
# 그 자리에서 즉시 순수익률 >= QUICK_PROFIT_TAKE_PROFIT_NET_PCT면 바로 전량
# 매도한다 — "기억된 고점"이 없으므로 그 기억이 이미 반전된 뒤 팔리는 문제 자체가
# 구조적으로 발생할 수 없다(2026-08-04에 고쳤던 문제의 근본 원인 제거). ON 상태로
# 전환된 바로 다음 tick부터(직전 이력과 무관하게) 즉시 이 조건으로 판정한다 —
# 이미 보유 중인 포지션이 이미 조건을 만족한 상태라면 그 tick에 바로 매도된다.
QUICK_PROFIT_FILTER_DEFAULT = _env_bool("MACD2_QUICK_PROFIT_FILTER_DEFAULT", False)
# 2026-08-18 사용자 요청: 문턱을 2.0%->2.5%로 상향.
QUICK_PROFIT_TAKE_PROFIT_NET_PCT = _env_float("MACD2_QUICK_PROFIT_TAKE_PROFIT_NET_PCT", 2.5)
EXIT_QUICK_PROFIT_TAKE_PROFIT = "QUICK_PROFIT_TAKE_PROFIT"

# ── Optional "시간대별 최적거래 필터" (Time-Window Optimal Trading Filter) —
# order gate + its OWN position-management ladder (2026-08-15 사용자 요청).
# Unlike major_flag_filter/sideways_filter/trend_persistence_filter/
# single_entry_filter (entry-gate only, exit logic untouched), this filter
# also owns take-profit/stop-loss ladder management for any position it
# opened — see app/trading/macd2/time_window_filter.py (entry gate) and
# app/trading/macd2/time_window_position_manager.py (exit ladder). Reuses
# signal_engine's confirmed MACD(12,26,9) crossover unchanged (no new flag
# creation, no change to which bar is "confirmed") and major_flag_filter's
# EMA10/EMA20/ATR/_prepare_bars helpers (no duplicated indicator math).
# Mutually exclusive with the other four entry filters — takes TOP priority
# in worker._judge_entry_gate when enabled (2026-08-15 사용자 요청: this is
# the newest, most complete redesign, meant to supersede the simpler
# entry-only gates when a user opts into it).
#
# 2026-08-27: this original variant ("TW1") was RETIRED and completely
# removed (code + toggle + UI) per 사용자 요청, after a validated TRAIN/OOS
# backtest (scripts/teg_c_train_oos_validation.py) showed TW2 + a Trend
# Establishment Gate (TEG) count-cap bypass beats plain TW2 on every OOS
# metric (entries/total%/compound%/MDD). See TIME_WINDOW_TEG_FILTER_DEFAULT
# below, which now occupies this toggle's former slot/priority tier. The
# §3-14 constants in this section (windows, MIN_FLAG_INTERVAL_MINUTES,
# QUALITY_SCORE_THRESHOLD, MAX_*_ENTRIES, MORNING_*/AFTERNOON_* ladder) are
# SHARED infrastructure time_window_filter.py/time_window_position_manager.py
# still expose to TW2 and the new TEG filter — untouched by TW1's removal.

# Session time windows (KST) — 20260815 spec §4-9.
TW_WINDOW1_START = time(9, 0)
TW_WINDOW1_END = time(9, 45)
TW_WINDOW2_START = time(9, 45)
TW_WINDOW2_END = time(10, 20)
TW_WINDOW3_START = time(10, 20)
TW_WINDOW3_END = time(10, 50)
TW_NO_NEW_ENTRY_START = time(10, 50)
TW_NO_NEW_ENTRY_END = time(13, 0)
TW_WINDOW5_START = time(13, 0)
TW_WINDOW5_END = time(14, 0)
TW_WINDOW6_START = time(14, 0)
TW_WINDOW6_END = time(15, 0)
# §9: "14:57 이후에는 새로운 플래그가 발생해도 15:00 이전 3분 확정이 불가능하므로
# 신규 진입시키지 않는다" — a flag confirmed at/after this time cannot complete
# its 3-minute confirmation before 15:00 forced liquidation.
TW_AFTERNOON_ENTRY_HARD_CUTOFF = time(14, 57)

# §3 짧은 왕복 교차 제거 + is_valid_reset().
MIN_FLAG_INTERVAL_MINUTES = _env_int("MACD2_TW_MIN_FLAG_INTERVAL_MINUTES", 9)
# is_valid_reset() sub-condition thresholds (not enumerated by name in the
# spec's "최소 다음 항목" list, but hardcoding them inline would violate its
# spirit — kept configurable and documented here):
#   1) opposite MACD state held for >= this many completed 3m bars before
#      the new flag ("최소 2개의 완성된 3분봉").
TW_RESET_MIN_OPPOSITE_BARS = _env_int("MACD2_TW_RESET_MIN_OPPOSITE_BARS", 2)
#   2) gap contraction ratio: the MACD-Signal gap must have shrunk to <= this
#      fraction of its value at the prior opposite flag before re-expanding.
TW_RESET_GAP_CONTRACTION_RATIO = _env_float("MACD2_TW_RESET_GAP_CONTRACTION_RATIO", 0.5)

# §4-9 windowed entry requirements.
# 2026-08-15 사용자 요청(승률 개선 튜닝 — 20거래일 백테스트로 검증, 앞/뒤 10일
# 분할검증 결과 승률 71.4%/63.2%, 총수익 +15.2%/+3.75%로 양쪽 다 양호): 원
# 스펙 기본값 4에서 3으로 완화. 4는 여전히 환경변수로 원복 가능.
# 2026-08-17 사용자 요청(승률/거래빈도 재튜닝 — 최근 20거래일(07/20~08/14)
# 백테스트로 검증): 3에서 4로 강화. 최근 20일 기준 승률 51.4%→58.6%(앞10일
# 55.6%/뒤10일 60.0% — 양쪽 다 개선, 과최적화 아님), 거래빈도 1.85→1.45회/일,
# 20일 누적수익 39.5%→30.6%, 최대낙폭 5.30%→5.30%. 이전 20일 구간(07/10~08/07)
# 재검증에서도 승률 개선 확인됨. 거래빈도를 늘리는 모든 시도(오후 세션 개방,
# 진입한도 상향 등)는 예외 없이 승률을 40%대로 떨어뜨려 반대 효과였음 — 3에서
# 4로 원복 가능(환경변수).
QUALITY_SCORE_THRESHOLD = _env_int("MACD2_TW_QUALITY_SCORE_THRESHOLD", 4)
TW_QUALITY_VOLUME_LOOKBACK_BARS = 5  # "최근 5개 완성봉 평균 거래량" — fixed by spec, not a sweep target

# §10 daily entry counts.
MAX_MORNING_ENTRIES = _env_int("MACD2_TW_MAX_MORNING_ENTRIES", 3)
MAX_AFTERNOON_ENTRIES = _env_int("MACD2_TW_MAX_AFTERNOON_ENTRIES", 2)
MAX_DAILY_ENTRIES = _env_int("MACD2_TW_MAX_DAILY_ENTRIES", 5)

# §11-12 morning position management.
# 2026-08-15 튜닝 히스토리 (사용자 요청으로 두 차례 조정, 둘 다 환경변수로
# 서로 되돌리기 가능):
#   1차(승률 우선): TP1/TP2를 원 스펙(2.5%/5.0%)에서 0.6%/1.2%로 축소 —
#     20거래일에서 승률 67.5%(+18.95%)로 원 스펙(37.3%, +11.4%)보다 승률은
#     크게 개선됐지만, 평균익절(1.47%)이 평균손절(-1.59%)보다 겨우 조금 큰
#     구조라 실제 aug-10~14 아웃오브샘플 5일에서는 승률 54.5%까지만 내려가도
#     순수익이 마이너스(-1.07%)로 뒤집혔다(사용자 지적: "평균 손절이 더 크니까
#     당연히 수익이 안좋지").
#   2차(원 스펙 TP 값으로 복원): TP1/TP2를 2.5%/5.0%로 되돌림 — 승률은
#     낮아지지만(20일 50.0%, aug10-14 5일 36.4%) 평균익절이 평균손절의 2~3배로
#     커져 두 구간 모두 순수익이 개선됐다(20일 +29.27%/PF 1.93, 5일 +6.94%/
#     PF 1.65 — 1차 튜닝에서 마이너스였던 5일 구간이 플러스로 전환). 승률
#     자체보다 리워드:리스크 비율이 총수익을 좌우한다는 결론.
#   3차(현재, 2026-08-18): TP1을 2.5%→3.0%로 상향(TP2 5.0%는 유지) — 최근
#     20영업일/56영업일 시뮬레이션 모두에서 승률·MDD·최대연속손실은 거의
#     동일하거나 유지되면서 누적수익(복리)과 Profit Factor가 함께 개선됨을
#     확인(20일: PF 2.23→2.33, 복리 30.09%→33.04%; 56일: PF 1.39→1.45, 복리
#     47.42%→56.87%). MU_MACD는 이 모듈(time_window_position_manager)을 그대로
#     import해서 쓰므로 이 상수 하나의 변경이 SK-MACD2/MU_MACD 양쪽에 모두
#     적용된다.
MORNING_TP1 = _env_float("MACD2_TW_MORNING_TP1", 0.03)
MORNING_TP1_SELL_RATIO = _env_float("MACD2_TW_MORNING_TP1_SELL_RATIO", 0.50)
MORNING_TP2 = _env_float("MACD2_TW_MORNING_TP2", 0.05)
#   4차(현재, 2026-08-18): 손절을 -1.5%->-1.7%로 완화 — 완성 3분봉 종가 기준
#     TRAIN(34일)/VAL(11일)/OOS(11일)을 나눠 -1.55~-1.80% 스윕한 결과,
#     -1.65~-1.75% 구간 전체가 세 구간 모두에서 -1.5%대보다 승률/복리수익/PF가
#     고르게 좋았고(단일 최고점이 아니라 구간 전체가 안정적), 그중 -1.7%가
#     TRAIN 2위·VAL 1위·OOS 2위로 어느 한 구간에도 치우치지 않아 과최적화
#     위험이 가장 낮았다. MDD는 VAL/OOS에서 소폭(1%p 미만) 상승, 최대연속손실은
#     전 구간에서 불변. MU_MACD도 이 모듈(time_window_position_manager)을 그대로
#     import해서 쓰므로 이 상수 하나의 변경이 SK-MACD2/MU_MACD 양쪽에 모두
#     적용된다 -- MU_MACD 자체의 (별도) 비-TW-필터 기본 손절(config.
#     STOP_LOSS_NET_PCT)은 이 필터가 검증 대상으로 삼은 적이 없어 그대로 둔다.
MORNING_STOP_LOSS = _env_float("MACD2_TW_MORNING_STOP_LOSS", -0.017)
MORNING_AFTER_TP1_STOP = _env_float("MACD2_TW_MORNING_AFTER_TP1_STOP", 0.003)
MORNING_TRAILING_TRIGGER = _env_float("MACD2_TW_MORNING_TRAILING_TRIGGER", 0.035)
MORNING_TRAILING_STOP = _env_float("MACD2_TW_MORNING_TRAILING_STOP", 0.020)

# §13-14 afternoon position management (2026-08-15: afternoon entries are
# disabled by default via TW_MORNING_ONLY below; kept at the original spec
# value for consistency with MORNING_TP1 if that toggle is ever turned off).
AFTERNOON_TP = _env_float("MACD2_TW_AFTERNOON_TP", 0.025)
AFTERNOON_STOP_LOSS = _env_float("MACD2_TW_AFTERNOON_STOP_LOSS", -0.012)
AFTERNOON_BREAKEVEN_TRIGGER = _env_float("MACD2_TW_AFTERNOON_BREAKEVEN_TRIGGER", 0.015)
AFTERNOON_BREAKEVEN_STOP = _env_float("MACD2_TW_AFTERNOON_BREAKEVEN_STOP", 0.002)
AFTERNOON_PROFIT_LOCK_TRIGGER = _env_float("MACD2_TW_AFTERNOON_PROFIT_LOCK_TRIGGER", 0.020)
AFTERNOON_PROFIT_LOCK_STOP = _env_float("MACD2_TW_AFTERNOON_PROFIT_LOCK_STOP", 0.010)

# §15 중복 진입 방지.
ALLOW_PYRAMIDING = _env_bool("MACD2_TW_ALLOW_PYRAMIDING", False)

# 2026-08-18 사용자 요청: 원래 스펙(§1)은 플래그 확정 bar T에서 order 권한을
# 안 주고, 한 bar(T+3) 더 기다려 MACD-Signal gap이 실제로 더 벌어졌는지 재확인
# 한 뒤에만 진입한다 — 이 "한 bar 대기"가 진입을 너무 늦춰 승률/수익을 깎는다는
# 지적. True면 evaluate_time_window_entry_immediate()(gap 재확인 없이 flag
# bar T 자신의 데이터만으로 판단)가 대신 쓰인다.
# scripts/tw_gate_immediate_entry_research.py로 TRAIN(34)/VAL(11)/OOS(11)
# 검증: "gap 확장 재확인" 대신 gap/ATR 비율(decisive-cross)이나 MACD 가속도로
# 약한 크로스를 거르는 두 후보 모두 어떤 임계값에서도 필터 없음(0.0)보다
# 나빴다(TRAIN에서 진입수/누적수익/PF 전부 하락) -- 이 두 leading indicator는
# 실제로 나쁜 플래그를 걸러내지 못하고 좋은 진입까지 함께 쳐냈다. 필터 없이
# 즉시진입만 했을 때는 TRAIN/VAL에서 기존 T+3 대비 누적수익·PF가 개선됐지만,
# 진짜 holdout인 OOS(11일)에서는 승률 60%->50%/PF 2.46->1.64/MDD 3.6%->5.7%로
# 오히려 악화됐다 -- 아직 프로덕션에 자신있게 켤 만큼 검증되지 않았다는 뜻이라
# 기본은 False로 유지. TW_IMMEDIATE_MIN_GAP_ATR_RATIO은 0.0(비활성 -- 필터
# 없음이 테스트한 것 중 최선이었으므로)이 기본이며, 더 나은 leading filter가
# 검증되기 전까지 이 값을 올리는 것은 TRAIN 결과만으로도 역효과가 확인됨.
TW_IMMEDIATE_ENTRY_ENABLED = _env_bool("MACD2_TW_IMMEDIATE_ENTRY_ENABLED", False)
TW_IMMEDIATE_MIN_GAP_ATR_RATIO = _env_float("MACD2_TW_IMMEDIATE_MIN_GAP_ATR_RATIO", 0.0)

# 2026-08-15 사용자 요청 (승률/완화 튜닝 — 20거래일 백테스트로 검증): 기본을
# True로 변경 — 이 구간도 W3/W5와 동일한 quality-score 게이트(QUALITY_SCORE_
# THRESHOLD, EMA20 기준)로 진입을 허용한다. 원 스펙(§7, 이 구간 전면 금지)은
# 환경변수로 여전히 복원 가능(False).
TW_ALLOW_ENTRY_1050_1300 = _env_bool("MACD2_TW_ALLOW_ENTRY_1050_1300", True)

# 2026-08-15 사용자 요청(승률 개선 튜닝): 오후 구간(13:00-15:00)은 20거래일
# 백테스트에서 오전보다 뚜렷하게 승률·수익이 낮았다(오전만 필터링 시 승률
# 67.5%/+18.95%, 전체 세션 포함 시 58.6%/+8.4%) -- 기본을 "오전만 진입"으로
# 바꾼다. True면 13:00-14:00(W5)/14:00-15:00(W6) confirmed 플래그는 신규
# 진입 없이 REJECT_TIME_WINDOW로 거절된다(원 스펙 §7-9는 환경변수로 복원 가능,
# False). 기존 포지션의 오후 청산 로직(time_window_position_manager.
# evaluate_afternoon_position)은 이 토글과 무관하게 그대로 동작한다 — 이건
# 신규 진입만 막는 게이트다.
TW_MORNING_ONLY = _env_bool("MACD2_TW_MORNING_ONLY", True)

# Decision / reject-reason labels (§16 debug log examples).
TW_APPROVED = "TIME_WINDOW_APPROVED"
TW_PENDING_CONFIRMATION = "TIME_WINDOW_PENDING_CONFIRMATION"
TW_REJECT_SHORT_FLAG_INTERVAL = "REJECT_SHORT_FLAG_INTERVAL"
TW_REJECT_NOT_CONFIRMED = "REJECT_NOT_CONFIRMED"
TW_REJECT_MACD_GAP_NOT_EXPANDING = "REJECT_MACD_GAP_NOT_EXPANDING"
TW_REJECT_LOW_QUALITY_SCORE = "REJECT_LOW_QUALITY_SCORE"
TW_REJECT_NO_RESET = "REJECT_NO_RESET"
TW_REJECT_TIME_WINDOW = "REJECT_TIME_WINDOW"
TW_REJECT_MAX_ENTRY_COUNT = "REJECT_MAX_ENTRY_COUNT"
TW_REJECT_DUPLICATE_POSITION = "REJECT_DUPLICATE_POSITION"

# ── 반대신호 청산 T+3 재확인("휩쏘-내성", 2026-08-19 사용자 요청) ──────────
# evaluate_time_window_entry가 반대방향 재진입 후보를 거절했을 때, 그 사유가
# 이 두 가지("MACD-Signal 관계가 T+3에도 유지 안 됨" / "gap이 확대 안 됨")면
# 원래 방향으로 복귀한 휩쏘로 보고 보유 포지션을 그대로 둔다(그 외 사유
# -- 품질점수/시간대/최대진입횟수/중복포지션 -- 는 기존과 동일하게 무조건
# 매도). 56거래일 TRAIN/VAL/OOS 백테스트(scripts/tw_gate_relaxed_optimization.py
# 계열 -- OOS/TRAIN 둘 다 손해 없이 개선, VAL은 휩쏘 이벤트 자체가 없어
# 동일)로 검증. gap 절대값 임계값 같은 추가 조건은 아직 없음(단순 버전).
# -1.7% 하드 손절(STOP_LOSS_NET_PCT/MORNING_STOP_LOSS 등)은 이 로직과
# 완전히 무관하게 매 tick 즉시 평가되므로 영향받지 않는다.
TW_WHIPSAW_REJECT_REASONS = frozenset({TW_REJECT_NOT_CONFIRMED, TW_REJECT_MACD_GAP_NOT_EXPANDING})

# ── Optional "탈락 DOWN_BLUE 예외진입" (하루 최대 1회) — 2026-08-18 사용자
# 요청. TW 필터(time_window_filter.evaluate_time_window_entry)가 REJECT한
# DOWN_BLUE 플래그만, 다른 조건 없이 하루 최대 1회 추가로 진입을 허용한다.
# 56거래일 TRAIN(34)/VAL(11)/OOS(11) 백테스트에서 검증된 3가지 중 채택된
# 버전(scripts/scratch_down_blue_exception_research.py variant B):
#   - "무조건 허용"이 TRAIN/VAL/OOS 세 구간 전부에서 개선 (56일 연쇄복리
#     69.34%->105.33%, PF 1.38->1.40, MDD 거의 불변 21.25%->21.11%).
#   - "+직전 반대플래그 지속>=45분" 조건을 추가한 버전은 오히려 VAL이
#     역전(-1.1%p)되어 재현성이 떨어져 기각 — 조건 없이 채택.
# TW 필터 자체(time_window_2_filter_enabled/time_window_teg_filter_enabled)가
# 둘 다 꺼져 있으면 이 토글은 아무 효과가 없다(진입 후보 자체가 생기지 않음).
# OFF가 기본값.
TW_DOWN_BLUE_EXCEPTION_FILTER_DEFAULT = _env_bool("MACD2_TW_DOWN_BLUE_EXCEPTION_FILTER_DEFAULT", False)
TW_DOWN_BLUE_EXCEPTION_FILTER_VERSION = "TW_DOWN_BLUE_EXCEPTION_V1_20260818"
TW_EXCEPTION_DOWN_BLUE_ENTRY = "TIME_WINDOW_EXCEPTION_DOWN_BLUE_ENTRY"
# NOTE: this "+1 DOWN_BLUE 예외진입" sub-filter has NO enabled-check of its
# own -- worker.py only ever reaches the down_blue_exception_applied branch
# from inside _resolve_time_window_candidate, which runs whenever EITHER TW2
# (time_window_2_filter_enabled) OR the TEG filter (time_window_teg_filter_
# enabled) is on (2026-08-21 사용자 요청: "+1 down blue 필터는 둘다 가능하게
# 해줘"; 2026-08-27: TW1 retired, TEG filter takes its former slot in this
# OR) -- so this toggle transparently works under both without any code
# change here.

# ── Optional TW2 ("시간대별 최적거래 필터 2") — VWAP 역행 veto + 최근 30분
# 교차과다 veto + TP2 상향 (2026-08-21 사용자 요청). 기본 T+3 재확인/품질점수/
# 시간대/최대진입횟수 게이트 + 포지션관리 래더를 그대로 재사용하고, 딱 두 가지
# 진입 veto만 추가로 얹는다(worker._resolve_time_window_candidate가 기본 게이트가
# 이미 승인한 후보에 대해서만 time_window_filter.evaluate_tw2_extra_vetoes()를
# 추가로 검사):
#   1) VWAP 역행 veto: 확정bar 종가가 진입방향 기준 세션 VWAP보다
#      TW2_VWAP_VETO_THRESHOLD_PCT(%) 이상 불리한 쪽에 있으면 스킵.
#   2) 최근 교차과다 veto: 직전 TW2_RECENT_CROSS_LOOKBACK_MINUTES분 안에
#      TW2_RECENT_CROSS_VETO_COUNT회 이상 확정 크로스오버가 있었으면(휩쏘
#      구간) 스킵.
# 통과한 진입은 TP1(3.0%)/손절(-1.7%)/트레일링 래더는 TW1과 완전히 동일하게
# 유지하되 TP2만 5.0%→TW2_MORNING_TP2(6.0%)로 올린다
# (time_window_position_manager의 tp2_pct_override 파라미터로 전달 -- MU_MACD
# 등 다른 호출자는 파라미터를 안 넘기므로 전혀 영향 없음).
#
# 2026-07-10~08-21 연속 29거래일(8/10-8/20 KIS 재조회로 공백 없이 채움),
# 앞 19일 TRAIN에서 찾은 위 임계값들을 뒤 10일 OOS에서 전혀 재튜닝하지 않고
# 그대로 재검증 — 진짜 진입시점 로직으로 스킵된 후보가 이후 진입횟수/포지션
# 상태까지 바꾸는 연쇄효과까지 전부 재현한 replay 결과:
#   TRAIN(19일): TW1 복리 +20.33% -> TW2 +52.31%, MDD 13.48%->6.18%.
#   OOS(10일, 재튜닝 없음): TW1 복리 +24.48% -> TW2 +35.70%, MDD 6.83%->5.18%.
#   FULL(29일): TW1 복리 +49.79% -> TW2 +106.69%.
# TW2는 (2026-08-27 TW1 제거 이후) 새 TEG 필터와 동시에 켤 수 없다 —
# service.set_time_window_2_filter_enabled()/set_time_window_teg_filter_
# enabled()가 서로를 자동으로 끈다. "+1 DOWN_BLUE 예외진입"은 위 노트대로
# 둘 다에서 그대로 동작한다. OFF가 기본값.
TIME_WINDOW_2_FILTER_DEFAULT = _env_bool("MACD2_TIME_WINDOW_2_FILTER_DEFAULT", True)
TIME_WINDOW_2_FILTER_VERSION = "TIME_WINDOW_2_V1_20260821"
TIME_WINDOW_2_STRATEGY_NAME = "시간대별 최적거래 필터 (TW2)"
TW2_VWAP_VETO_THRESHOLD_PCT = _env_float("MACD2_TW2_VWAP_VETO_THRESHOLD_PCT", -1.0)
TW2_RECENT_CROSS_VETO_COUNT = _env_int("MACD2_TW2_RECENT_CROSS_VETO_COUNT", 4)
TW2_RECENT_CROSS_LOOKBACK_MINUTES = _env_int("MACD2_TW2_RECENT_CROSS_LOOKBACK_MINUTES", 30)
TW2_MORNING_TP2 = _env_float("MACD2_TW2_MORNING_TP2", 0.06)
TW2_REJECT_VWAP_VETO = "TW2_REJECT_VWAP_VETO"
TW2_REJECT_RECENT_CROSSES = "TW2_REJECT_RECENT_CROSSES"

# ── TW2 + TEG count-cap bypass ("TEG 필터", 2026-08-27 사용자 요청) ─────────
# Replaces TW1's former toggle slot/priority tier entirely. Entry gating is
# BYTE-IDENTICAL to TW2 (same evaluate_time_window_entry + evaluate_tw2_
# extra_vetoes, same TW2_MORNING_TP2 exit ladder) with exactly one addition:
# a candidate TW2 would reject SOLELY for exceeding the daily entry-count cap
# (block_reason == TW_REJECT_MAX_ENTRY_COUNT -- which, by evaluate_time_
# window_entry's own check ordering, is only ever reached after interval/
# window/quality-score/reset/extra-vetoes already passed) gets ONE extra
# chance per day: if app.trading.macd2.teg_gate.evaluate_teg() also approves
# it, it enters anyway. Capped at exactly 1 bypass/trading day (state.
# time_window_teg_count_cap_bypass_used, reset on day rollover like the
# down-blue-exception flag) -- matches the actual backtested "variant C"
# behavior (scripts/teg_backtest_60day_v2.py), NOT an uncapped bypass.
#
# Validated 2026-06-01~08-26 (60 business days), TRAIN=2026-06-01~07-28 (40
# days, threshold calibration only) / OOS=2026-07-29~08-26 (20 days, held
# out): OOS variant C (TW2+bypass) beat variant A (plain TW2) on ALL FOUR
# decision metrics -- entries 52->58, total% 20.865->25.493, compound%
# 20.015->25.501, MDD% 13.012->13.012 (tied). See
# data/validation/teg_c_train_oos/summary.json for full results and
# app/trading/macd2/teg_gate.py for the frozen TEG condition thresholds'
# own derivation. Mutually exclusive with TW2 (the two setters force each
# other off, same pattern as TW1/TW2 before). OFF by default.
#
# 2026-08-27 확장 (사용자 요청, 위 TRAIN/OOS 백테스트는 이 케이스를 검증하지
# 않음): 같은 하루 1회 우회가 count-cap 초과뿐 아니라 TW_MORNING_ONLY로
# 시간대 자체에서 막힌 오후(13:00-14:57) 후보에도 적용된다 (worker.py의
# _resolve_time_window_candidate 참고). TW_MORNING_ONLY=True 기본값은 20
# 거래일 백테스트에서 오후 진입 포함 시 승률/수익이 뚜렷이 나빴다는 근거로
# 도입됐다(위 TW_MORNING_ONLY 주석 참고) -- TEG의 개별 조건(VWAP/EMA/gap
# 가속도 등)이 그 나쁜 오후 진입들을 충분히 걸러낼지는 아직 별도로 backtest
# 검증되지 않았다.
TIME_WINDOW_TEG_FILTER_DEFAULT = _env_bool("MACD2_TIME_WINDOW_TEG_FILTER_DEFAULT", False)
TIME_WINDOW_TEG_FILTER_VERSION = "TIME_WINDOW_TEG_V2_20260827"
TIME_WINDOW_TEG_STRATEGY_NAME = "TW2 + TEGv2 추가진입"
TW_TEG_COUNT_CAP_BYPASS = "TW_TEG_COUNT_CAP_BYPASS"

# ── TW2 3-SLOT ("3슬롯 유연배분", 2026-09-01 사용자 요청) ────────────────────
# A THIRD, separately selectable time-window mode alongside TW2 and TEG —
# mutually exclusive with BOTH (service.py's three setters each force the
# other two off; app/trading/macd2/time_window_3slot.py's own module
# docstring explains why: TEG's gate check already fires on the SAME
# ``time_window_2_filter_enabled or time_window_teg_filter_enabled``
# condition this mode would otherwise overlap with). Reuses TW2's own
# evaluate_time_window_entry/evaluate_tw2_extra_vetoes/teg_gate.evaluate_teg/
# time_window_position_manager ladder/whipsaw-tolerant reversal exit
# COMPLETELY UNCHANGED -- see app/trading/macd2/time_window_3slot.py's
# module docstring for exactly what is reused vs. new. Backtested as
# "Strategy C" (scripts/tw2_3slot_flex_backtest.py) and cross-validated via a
# second, independently-coded simulation loop
# (scripts/tw2_3slot_flex_cross_check.py) over 60 trading days
# (TRAIN 2026-06-05~07-31 / OOS 2026-08-03~08-31, data/validation/
# tw2_3slot_flex/): beats plain TW2 on OOS compound return, PF, MDD, and
# top10-excluded return (the metric weighted most -- C is materially less
# reliant on a handful of big wins than TW2). OFF by default -- TW2 stays
# the live default; the user will flip to this mode manually after live
# verification, at their own pace.
#
# Daily hard cap: TW2_3SLOT_DAILY_CAP (3) new entries total, enforced
# entirely in time_window_3slot.resolve_slot -- worker.py always calls
# evaluate_time_window_entry with morning_entry_count=0/afternoon_entry_
# count=0/daily_entry_count=0 for this mode so ITS OWN 3/2/5 caps never
# fire; this mode's own tw2_3slot_slots_used_today/tw2_3slot_morning_count/
# tw2_3slot_afternoon_count state fields (separate from TW2's own
# time_window_morning_entry_count/afternoon_entry_count -- never shared or
# corrupted) are the only cap enforced.
# 09:00-11:00 morning: 1st/2nd candidate = plain TW2 approval (T+3 + extra
# vetoes, no extra gate). 3rd candidate (2 slots already used, still
# morning) additionally requires >= TW2_3SLOT_MORNING_3RD_QUALITY_MIN (3) of
# the 5 "Trend Quality" conditions (time_window_3slot.evaluate_trend_
# quality) -- if it fails, the 3rd slot is preserved for the afternoon
# rather than spent.
# 11:00-14:50 afternoon: only if a slot remains. Requires TW2 approval AND
# teg_gate.evaluate_teg() approval (both mandatory -- NOT production's
# once-daily count-cap-bypass mechanism, a deliberately different use of the
# same unmodified TEG function). A 2nd afternoon entry (when 2 slots remain)
# requires the prior afternoon position to be fully closed AND the new
# candidate to be the OPPOSITE direction from it -- a same-direction
# re-entry is rejected outright.
# PREMARKET_CARRY (08:45-08:59:59 flag surviving unopposed to 09:03) is
# reused verbatim for this mode too (worker.py's existing premarket-carry
# functions gain an additive OR-condition, exactly as TEG's addition to TW2
# did before) -- if it fires, it consumes 1 of the day's 3 slots.
TW2_3SLOT_FILTER_DEFAULT = _env_bool("MACD2_TW2_3SLOT_FILTER_DEFAULT", False)
TW2_3SLOT_FILTER_VERSION = "TW2_3SLOT_V1_20260901"
TW2_3SLOT_STRATEGY_NAME = "TW2 3-SLOT"
TW2_3SLOT_DAILY_CAP = _env_int("MACD2_TW2_3SLOT_DAILY_CAP", 3)
TW2_3SLOT_MORNING_WINDOW_END = time(11, 0)
TW2_3SLOT_AFTERNOON_WINDOW_END = time(14, 50)
TW2_3SLOT_MORNING_3RD_QUALITY_MIN = _env_int("MACD2_TW2_3SLOT_MORNING_3RD_QUALITY_MIN", 3)  # out of 5, TRAIN-selected
TW2_3SLOT_QUALITY_NET_CHANGE_BARS = 2
TW2_3SLOT_EMA20_SLOPE_BARS = 2
TW2_3SLOT_REJECT_OUTSIDE_WINDOW = "TW2_3SLOT_REJECT_OUTSIDE_WINDOW"
TW2_3SLOT_REJECT_SLOT_CAP = "TW2_3SLOT_REJECT_DAILY_SLOT_CAP"
TW2_3SLOT_REJECT_QUALITY = "TW2_3SLOT_REJECT_TREND_QUALITY"
TW2_3SLOT_REJECT_TEG = "TW2_3SLOT_REJECT_TEG"
TW2_3SLOT_REJECT_SAME_DIRECTION_AFTERNOON_2ND = "TW2_3SLOT_REJECT_SAME_DIRECTION_AFTERNOON_2ND"

# ── "무필터 09:00-11:00" 즉시청산 진입모드 (2026-08-20 사용자 요청) ─────────
# 6th peer entry gate in worker._judge_entry_gate (right after TIME_WINDOW),
# same shape as MAJOR/SIDEWAYS/TREND_PERSISTENCE/SINGLE_ENTRY: a single
# approve/reject MajorFlagDecision, no quality score, no T+3 pending wait --
# approved iff decision_at falls in [NO_FILTER_ENTRY_WINDOW_START,
# NO_FILTER_ENTRY_WINDOW_END). Because it is judged through the SAME generic
# path those four filters use (never through TIME_WINDOW's own
# _resolve_time_window_candidate/whipsaw logic), a rejected reversal under
# this gate ALWAYS sells immediately via worker._execute_reversal_exit_
# only_for_filtered_entry -- there is no whipsaw-tolerant hold for this mode,
# by construction, without any change to the TIME_WINDOW whipsaw code itself.
# 56일 TRAIN(34)/VAL(11)/OOS(11) corrected-clock 백테스트(scripts/tw_gate_
# corrected_4scenario_compare.py)에서 이 조합(무필터 09-11 + 즉시청산)이
# TW필터+휩쏘내성보다 56일 복리수익 우위(+104.8% vs +15.7%)를 보여 채택.
# OFF가 기본값 -- UI 체크박스 / service.set_no_filter_0900_1100_filter_enabled()
# 로만 켠다. TIME_WINDOW_FILTER와 동시에 켜지면 TIME_WINDOW가 우선한다
# (_judge_entry_gate의 기존 우선순위 그대로).
NO_FILTER_0900_1100_FILTER_DEFAULT = _env_bool("MACD2_NO_FILTER_0900_1100_FILTER_DEFAULT", False)
# V2 (2026-08-28, real incident): this mode now also rejects once the true
# filter-agnostic daily entry count (state.daily_total_entry_count) reaches
# config.MAX_DAILY_ENTRIES -- previously unlimited, letting a TW2/TEG<->
# no-filter toggle bypass the daily cap from this side. The 09:00-11:00
# window condition and 56-day TRAIN/VAL/OOS-validated immediate-liquidation
# behavior above are unchanged -- see worker._judge_no_filter_flag.
NO_FILTER_0900_1100_FILTER_VERSION = "NO_FILTER_0900_1100_V2_20260828"
NO_FILTER_ENTRY_WINDOW_START = time(9, 0)
NO_FILTER_ENTRY_WINDOW_END = time(11, 0)
NO_FILTER_REJECT_OUTSIDE_WINDOW = "REJECT_OUTSIDE_ENTRY_WINDOW"

# Exit-reason labels for the position-management ladder (§11-14).
EXIT_TW_STOP_LOSS = "TIME_WINDOW_STOP_LOSS"
EXIT_TW_TP1_PARTIAL = "TIME_WINDOW_TP1_PARTIAL"
EXIT_TW_TP2_FULL = "TIME_WINDOW_TP2_FULL"
EXIT_TW_AFTER_TP1_STOP = "TIME_WINDOW_AFTER_TP1_STOP"
EXIT_TW_TRAILING_STOP = "TIME_WINDOW_TRAILING_STOP"
EXIT_TW_AFTERNOON_TP = "TIME_WINDOW_AFTERNOON_TP"
EXIT_TW_BREAKEVEN_STOP = "TIME_WINDOW_BREAKEVEN_STOP"
EXIT_TW_PROFIT_LOCK_STOP = "TIME_WINDOW_PROFIT_LOCK_STOP"

# ── Isolated MACD2 runtime/ledger paths (never shared with MACD v1) ───────
# Resolved lazily via app.utils.data_paths inside state_store.py/ledger.py so
# tests can monkeypatch those modules' own path constants, not these names.
RUNTIME_STATE_FILENAME = "macd2_runtime.json"
SIGNAL_LEDGER_FILENAME = "macd2_signal_ledger.csv"
EXECUTION_LEDGER_FILENAME = "macd2_execution_ledger.csv"
