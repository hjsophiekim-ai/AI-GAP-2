"""TREND-ONLY-2SLOT 하네스 생성. peaklock 하네스에 monkeypatch 만 더한다 (production 무수정)."""
import os, sys

sys.stdout.reconfigure(encoding="utf-8")
SRC = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261008_peaklock/harness/test_zz_wk22.py"
W = os.path.dirname(os.path.abspath(__file__))
DST = W + "/test_zz_tr2.py"
s = open(SRC, encoding="utf-8").read()

# ── 1) VAR 정규화 : TREND2/TREND3/E2SLOT -> UPFASTRS 경로 + 플래그 ──────────
A = '''    PLV = VAR if VAR in ("PEAKLOCK", "PEAKPART") else None
    if PLV:
        VAR = "UPFASTRS"
'''
B = '''    PLV = VAR if VAR in ("PEAKLOCK", "PEAKPART") else None
    if PLV:
        VAR = "UPFASTRS"
    # TREND-ONLY-2SLOT 연구 (2026-10-08b) — VAR 를 UPFASTRS(E) 로 정규화하고
    # 진입 트리거 조건과 일일 진입 한도만 바꾼다. 청산/사이징/슬롯판정은 전부 E 그대로.
    TRND = VAR if VAR in ("TREND2", "TREND3", "E2SLOT") else None
    if TRND:
        VAR = "UPFASTRS"
    # B : CHOP 구조손절 (2026-10-08c). B3 래더는 CHOP 분기 전용이므로
    #     TREND 거래는 자동으로 기존 -1% 손절이 그대로 유지된다.
    STRUCT = (VAR == "STRUCTSTOP")
    if STRUCT:
        VAR = "UPFASTRS"
'''
assert A in s
s = s.replace(A, B, 1)

# ── 2) 상수 : 사전 고정값 (오늘 데이터로 고른 값 없음) ──────────────────────
A = '''    RS_UP, WAIT_MIN, BUF_TICKS, HY_TICK = 1.25, 15, 0.0, 500.0'''
B = '''    RS_UP, WAIT_MIN, BUF_TICKS, HY_TICK = 1.25, 15, 0.0, 500.0
    # TREND-ONLY : 전부 사전 고정 (사용자가 준 범위의 중앙값/하한)
    TR_BOXW, TR_BOXSKIP, TR_HHLL = 30, 3, 5      # 박스 30분(마지막 3분 제외), 고저갱신 lookback 5봉
    TR_N = {"TREND2": 2, "TREND3": 3}.get(TRND, 0)   # 박스 밖 연속 유지 완성봉 수
    TR_WAIT = 45 if TRND in ("TREND2", "TREND3") else WAIT_MIN               # candidate 만료 (30~60 중앙)
    TR_CAP = 2 if TRND else None                     # 하루 최대 진입 수
    NENT = {"n": 0, "d": None}'''
assert A in s
s = s.replace(A, B, 1)

# ── 3) early-pass 봉쇄 (TREND 계열만) : 모든 승인은 candidate 로만 ──────────
A = '''        return bool(c1 and c2 and c3), dict(dist=round(float(dist), 4), c1=bool(c1), c2=bool(c2),'''
B = '''        if TRND in ("TREND2", "TREND3"):
            return False, dict(dist=round(float(dist), 4), c1=bool(c1), c2=bool(c2), c3=bool(c3),
                               up=bool(up), trnd=True)
        return bool(c1 and c2 and c3), dict(dist=round(float(dist), 4), c1=bool(c1), c2=bool(c2),'''
assert A in s
s = s.replace(A, B, 1)

