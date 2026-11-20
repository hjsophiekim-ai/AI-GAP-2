"""KIS 1분봉 시각 정규화 -- 모든 1분봉 수집·로드 경로가 쓰는 **단일** 함수 (2026-11-20).

■ 왜 필요한가 (2026-11-19 실사고)
  수능일(개장 10:00) 하루 동안 KIS 당일 분봉(inquire-time-itemchartprice, NX)의
  ``stck_cntg_hour`` 가 ``HHMM30`` 으로 왔다. 그대로 ``datetime`` 으로 쓰던 MACD2 는
  3분봉 완성 판정(구성 1분봉 ``HH:MM:00`` 3개 존재)에서 당일 봉을 **전부** 탈락시켜
  MACD 가 전일 19:57 봉에 하루 종일 멈췄다 -- 플래그 0, 거래 0, 경보 0.

  Render 디스크 아카이브 실측(first_seen − 라벨 분 시작): 11/18 [-9.2,-0.4,1.8,4.4]초,
  11/20 [-0.5,-0.4,1.8,4.3]초, 11/19 [25.2,29.5,32.0,34.6]초. 정상일에도 봉은 자기
  라벨 시각 무렵 처음 들어와 이후 계속 갱신되고(진행 중 봉 수신은 원래 동작),
  11/19 의 ``HH:MM:30`` 봉도 **라벨 시각** 무렵 처음 들어왔다. 즉 이름표만 바뀐 게 아니라
  봉 구간 자체가 ``[HH:MM:30, HH:MM+1:30)`` 으로 30초 밀렸을 가능성이 높다.

■ 이 모듈이 하는 일
  * ``datetime`` 을 KST 로 통일하고 **분 단위로 내림**한다(이력 연속성 유지).
  * KIS 가 준 원래 라벨은 ``raw_bar_ts`` 컬럼에 보존한다. 3분봉 완성 판정은 내림한
    시각이 아니라 이 원래 라벨 + 60초로 한다(``signal_engine.resample_completed_3m``).
  * 같은 분이 여러 행이면 **입력 순서상 마지막** 행 하나만 남긴다. 호출부는 입력 순서가
    "나중에 받은 것이 뒤" 가 되도록 넘긴다(concat 순서 = base → backfill → live).
  * 시각 문자열 길이·형식이 예상과 다르면 해석 가능한 한 **버리지 않고** 경고를 남긴다.
    해석 자체가 불가능한 행만 버리고 그 수를 보고한다.

  이 모듈은 값을 만들거나 바꾸지 않는다(OHLCV 그대로). 시각 표기만 맞춘다.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

#: KIS 가 준 원래 봉 라벨(내림 전). 완성 판정의 기준.
RAW_TS_COL = "raw_bar_ts"
#: 이 행의 현재 값을 담은 KIS 요청이 **시작된** 시각. 이 시각 이전 데이터만 들어 있다.
#: 디스크/과거 로드처럼 모르면 NaT -- 완성 판정은 시각 조건만 본다.
RECEIVED_AT_COL = "received_at"

_WARN_INTERVAL_SEC = 60.0
_warn_lock = threading.Lock()
_last_warn_at: dict[str, float] = {}


@dataclass
class NormalizeReport:
    source: str = ""
    rows_in: int = 0
    rows_out: int = 0
    nonzero_second_rows: int = 0
    nonzero_second_examples: list[str] = field(default_factory=list)
    naive_tz_rows: int = 0
    unparseable_rows: int = 0
    collapsed_duplicate_rows: int = 0
    format_issues: dict[str, int] = field(default_factory=dict)

    def add_issue(self, code: str, n: int = 1) -> None:
        self.format_issues[code] = self.format_issues.get(code, 0) + n

    @property
    def anomalous(self) -> bool:
        return bool(self.nonzero_second_rows or self.naive_tz_rows or self.unparseable_rows
                    or self.format_issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "rows_in": self.rows_in, "rows_out": self.rows_out,
            "nonzero_second_rows": self.nonzero_second_rows,
            "nonzero_second_examples": list(self.nonzero_second_examples),
            "naive_tz_rows": self.naive_tz_rows, "unparseable_rows": self.unparseable_rows,
            "collapsed_duplicate_rows": self.collapsed_duplicate_rows,
            "format_issues": dict(self.format_issues),
        }


def _warn_throttled(source: str, msg: str, *args: Any) -> None:
    now = _time.monotonic()
    with _warn_lock:
        last = _last_warn_at.get(source)
        if last is not None and now - last < _WARN_INTERVAL_SEC:
            return
        _last_warn_at[source] = now
    logger.warning(msg, *args)


def parse_kis_minute_stamp(date_raw: Any, time_raw: Any) -> tuple[Optional[datetime], Optional[str]]:
    """KIS ``stck_bsop_date`` + ``stck_cntg_hour`` -> (KST datetime **내림 전**, 형식이슈코드).

    정상은 ``YYYYMMDD`` + ``HHMMSS``. 길이가 다르면 해석 가능한 범위에서 살리고
    이슈 코드를 돌려준다(``HHMM_4DIGIT`` / ``HMMSS_5DIGIT`` / ``HMM_3DIGIT`` /
    ``TRUNCATED_GT6``). 해석 불가면 ``(None, 코드)``.
    """
    d = "".join(ch for ch in str(date_raw or "") if ch.isdigit())
    t = "".join(ch for ch in str(time_raw or "") if ch.isdigit())
    if len(d) != 8:
        return None, "BAD_DATE"
    issue: Optional[str] = None
    if len(t) == 6:
        pass
    elif len(t) == 4:
        t, issue = t + "00", "HHMM_4DIGIT"
    elif len(t) == 5:
        t, issue = t.zfill(6), "HMMSS_5DIGIT"
    elif len(t) == 3:
        t, issue = "0" + t + "00", "HMM_3DIGIT"
    elif len(t) > 6:
        t, issue = t[:6], "TRUNCATED_GT6"
    else:
        return None, "EMPTY_OR_SHORT_TIME"
    try:
        return datetime.strptime(d + t, "%Y%m%d%H%M%S").replace(tzinfo=KST), issue
    except ValueError:
        return None, "UNPARSEABLE"


def _to_kst_series(values: pd.Series, report: NormalizeReport) -> pd.Series:
    """임의의 시각 Series -> tz-aware KST Series. tz naive 는 KST 로 간주(경고 집계)."""
    try:
        parsed = pd.to_datetime(values, errors="coerce")
    except (ValueError, TypeError):
        parsed = None
    if parsed is not None and isinstance(parsed.dtype, pd.DatetimeTZDtype):
        return parsed.dt.tz_convert(KST)
    if parsed is not None and pd.api.types.is_datetime64_dtype(parsed.dtype):
        report.naive_tz_rows += int(parsed.notna().sum())
        return parsed.dt.tz_localize(KST)
    # 혼합(naive/aware 섞임, 다른 offset 섞임) -- 원소 단위로 처리
    out = []
    for v in values:
        try:
            ts = pd.Timestamp(v)
        except (ValueError, TypeError):
            out.append(pd.NaT)
            continue
        if pd.isna(ts):
            out.append(pd.NaT)
            continue
        if ts.tzinfo is None:
            report.naive_tz_rows += 1
            ts = ts.tz_localize(KST)
        else:
            ts = ts.tz_convert(KST)
        out.append(ts)
    return pd.Series(pd.to_datetime(pd.Series(out, index=values.index, dtype=object), utc=True),
                     index=values.index).dt.tz_convert(KST)


def normalize_minute_frame(
    df: Optional[pd.DataFrame], *, source: str, received_at: Optional[datetime] = None,
) -> tuple[Optional[pd.DataFrame], NormalizeReport]:
    """1분봉 프레임 정규화(멱등). 반환 프레임: ``datetime``(KST, 분 내림) 오름차순,
    분당 1행(입력 순서상 마지막), ``raw_bar_ts`` 보존, ``received_at`` 은 인자로 주면
    새로 채우고(이번 KIS 응답), 없으면 기존 값을 유지한다.

    이미 ``raw_bar_ts`` 가 있는 행은 그 값을 원래 라벨로 유지한다(재정규화해도 원래
    라벨을 잃지 않는다)."""
    report = NormalizeReport(source=source)
    if df is None or df.empty or "datetime" not in df.columns:
        if df is not None:
            report.rows_in = report.rows_out = int(len(df))
        return df, report
    report.rows_in = int(len(df))
    work = df.copy().reset_index(drop=True)
    stamps = _to_kst_series(work["datetime"], report)
    if RAW_TS_COL in work.columns:
        raw_prev = _to_kst_series(work[RAW_TS_COL], NormalizeReport())
        raw = raw_prev.where(raw_prev.notna(), stamps)
    else:
        raw = stamps
    bad = stamps.isna()
    if bool(bad.any()):
        report.unparseable_rows += int(bad.sum())
    work["datetime"] = stamps.dt.floor("min")
    work[RAW_TS_COL] = raw
    if received_at is not None:
        work[RECEIVED_AT_COL] = pd.Timestamp(received_at).tz_convert(KST) \
            if pd.Timestamp(received_at).tzinfo is not None else pd.Timestamp(received_at).tz_localize(KST)
    elif RECEIVED_AT_COL in work.columns:
        work[RECEIVED_AT_COL] = _to_kst_series(work[RECEIVED_AT_COL], NormalizeReport())
    work = work.loc[~bad]
    off = work[RAW_TS_COL] - work["datetime"]
    nz = off.notna() & (off != pd.Timedelta(0))
    report.nonzero_second_rows = int(nz.sum())
    if report.nonzero_second_rows:
        report.nonzero_second_examples = [
            pd.Timestamp(x).isoformat() for x in work.loc[nz, RAW_TS_COL].head(3).tolist()]
    work["_seq"] = range(len(work))
    work = work.sort_values(["datetime", "_seq"], kind="mergesort")
    before = len(work)
    work = work.drop_duplicates(subset=["datetime"], keep="last")
    report.collapsed_duplicate_rows = before - len(work)
    work = work.drop(columns=["_seq"]).reset_index(drop=True)
    report.rows_out = int(len(work))
    if report.anomalous:
        _warn_throttled(
            source,
            "[1M-NORMALIZE][%s] 비정상 1분봉 시각 흡수: 초≠00 %d행(예 %s), tz-naive %d행, "
            "해석불가 %d행, 형식이슈 %s, 분중복 정리 %d행",
            source, report.nonzero_second_rows, report.nonzero_second_examples,
            report.naive_tz_rows, report.unparseable_rows, report.format_issues,
            report.collapsed_duplicate_rows,
        )
    return work, report


def candles_to_minute_frame(
    candles: list[dict], *, source: str, received_at: Optional[datetime] = None,
    default_date: str = "",
) -> tuple[pd.DataFrame, NormalizeReport]:
    """KIS 분봉 dict 목록(``kis_client.get_minute_candles*`` 반환형) -> 정규화 프레임."""
    rows: list[dict[str, Any]] = []
    report_issues: dict[str, int] = {}
    unparseable = 0
    for c in candles or []:
        dt, issue = parse_kis_minute_stamp(c.get("date") or default_date, c.get("time"))
        if issue:
            report_issues[issue] = report_issues.get(issue, 0) + 1
        if dt is None:
            unparseable += 1
            continue
        rows.append({
            "datetime": dt,
            "open": float(c.get("open") or 0), "high": float(c.get("high") or 0),
            "low": float(c.get("low") or 0), "close": float(c.get("close") or 0),
            "volume": int(c.get("volume") or 0),
        })
    if not rows:
        rep = NormalizeReport(source=source, rows_in=len(candles or []), unparseable_rows=unparseable,
                              format_issues=report_issues)
        if rep.anomalous:
            _warn_throttled(source, "[1M-NORMALIZE][%s] 해석 가능한 분봉 없음: %s", source, rep.as_dict())
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]), rep
    # 같은 분이 둘이면 원래 라벨이 늦은 쪽(더 최근 데이터)이 남도록 원래 라벨 순으로 정렬해 넘긴다.
    frame = pd.DataFrame(rows).sort_values("datetime", kind="mergesort").reset_index(drop=True)
    out, rep = normalize_minute_frame(frame, source=source, received_at=received_at)
    rep.rows_in = len(candles or [])
    rep.unparseable_rows += unparseable
    for k, v in report_issues.items():
        rep.add_issue(k, v)
    if (unparseable or report_issues) and not rep.nonzero_second_rows:
        _warn_throttled(source + ":fmt", "[1M-NORMALIZE][%s] 분봉 시각 형식 이슈: %s",
                        source, rep.as_dict())
    return out, rep


def page_cursor_before(frame: pd.DataFrame) -> str:
    """역방향 페이지 커서(HHMMSS): 프레임에서 가장 이른 봉의 **원래 라벨** − 1분.

    정상일(라벨 HH:MM:00)에는 기존 ``(oldest - 1분)`` 과 같은 문자열이다. 라벨이
    HH:MM:30 인 날 내림한 시각으로 커서를 만들면 (oldest−1분):30 행을 건너뛰어 페이지
    경계마다 1분씩 구멍이 난다 -- 그래서 원래 라벨을 쓴다."""
    oldest_idx = frame["datetime"].idxmin()
    if RAW_TS_COL in frame.columns and pd.notna(frame.at[oldest_idx, RAW_TS_COL]):
        base = pd.Timestamp(frame.at[oldest_idx, RAW_TS_COL])
    else:
        base = pd.Timestamp(frame.at[oldest_idx, "datetime"])
    return (base - timedelta(minutes=1)).strftime("%H%M%S")


def strip_aux_columns(df: pd.DataFrame) -> pd.DataFrame:
    """보조 컬럼(raw_bar_ts / received_at)을 뺀 사본 -- 고정 스키마 CSV 쓰기용."""
    drop = [c for c in (RAW_TS_COL, RECEIVED_AT_COL) if c in df.columns]
    return df.drop(columns=drop) if drop else df
