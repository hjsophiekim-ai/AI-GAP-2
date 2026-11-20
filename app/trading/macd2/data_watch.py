"""데이터 진행 감시 · 장 전 자가진단 · 장 마감 대사 (2026-11-20, 11/19 무플래그 실사고).

supervisor 는 원래 worker 생존(DEAD/STALLED)과 history 워치독(1분봉 프레임이 전진하는가)
만 봤다. 11/19 에는 1분봉이 계속 들어와 프레임은 전진했지만 3분봉 완성 판정에서 당일 봉이
전부 탈락해 MACD 가 전일 19:57 봉에 멈췄고, 어느 감시도 울리지 않았다. 여기서는
**결과물(완성 3분봉·평가 횟수)** 이 진행하는지를 본다.

정규장(09:00~) 감시 조건 -- 자동매매 ON 일 때만:
  (a) STALE_3M        last_completed_3m_bar_at 이 오늘이 아니거나 9분 이상 정체 (09:00~15:20)
  (b) CONTRACT        HISTORY_GAP / DATA_CONTRACT_VIOLATION 6분 이상 지속,
                      BAR_ALIGNMENT_ANOMALY 는 즉시
  (c) NO_3M_BY_0930   09:30 까지 당일 완성 3분봉 0개
  (d) NO_EVAL_BY_1100 11:00 까지 당일 confirmed crossover **평가** 0회 (플래그 0개는 정상일 수 있음)
11/19 재현 시 (a) 가 09:00 첫 tick 에 성립 -> 즉시 경보(09:00~09:01).

경보 경로는 supervisor.record_event(alert=True): logger.error + supervisor 원장 + 웹훅.
이 모듈은 주문·신호 판정에 관여하지 않는다(관측·경보 전용). 어떤 예외도 밖으로 내지 않는다.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from app.logger import logger
from app.trading.macd2 import config, ledger, state_store

KST = config.KST

REGULAR_OPEN = time(9, 0)
STALE_CHECK_END = time(15, 20)
STALE_3M_ALERT_MIN = 9.0
CONTRACT_PERSIST_MIN = 6.0
NO_3M_DEADLINE = time(9, 30)
NO_EVAL_DEADLINE = time(11, 0)
WATCH_END = time(15, 30)

SELFCHECK_AT = time(8, 40)
SELFCHECK_UNTIL = time(8, 58)
SELFCHECK_MIN_PRIOR_BARS = 200
REPORT_AT = time(15, 40)
REPORT_UNTIL = time(18, 0)
REPORT_MIN_3M = 100

SELFCHECK_NAME = "macd2_preopen_selfcheck.json"
REPORT_STATE_NAME = "macd2_daily_report_last.json"
REPORT_LEDGER_NAME = "macd2_daily_report.csv"
REPORT_COLUMNS = [
    "trading_date", "generated_at", "worker_running", "today_1m_count", "today_completed_3m_count",
    "crossover_evaluations", "flags", "orders", "contract_violation_events", "alignment_anomaly_events",
    "nonzero_second_1m_count", "verdict", "problems",
]


def selfcheck_path() -> Path:
    return Path(state_store.STATE_DIR_PATH) / SELFCHECK_NAME


def report_state_path() -> Path:
    return Path(state_store.STATE_DIR_PATH) / REPORT_STATE_NAME


def report_ledger_path() -> Path:
    return Path(ledger.LOGS_DIR_PATH) / REPORT_LEDGER_NAME


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _parse(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        out = pd.Timestamp(ts)
        if out.tzinfo is None:
            out = out.tz_localize(KST)
        return out.to_pydatetime()
    except Exception:
        return None


def _minutes_since(ts: Any, now: datetime) -> Optional[float]:
    dt = _parse(ts)
    return None if dt is None else (now - dt).total_seconds() / 60.0


# ── (3층) 정규장 데이터 진행 판정 ───────────────────────────────────────────

def assess(state: Any, now: datetime) -> dict[str, dict]:
    """활성 조건 {key: detail}. 비어 있으면 정상. 순수 함수(상태 변경 없음)."""
    out: dict[str, dict] = {}
    if not getattr(state, "auto_trade_on", False):
        return out
    kst = now.astimezone(KST)
    if kst.weekday() >= 5:
        return out
    t = kst.time()
    if not (REGULAR_OPEN <= t < WATCH_END):
        return out
    today = kst.date()
    hint = "휴장일이면 무시. 수능일 등 개장시간 변경일이면 개장 전까지 정상일 수 있음."
    last = _parse(getattr(state, "last_completed_3m_bar_at", None))
    last_end = last + timedelta(minutes=3) if last else None
    if t < STALE_CHECK_END:
        ref = last_end
        open_dt = kst.replace(hour=REGULAR_OPEN.hour, minute=0, second=0, microsecond=0)
        if ref is not None and ref.astimezone(KST).date() == today and ref < open_dt:
            ref = open_dt      # 프리마켓 마지막 봉(08:48) ~ 09:00 padding 은 정체로 보지 않는다
        stale = None if ref is None else round((now - ref).total_seconds() / 60.0, 1)
        not_today = last is None or last.astimezone(KST).date() != today
        if not_today or (stale is not None and stale >= STALE_3M_ALERT_MIN):
            out["STALE_3M"] = {
                "last_completed_3m_bar_at": getattr(state, "last_completed_3m_bar_at", None),
                "not_today": not_today, "stale_min": stale,
                "history_newest_at": getattr(state, "history_newest_at", None),
                "today_1m_bar_count": getattr(state, "today_1m_bar_count", None), "hint": hint,
            }
    dc = dict(getattr(state, "data_contract", None) or {})
    status = dc.get("status")
    if dc.get("trading_date") == kst.strftime("%Y%m%d"):
        if status == "BAR_ALIGNMENT_ANOMALY":
            out["BAR_ALIGNMENT_ANOMALY"] = {"since": dc.get("since"), "reasons": dc.get("reasons"),
                                            "metrics": _brief(dc.get("metrics"))}
        if status == "DATA_CONTRACT_VIOLATION":
            mins = _minutes_since(dc.get("since"), now)
            if mins is not None and mins >= CONTRACT_PERSIST_MIN:
                out["DATA_CONTRACT_VIOLATION"] = {"since": dc.get("since"), "minutes": round(mins, 1),
                                                  "reasons": dc.get("reasons"), "metrics": _brief(dc.get("metrics"))}
        gap_mins = _minutes_since(dc.get("history_gap_since"), now)
        if gap_mins is not None and gap_mins >= CONTRACT_PERSIST_MIN:
            out["HISTORY_GAP_PERSIST"] = {"since": dc.get("history_gap_since"), "minutes": round(gap_mins, 1)}
    if t >= NO_3M_DEADLINE:
        n3 = getattr(state, "today_completed_3m_count", None)
        last_today = last is not None and last.astimezone(KST).date() == today
        if not last_today and not n3:
            out["NO_3M_BY_0930"] = {"today_completed_3m_count": n3, "hint": hint}
    if t >= NO_EVAL_DEADLINE:
        n_eval = len(getattr(state, "evaluated_bar_ts_today", None) or [])
        if n_eval == 0:
            out["NO_EVAL_BY_1100"] = {"crossover_evaluations": 0, "hint": hint}
    return out


def _brief(m: Any) -> dict:
    m = dict(m or {})
    keys = ("today_1m_count", "nonzero_second_count", "trailing_nonzero_run", "offset_examples",
            "coverage", "stale_3m_min", "last_completed_3m_bar_at")
    return {k: m.get(k) for k in keys if k in m}


class DataWatchTracker:
    """조건별 경보/반복/해제. 조건 자체가 지속시간 기준을 품고 있으므로 성립 즉시 경보."""

    def __init__(self, record_event: Callable[..., None], repeat_sec: Optional[float] = None) -> None:
        self._record = record_event
        self.repeat_sec = float(repeat_sec if repeat_sec is not None else config.SUPERVISOR_ALERT_REPEAT_SEC)
        self.active: dict[str, dict] = {}

    def observe(self, conditions: dict[str, dict], mode: str, now: datetime) -> list[str]:
        fired: list[str] = []
        for key, detail in (conditions or {}).items():
            st = self.active.get(key)
            if st is None:
                st = self.active[key] = {"since": now, "last_alert_at": None, "alerts": 0}
            due = st["last_alert_at"] is None or (now - st["last_alert_at"]).total_seconds() >= self.repeat_sec
            if due:
                st["last_alert_at"] = now
                st["alerts"] += 1
                self._record(f"DATA_{key}", mode=mode, verdict=key, alert=True, now=now,
                             detail={**detail, "alert_no": st["alerts"], "since": st["since"].isoformat()})
                fired.append(key)
        for key in [k for k in self.active if k not in (conditions or {})]:
            st = self.active.pop(key)
            self._record(f"DATA_{key}_RESOLVED", level="WARNING", mode=mode, verdict=key, now=now,
                         alert=st["alerts"] > 0,
                         detail={"since": st["since"].isoformat(),
                                 "duration_min": round((now - st["since"]).total_seconds() / 60.0, 1)})
        return fired


# ── (4층) 08:40 장 전 자가진단 ──────────────────────────────────────────────

def _token_status(client: Any) -> dict:
    """워커 자기 토큰만 본다. 절대 발급/갱신하지 않는다(속성만 읽음)."""
    if client is None:
        return {"ok": False, "reason": "NO_CLIENT"}
    tok = getattr(client, "_token", "")
    exp = getattr(client, "_token_expires_at", None)
    if not tok or not isinstance(exp, datetime):
        return {"ok": False, "reason": "NO_TOKEN_IN_MEMORY"}
    left_h = (exp - datetime.now()).total_seconds() / 3600.0
    # 장 마감(15:40)까지 버텨야 한다 -- 08:40 기준 7시간
    return {"ok": left_h >= 7.0, "hours_left": round(left_h, 2),
            "reason": None if left_h >= 7.0 else "TOKEN_EXPIRES_BEFORE_CLOSE"}


def run_preopen_selfcheck(service: Any, state: Any, now: datetime, *, heartbeat_age_sec: Optional[float]) -> dict:
    kst = now.astimezone(KST)
    today = kst.strftime("%Y%m%d")
    checks: dict[str, dict] = {}
    md = getattr(service, "_market_data", None)
    worker = getattr(service, "_worker", None)
    running = bool(getattr(state, "auto_trade_on", False) and worker is not None and worker.is_alive())
    checks["worker_running"] = {"ok": running, "auto_trade_on": bool(getattr(state, "auto_trade_on", False))}
    # 전일 warm-up
    try:
        df = md.get_history_df() if md is not None else None
        if df is None or df.empty:
            checks["prior_day_warmup"] = {"ok": False, "reason": "NO_HISTORY"}
            checks["time_format"] = {"ok": False, "reason": "NO_HISTORY"}
        else:
            dt = pd.to_datetime(df["datetime"], errors="coerce")
            ymd = dt.dt.tz_convert(KST).dt.strftime("%Y%m%d")
            prior = dt[ymd < today]
            prior_last = prior.max() if len(prior) else None
            n_prior = int((ymd == (ymd[ymd < today].max() if len(prior) else "")).sum()) if len(prior) else 0
            checks["prior_day_warmup"] = {
                "ok": n_prior >= SELFCHECK_MIN_PRIOR_BARS and prior_last is not None
                and prior_last.tz_convert(KST).time() >= time(15, 0),
                "bars": n_prior, "last": None if prior_last is None else prior_last.isoformat()}
            from app.utils.minute_bars import RAW_TS_COL
            if RAW_TS_COL in df.columns:
                raw = pd.to_datetime(df[RAW_TS_COL], errors="coerce")
                nz = int(((raw - dt) != pd.Timedelta(0)).sum())
            else:
                nz = int((dt.dt.second != 0).sum())
            checks["time_format"] = {"ok": nz == 0, "nonzero_second_rows": nz}
    except Exception as exc:
        checks["prior_day_warmup"] = {"ok": False, "reason": repr(exc)[:200]}
    # 토큰 (워커 자기 토큰, 발급 금지)
    try:
        client = getattr(md, "_history_kis_client", None) if md is not None else None
        checks["token"] = _token_status(client)
    except Exception as exc:
        checks["token"] = {"ok": False, "reason": repr(exc)[:200]}
    # 감시 스레드 heartbeat
    try:
        hist_alive = bool(md.history_updater_alive()) if md is not None else False
    except Exception:
        hist_alive = False
    checks["history_updater"] = {"ok": hist_alive}
    checks["worker_heartbeat"] = {"ok": heartbeat_age_sec is not None and heartbeat_age_sec <= 60.0,
                                  "age_sec": heartbeat_age_sec}
    failed = [k for k, v in checks.items() if not v.get("ok")]
    return {"trading_date": today, "ran_at": now.isoformat(), "ok": not failed, "failed": failed,
            "checks": checks}


# ── (5층) 15:40 당일 대사 ───────────────────────────────────────────────────

def _count_today_rows(path: Path, today_dash: str, today_compact: str, *, col: str) -> list[dict]:
    if not path.exists():
        return []
    out = []
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                v = str(row.get(col) or "")
                if v.startswith(today_dash) or v.startswith(today_compact):
                    out.append(row)
    except Exception:
        return []
    return out


def build_daily_report(service: Any, state: Any, now: datetime) -> dict:
    kst = now.astimezone(KST)
    today_c, today_d = kst.strftime("%Y%m%d"), kst.strftime("%Y-%m-%d")
    sig = _count_today_rows(Path(ledger.SIGNAL_LEDGER_PATH), today_d, today_c, col="trading_date")
    flags = len({str(r.get("signal_id") or "").split(":")[0] for r in sig if r.get("signal_id")})
    exe = _count_today_rows(Path(ledger.EXECUTION_LEDGER_PATH), today_d, today_c, col="timestamp")
    orders = sum(1 for r in exe if str(r.get("side") or "").upper() in ("BUY", "SELL"))
    dc = dict(getattr(state, "data_contract", None) or {})
    dc_today = dc.get("trading_date") == today_c
    worker = getattr(service, "_worker", None)
    rep = {
        "trading_date": today_c, "generated_at": now.isoformat(),
        "worker_running": bool(worker is not None and worker.is_alive()),
        "today_1m_count": getattr(state, "today_1m_bar_count", None),
        "today_completed_3m_count": getattr(state, "today_completed_3m_count", None),
        "crossover_evaluations": len(getattr(state, "evaluated_bar_ts_today", None) or []),
        "flags": flags, "orders": orders,
        "contract_violation_events": int(dc.get("violation_events") or 0) if dc_today else 0,
        "alignment_anomaly_events": int(dc.get("align_events") or 0) if dc_today else 0,
        "nonzero_second_1m_count": (dc.get("metrics") or {}).get("nonzero_second_count") if dc_today else None,
    }
    problems = []
    if not getattr(state, "auto_trade_on", False) and rep["crossover_evaluations"] == 0:
        problems.append("AUTO_TRADE_OFF_ALL_DAY")
    if flags == 0:
        problems.append("ZERO_FLAGS")
    n3 = rep["today_completed_3m_count"] or 0
    if n3 < REPORT_MIN_3M:
        problems.append(f"FEW_COMPLETED_3M:{n3}<{REPORT_MIN_3M}")
    if rep["contract_violation_events"] or rep["alignment_anomaly_events"]:
        problems.append("DATA_CONTRACT_EVENTS")
    rep["verdict"] = "OK" if not problems else "CHECK"
    rep["problems"] = problems
    return rep


def append_report_row(rep: dict) -> None:
    path = report_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=REPORT_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({**rep, "problems": ";".join(rep.get("problems") or [])})


# ── supervisor tick 에서 부르는 스케줄 ──────────────────────────────────────

def run_scheduled(service: Any, state: Any, now: datetime, *, record_event: Callable[..., None],
                  heartbeat_age_sec: Optional[float]) -> dict:
    """하루 1회 작업(08:40 자가진단, 15:40 대사). 결과는 별도 JSON 파일에 남긴다
    (runtime state 에 쓰면 worker 의 다음 저장에 덮인다)."""
    done: dict[str, Any] = {}
    kst = now.astimezone(KST)
    if kst.weekday() >= 5:
        return done
    today = kst.strftime("%Y%m%d")
    t = kst.time()
    mode = str(getattr(state, "mode", "") or "")
    try:
        if SELFCHECK_AT <= t < SELFCHECK_UNTIL and _read_json(selfcheck_path()).get("trading_date") != today:
            res = run_preopen_selfcheck(service, state, now, heartbeat_age_sec=heartbeat_age_sec)
            _write_json(selfcheck_path(), res)
            alert = (not res["ok"]) and bool(getattr(state, "auto_trade_on", False))
            record_event("PREOPEN_SELFCHECK_" + ("OK" if res["ok"] else "FAILED"),
                         level="INFO" if res["ok"] else "WARNING", mode=mode, alert=alert, now=now,
                         detail={"failed": res["failed"], "checks": res["checks"]})
            done["selfcheck"] = res
    except Exception as exc:
        logger.exception("[MACD2][DATA_WATCH] 자가진단 실패")
        record_event("PREOPEN_SELFCHECK_ERROR", level="WARNING", mode=mode, alert=True, now=now,
                     detail={"error": repr(exc)[:300]})
    try:
        if REPORT_AT <= t < REPORT_UNTIL and _read_json(report_state_path()).get("trading_date") != today:
            rep = build_daily_report(service, state, now)
            _write_json(report_state_path(), rep)
            append_report_row(rep)
            alert = rep["verdict"] != "OK" and "AUTO_TRADE_OFF_ALL_DAY" not in rep["problems"]
            record_event("DAILY_RECONCILE_" + rep["verdict"], level="INFO" if not alert else "WARNING",
                         mode=mode, alert=alert, now=now, detail=rep)
            done["report"] = rep
    except Exception as exc:
        logger.exception("[MACD2][DATA_WATCH] 대사 리포트 실패")
        record_event("DAILY_RECONCILE_ERROR", level="WARNING", mode=mode, alert=True, now=now,
                     detail={"error": repr(exc)[:300]})
    return done


def banner_status(now: Optional[datetime] = None, state: Any = None) -> list[str]:
    """UI 상단 빨간 배너 문구. 없으면 빈 목록."""
    now = now or datetime.now(KST)
    today = now.astimezone(KST).strftime("%Y%m%d")
    msgs = []
    sc = _read_json(selfcheck_path())
    if sc.get("trading_date") == today and not sc.get("ok", True):
        msgs.append(f"장 전 자가진단 실패: {', '.join(sc.get('failed') or [])} ({sc.get('ran_at', '')[11:19]})")
    if state is not None:
        dc = dict(getattr(state, "data_contract", None) or {})
        if dc.get("trading_date") == today and dc.get("status") not in (None, "OK"):
            msgs.append(f"1분봉 입력 이상 [{dc.get('status')}] -- 신규 자동진입 차단 중 "
                        f"(since {str(dc.get('since') or '')[11:19]}): {'; '.join(dc.get('reasons') or [])}")
        for key, detail in assess(state, now).items():
            if key in ("BAR_ALIGNMENT_ANOMALY", "DATA_CONTRACT_VIOLATION"):
                continue
            msgs.append(f"데이터 진행 경보 [{key}] {detail.get('hint', '')}".strip())
    rep = _read_json(report_state_path())
    if rep.get("trading_date") == today and rep.get("verdict") not in (None, "OK"):
        msgs.append(f"당일 대사 확인 필요: {', '.join(rep.get('problems') or [])}")
    return msgs