# ── 4) 추세 확인 함수 ───────────────────────────────────────────────────────
A = '''    def _pub(rec, **extra):'''
B = '''    def _cap_tick(now):
        """일일 진입 한도는 **재생일 기준**으로 리셋한다(전일 선재생분을 세면 안 된다)."""
        dt = pd.Timestamp(now).date()
        if NENT["d"] != dt:
            NENT["d"] = dt
            NENT["n"] = 0
        return TR_CAP is None or NENT["n"] < TR_CAP

    def _tr_box(now, direction):
        """candidate 생성 시점의 박스(최근 30분 중 마지막 3분 제외, 완성 1분봉 high/low)."""
        d = _win(now)
        if len(d) < TR_BOXW // 2:
            return None
        w = d.iloc[-TR_BOXW:]
        w = w.iloc[:-TR_BOXSKIP] if len(w) > TR_BOXSKIP else w
        if not len(w):
            return None
        bh, bl = float(w["high"].max()), float(w["low"].min())
        return dict(hi=bh, lo=bl, mid=(bh + bl) / 2.0)

    def _trend_ok(now, rec, bars_3m):
        """진입 조건 — 전부 완성 1분봉/완성 3분봉만 사용 (선행정보 없음).
        (a) 마지막 N 개 완성봉 종가가 모두 박스 밖
        (b) 마지막 완성봉이 직전 5봉 극값을 신호방향으로 갱신
        (c) candidate 생성 이후 종가가 박스 중앙을 반대로 넘은 적 없음
        (d) 3분봉 MACD gap 부호가 신호방향 유지
        """
        box = rec.get("box")
        if box is None:
            return False, None
        up = (rec["dir"] == "UP_RED")
        d = _win(now)
        if len(d) < TR_N + TR_HHLL + 1:
            return False, None
        c = d["close"].to_numpy(float)
        hh = d["high"].to_numpy(float)
        ll = d["low"].to_numpy(float)
        bound = box["hi"] if up else box["lo"]
        last = c[-TR_N:]
        a = bool((last > bound).all()) if up else bool((last < bound).all())
        if up:
            b = bool(hh[-1] > hh[-1 - TR_HHLL:-1].max())
        else:
            b = bool(ll[-1] < ll[-1 - TR_HHLL:-1].min())
        # (c) 박스 중앙 재진입 없음 — candidate 생성 이후 "박스 밖으로 한 번 나갔다가
        #     중앙선을 반대로 되밟은" 적이 있으면 그 candidate 는 왕복으로 보고 탈락.
        #     아직 한 번도 나간 적이 없으면 공허참(아래 (a) 가 어차피 막는다).
        arm = pd.Timestamp(rec["armed_at"])
        since = d[d["datetime"] >= arm]["close"].to_numpy(float)
        out_seen, cc = False, True
        for x in since:
            if (x > bound) if up else (x < bound):
                out_seen = True
            elif out_seen and ((x < box["mid"]) if up else (x > box["mid"])):
                cc = False
                break
        g = _macd_gap_series(bars_3m) if bars_3m is not None and len(bars_3m) else None
        dd = bool(g is not None and len(g) and ((g[-1] > 0) if up else (g[-1] < 0)))
        info = dict(a=a, b=b, c=cc, d=dd, bound=round(bound, 1), px=round(float(c[-1]), 1),
                    nbars=int(len(d)))
        return bool(a and b and cc and dd), info

    def _pub(rec, **extra):'''
assert A in s
s = s.replace(A, B, 1)

# ── 5) candidate 에 박스 저장 + 만료시간 TR_WAIT ────────────────────────────
A = '''                    rec = dict(direction=direction, dir=direction.value, signal_id=sid,
                               flag_bar_dt=flag_bar_dt, trigger=trig, armed_at=now,
                               deadline=now + timedelta(minutes=WAIT_MIN),
                               cache={k: list(v) for k, v in REC["cache"].items()})'''
B = '''                    rec = dict(direction=direction, dir=direction.value, signal_id=sid,
                               flag_bar_dt=flag_bar_dt, trigger=trig, armed_at=now,
                               deadline=now + timedelta(minutes=TR_WAIT),
                               box=(_tr_box(now, direction) if TRND in ("TREND2", "TREND3") else None),
                               cache={k: list(v) for k, v in REC["cache"].items()})'''
assert A in s
s = s.replace(A, B, 1)

# ── 6) 일일 진입 한도 : 승인 자체를 막는다 (슬롯은 진입 때만 소비) ──────────
A = '''        def _eow(*, broker, market_data, state, now, macd_snap, direction, signal_id, signal_type,
                 position, result, signal_detected_at=None, budget_multiplier=1.0):
            sid = str(signal_id)
            is3 = sid.endswith(":TW2_3SLOT_CONFIRM")'''
B = '''        def _eow(*, broker, market_data, state, now, macd_snap, direction, signal_id, signal_type,
                 position, result, signal_detected_at=None, budget_multiplier=1.0):
            sid = str(signal_id)
            is3 = sid.endswith(":TW2_3SLOT_CONFIRM")
            if not _cap_tick(now) and FIRE["on"] is None:
                LOG.append(dict(ev="DAYCAP_BLOCK", t=now.isoformat(), sid=sid,
                                dir=direction.value, n=NENT["n"], armed_at=now.isoformat()))
                state.processed_signal_ids = list(state.processed_signal_ids) + [sid]
                return None'''
assert A in s
s = s.replace(A, B, 1)

# ── 7) 진입 성공 카운트 (즉시진입 경로) ─────────────────────────────────────
A = '''            _out = _orig_eow(budget_multiplier=bm, **kw)
            if _extra > 0 and _out is not None and _out.final_state == worker.SignalState.EXECUTED:'''
