    # ── CHOP STAGED ENTRY (STG50 = 50/50, STG30 = 30/70) ────────────────
    # 평소에는 현행 P3 와 완전히 동일하다. 진입 직전 **최근 30분**이
    # "저효율(PE<0.20) + 저변동(range<1.0%) + 좁은 박스 왕복(EMA20 교차 3회 이상)"
    # 으로 판정될 때만
    # T+3 승인 시 일부만 진입하고, (플래그봉, T+3 확정봉) 극값을 돌파하면
    # 15분 안에 나머지를 추가한다. 미도달이면 추가진입만 취소한다.
    # 첫 부분진입 포지션은 production P3 경로 그대로 관리된다.
    STG = {"STG50": 0.5, "STG30": 0.3}.get(VAR)
    PE_MAX, RNG_MAX, XC_MIN, WIN_MIN, ADD_WAIT_MIN = 0.20, 1.0, 3, 30, 15
    PEND = {"rec": None}
    CTX = {"bars_3m": None, "flag_bar_ts": None}
    STG_LOG = []

    def _chop30(now):
        """최근 30분 (PE, RNG, EMA20 교차횟수). 당일 완성봉만 — calib2.py 와 같은 정의."""
        d = etf.get(config.WATCH_SYMBOL)
        if d is None:
            return None
        ts = pd.Timestamp(now)
        done = d[(d["datetime"] + pd.Timedelta(minutes=1) <= ts)
                 & (d["datetime"].dt.date == ts.date())]
        if len(done) < WIN_MIN:
            return None
        c_all = done["close"].astype(float)
        ema = c_all.ewm(span=20, adjust=False).mean()
        w = done.iloc[-WIN_MIN:]
        c = w["close"].to_numpy(dtype=float)
        path = float(abs(pd.Series(c).diff()).sum())
        if path <= 0:
            return None
        pe = abs(c[-1] - c[0]) / path
        rng = float((w["high"].max() - w["low"].min()) / c[-1] * 100.0)
        s = (c_all - ema).to_numpy()[-WIN_MIN:]
        s = s[s != 0]
        xc = int((abs(pd.Series(s).apply(lambda v: 1 if v > 0 else -1).diff().dropna()) > 0).sum()) if len(s) > 1 else 0
        return pe, rng, xc

    def _is_chop(now):
        f = _chop30(now)
        return (f is not None and f[0] < PE_MAX and f[1] < RNG_MAX and f[2] >= XC_MIN), f

    def _trigger_px(bars_3m, flag_bar_dt, confirm_bar_dt, direction):
        ts = pd.to_datetime(bars_3m["datetime"])
        rows = []
        for dt in (flag_bar_dt, confirm_bar_dt):
            if dt is None:
                continue
            m = bars_3m[ts == pd.Timestamp(dt)]
            if len(m):
                rows.append(m.iloc[-1])
        if not rows:
            return None
        if direction == Direction.UP_RED:
            return max(float(r["high"]) for r in rows)
        return min(float(r["low"]) for r in rows)

    def _hy_px(now):
        """기초자산 호가 — REAL 체결모형과 같은 분봉 시가→종가 선형보간."""
        ix = idx.get(config.WATCH_SYMBOL)
        ts = pd.Timestamp(now)
        m = ts.floor("min")
        if ix is not None and m in ix.index:
            r = ix.loc[m]
            if isinstance(r, pd.DataFrame):
                r = r.iloc[-1]
            return float(r["open"]) + (float(r["close"]) - float(r["open"])) * ((ts - m).total_seconds() / 60.0)
        d = etf.get(config.WATCH_SYMBOL)
        if d is None:
            return None
        done = d[d["datetime"] + pd.Timedelta(minutes=1) <= ts]
        return float(done["close"].iloc[-1]) if len(done) else None

    def _pub(rec, **extra):
        return dict(sid=rec["signal_id"], dir=rec["dir"], trigger=rec["trigger"],
                    pe=rec["pe"], rng=rec["rng"], xc=rec["xc"], first_qty=rec["first_qty"],
                    armed_at=pd.Timestamp(rec["armed_at"]).isoformat(),
                    deadline=pd.Timestamp(rec["deadline"]).isoformat(), **extra)

    def _held_qty(broker, sym):
        p = broker.get_position(sym)
        return int(p.quantity) if p is not None else 0

    if STG:
        _orig_resolve = worker._resolve_tw2_3slot_candidate
        _orig_eow = worker._execute_or_wait

        def _eow(*, broker, market_data, state, now, macd_snap, direction, signal_id, signal_type,
                 position, result, signal_detected_at=None, budget_multiplier=1.0):
            if not str(signal_id).endswith(":TW2_3SLOT_CONFIRM"):
                return _orig_eow(broker=broker, market_data=market_data, state=state, now=now,
                                 macd_snap=macd_snap, direction=direction, signal_id=signal_id,
                                 signal_type=signal_type, position=position, result=result,
                                 signal_detected_at=signal_detected_at, budget_multiplier=budget_multiplier)
            chop, f = _is_chop(now)
            if not chop:
                return _orig_eow(broker=broker, market_data=market_data, state=state, now=now,
                                 macd_snap=macd_snap, direction=direction, signal_id=signal_id,
                                 signal_type=signal_type, position=position, result=result,
                                 signal_detected_at=signal_detected_at, budget_multiplier=budget_multiplier)
            full = float(budget_multiplier or 1.0)
            out = _orig_eow(broker=broker, market_data=market_data, state=state, now=now,
                            macd_snap=macd_snap, direction=direction, signal_id=signal_id,
                            signal_type=signal_type, position=position, result=result,
                            signal_detected_at=signal_detected_at, budget_multiplier=full * STG)
            if out is None or out.final_state != worker.SignalState.EXECUTED:
                STG_LOG.append(dict(sid=signal_id, dir=direction.value, trigger=None, pe=f[0], rng=f[1], xc=f[2],
                                    first_qty=0, armed_at=now.isoformat(), deadline=now.isoformat(),
                                    ev="FIRST_NOT_EXECUTED"))
                return out
            trig = _trigger_px(CTX["bars_3m"], worker._parse_iso_dt(CTX["flag_bar_ts"]),
                               macd_snap.bar_dt, direction)
            rec = dict(direction=direction, dir=direction.value, signal_id=signal_id, trigger=trig,
                       pe=f[0], rng=f[1], xc=f[2], first_qty=int(out.quantity or 0),
                       symbol=out.target_symbol, rest_mult=full * (1.0 - STG),
                       epoch=int(state.position_epoch or 0) + 1,
                       armed_at=now, deadline=now + timedelta(minutes=ADD_WAIT_MIN))
            if trig is None:
                STG_LOG.append(_pub(rec, ev="NO_TRIGGER"))
                return out
            PEND["rec"] = rec
            STG_LOG.append(_pub(rec, ev="STAGED_FIRST", first_px=float(out.filled_avg_price or 0.0)))
            return out

        def _add_on(*, broker, market_data, state, now, rec, price):
            """돌파 시 나머지 비중 추가 — production order_executor 그대로 사용.
            position=None 으로 넘겨야 BLOCK_ALREADY_HOLDING 을 거치지 않고 매수 레그만 탄다."""
            sym = rec["symbol"]
            before = _held_qty(broker, sym)
            _, quotes = worker._quote_status_for_order(market_data, (sym,))
            if not quotes.get(sym):
                STG_LOG.append(_pub(rec, ev="ADD_NO_QUOTE", at=now.isoformat()))
                return
            out = worker.order_executor.execute_signal(
                broker=broker, direction=rec["direction"], signal_id=rec["signal_id"] + ":ADD",
                quotes=quotes, position=None,
                budget=float(state.budget or 0.0) * float(rec["rest_mult"]),
                processed_signal_ids=frozenset(),
                reconcile_retries=worker.ORDER_FILL_RECONCILE_RETRIES,
                reconcile_delay_sec=worker.ORDER_FILL_RECONCILE_DELAY_SEC,
            )
            added = _held_qty(broker, sym) - before
            if added > 0:
                # 부분청산 비중이 전체 수량 기준이 되도록 초기수량을 갱신하고,
                # 일일 노출 누계에 나머지 비중을 반영한다(진입 순번은 올리지 않는다).
                state.time_window_initial_quantity = int(state.time_window_initial_quantity or 0) + added
                state.x2lite_exposure_used_today = round(
                    float(getattr(state, "x2lite_exposure_used_today", 0.0) or 0.0) + float(rec["rest_mult"]), 6)
            STG_LOG.append(_pub(rec, ev="ADDED" if added > 0 else "ADD_FAILED", at=now.isoformat(),
                                price=price, added_qty=added,
                                add_px=float(getattr(out, "filled_avg_price", 0.0) or 0.0),
                                block=(out.block_reason or None),
                                delay_min=(now - pd.Timestamp(rec["armed_at"]).to_pydatetime()).total_seconds() / 60.0))

        def _resolve_stg(*, broker, market_data, state, now, macd_snap, bars_3m, df_1m, position, result):
            rec = PEND["rec"]
            if rec is not None:
                drop = None
                if now.date() != pd.Timestamp(rec["armed_at"]).date() or now > rec["deadline"]:
                    drop = "EXPIRED"
                elif state.position is None or _held_qty(broker, rec["symbol"]) <= 0:
                    drop = "CANCELLED_FLAT"        # 추가 전에 이미 청산됨
                elif int(state.position_epoch or 0) != rec["epoch"]:
                    drop = "CANCELLED_EPOCH"       # 다른 포지션으로 교체됨
                if drop:
                    PEND["rec"] = None
                    STG_LOG.append(_pub(rec, ev=drop, at=now.isoformat()))
                else:
                    p = _hy_px(now)
                    hit = p is not None and ((p >= rec["trigger"]) if rec["dir"] == "UP_RED" else (p <= rec["trigger"]))
                    if hit:
                        PEND["rec"] = None
                        _add_on(broker=broker, market_data=market_data, state=state, now=now, rec=rec, price=p)
            CTX["bars_3m"] = bars_3m
            CTX["flag_bar_ts"] = state.tw2_3slot_pending_flag_bar_ts
            return _orig_resolve(broker=broker, market_data=market_data, state=state, now=now,
                                 macd_snap=macd_snap, bars_3m=bars_3m, df_1m=df_1m,
                                 position=position, result=result)
        monkeypatch.setattr(worker, "_execute_or_wait", _eow)
        monkeypatch.setattr(worker, "_resolve_tw2_3slot_candidate", _resolve_stg)

