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
}


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
    _wrap(worker, "_persist_tw2_3slot_decision", lambda r, a, k: _glog(
        "DECISION", sid=a[2] if len(a) > 2 else None, approved=bool(a[1].approved),
        decision=a[1].decision, block=a[1].block_reason, metrics=dict(a[1].metrics or {})))

    # ── 2026-10-30 ARM3 ② +3% 틱 수익보호선 (B=SOFT / C=MEDIUM) ────────────
    # 기존 청산(TP/SL/C1/B3/H50/반대신호/시간/15:00)은 한 줄도 바꾸지 않는다.
    # 같은 tick 에 기존 청산이 발동하면 그쪽이 이긴다 — 이 블록은 기존 위험관리가
    # 아무것도 하지 않은 tick 에서만 판정한다("먼저 발생한 것 적용").
    ARM = {"epoch": None, "peak": 0.0, "line": None, "prev_t": None}
    ARM3LOG = []
    ARM_REASON = "TICK_ARM3_STOP"

    if _ARM in ("SOFT", "MEDIUM"):
        from app.trading.macd2 import order_executor as _oex
        from app.trading.macd2.models import SignalState as _SS
        _o_rm = worker._advance_held_position_risk_management

        def _arm_rm(*, broker, state, market_data, now, quotes, pos, result):
            fired = _o_rm(broker=broker, state=state, market_data=market_data, now=now,
                          quotes=quotes, pos=pos, result=result)
            ep = int(getattr(state, "position_epoch", 0) or 0)
            if ARM["epoch"] != ep:
                ARM.update(epoch=ep, peak=0.0, line=None, prev_t=None)
            if fired or state.position is None or int(state.position.quantity or 0) <= 0:
                return fired
            sym, avg, qty = pos.symbol, float(pos.avg_price), int(pos.quantity)
            t0 = ARM["prev_t"] or (now - timedelta(seconds=_TICK_SEC))
            ARM["prev_t"] = now
            # 틱 최고수익: (직전 tick, 지금] 의 실제 1초 스냅샷 전부. 없으면 지금 시세 하나.
            w = _tick_window(sym, t0, now)
            if w is not None:
                hi = float(w["price"].max())
                lo_bid = float(w["best_bid"].min()) if w["best_bid"].notna().any() else None
            else:
                cur = quotes.get(sym) or px(sym, now)
                hi = float(cur) if cur else None
                lo_bid = BOOKQ.get(sym, (None, None))[0]
            if hi is not None:
                ARM["peak"] = max(ARM["peak"], worker._net_return_pct(sym, avg, hi, qty))
            line = _arm_line(ARM["peak"])
            if line is not None and (ARM["line"] is None or line > ARM["line"]):
                ARM["line"] = line               # 보호선은 내려가지 않는다
                ARM3LOG.append(dict(k="ARM", t=now.isoformat(), sym=sym, peak=round(ARM["peak"], 4),
                                    line=ARM["line"], epoch=ep))
            if ARM["line"] is None or lo_bid is None:
                return fired
            bid_net = worker._net_return_pct(sym, avg, float(lo_bid), qty)
            if bid_net > ARM["line"]:
                return fired
            ARM3LOG.append(dict(k="BREACH", t=now.isoformat(), sym=sym, peak=round(ARM["peak"], 4),
                                line=ARM["line"], bid=float(lo_bid), bid_net=round(bid_net, 4), epoch=ep))
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
    # ⑥ 산출에 새 기록 추가
    ("p70r=P70RLOG, par=dict(",
     "gates=GATELOG, arm3=ARM3LOG, "
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