B = '''            _out = _orig_eow(budget_multiplier=bm, **kw)
            if _out is not None and _out.final_state == worker.SignalState.EXECUTED:
                NENT["n"] += 1
            if _extra > 0 and _out is not None and _out.final_state == worker.SignalState.EXECUTED:'''
assert A in s
s = s.replace(A, B, 1)

# ── 8) 발사 조건 교체 + 발사 시 카운트 ──────────────────────────────────────
A = '''            if rec is not None:
                p = _hy_px(now)
                hit = p is not None and ((p >= rec["trigger"]) if rec["dir"] == "UP_RED" else (p <= rec["trigger"]))
                if hit:'''
B = '''            _cap_tick(now)
            if rec is not None and TR_CAP is not None and NENT["n"] >= TR_CAP:
                PEND["rec"] = None
                LOG.append(_pub(rec, ev="DAYCAP_DROP", at=now.isoformat()))
                rec = None
            if rec is not None:
                p = _hy_px(now)
                if TRND in ("TREND2", "TREND3"):
                    hit, _ti = _trend_ok(now, rec, bars_3m)
                    if hit:
                        LOG.append(_pub(rec, ev="TREND_OK", at=now.isoformat(), **(_ti or {})))
                else:
                    hit = p is not None and ((p >= rec["trigger"]) if rec["dir"] == "UP_RED" else (p <= rec["trigger"]))
                if hit:'''
assert A in s
s = s.replace(A, B, 1)

A = '''                    ok = bool(out is not None and out.final_state == worker.SignalState.EXECUTED)
                    LOG.append(_pub(rec, ev="FIRED", at=now.isoformat(), price=p, executed=ok,'''
B = '''                    ok = bool(out is not None and out.final_state == worker.SignalState.EXECUTED)
                    # 발사 경로도 결국 _eow -> _rs_eow 를 타므로 거기서 이미 1 증가했다 (중복집계 금지).
                    LOG.append(_pub(rec, ev="FIRED", at=now.isoformat(), price=p, executed=ok,'''
assert A in s
s = s.replace(A, B, 1)

# ── 9) B : CHOP 구조손절 블록 삽입 ──────────────────────────────────────────
ANCH = '        monkeypatch.setattr(worker, "_resolve_tw2_3slot_candidate", _resolve_rb)' + chr(10)
assert ANCH in s, "anchor"
s = s.replace(ANCH, ANCH + open(W + "/block_structstop.txt", encoding="utf-8").read(), 1)

# SSLOG 를 출력 json 에 싣는다
A2 = '                                                     brk=[e for e in LOG if e["armed_at"][:10].replace("-", "") == D],'
assert A2 in s, "json anchor"
s = s.replace(A2, A2 + chr(10) + '                                                     ss=[e for e in SSLOG if e["t"][:10].replace("-", "") == D],', 1)

# ── 10) A : PROBE -> PROMOTE ───────────────────────────────────────────────
s = s.replace('    STRUCT = (VAR == "STRUCTSTOP")',
              '    PROBE = (VAR == "PROBE")' + chr(10) +
              '    if PROBE:' + chr(10) +
              '        VAR = "UPFASTRS"' + chr(10) +
              '    PR_FRAC, PR_WIN = 0.33, 6' + chr(10) +
              '    PRQ, PRLOG = {"rec": None}, []' + chr(10) +
              '    STRUCT = (VAR == "STRUCTSTOP")', 1)
s = s.replace('    TR_N = {"TREND2": 2, "TREND3": 3}.get(TRND, 0)',
              '    TR_N = {"TREND2": 2, "TREND3": 3}.get(TRND, 0) or (2 if PROBE else 0)', 1)

A3 = '            _out = _orig_eow(budget_multiplier=bm, **kw)'
assert A3 in s, "probe anchor1"
s = s.replace(A3, open(W + "/block_probe.txt", encoding="utf-8").read() + A3, 1)

A4 = '            if _out is not None and _out.final_state == worker.SignalState.EXECUTED:' + chr(10) + '                NENT["n"] += 1'
assert A4 in s, "probe anchor2"
s = s.replace(A4, A4 + chr(10) + open(W + "/block_probe2.txt", encoding="utf-8").read().rstrip() + chr(10), 1)

A5 = '            _cap_tick(now)'
assert A5 in s, "probe anchor3"
s = s.replace(A5, A5 + chr(10) + open(W + "/block_probe3.txt", encoding="utf-8").read().rstrip() + chr(10), 1)

A6 = '                                                     ss=[e for e in SSLOG if e["t"][:10].replace("-", "") == D],'
assert A6 in s, "probe anchor4"
s = s.replace(A6, A6 + chr(10) + '                                                     pr=[e for e in PRLOG if e["t"][:10].replace("-", "") == D],', 1)

open(DST, "w", encoding="utf-8").write(s)
print("wrote", DST, len(s), "bytes")
