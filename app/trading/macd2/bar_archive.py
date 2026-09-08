"""live 1분봉 프레임 아카이브 — production 신호 재현 전용 (2026-09-08).

■ 왜 필요한가
  ``MarketDataService._df_1m`` 은 **메모리에만** 존재하고 디스크에 남지 않는다
  (``market_data.get_history_df()`` 는 그 프레임의 copy 를 돌려줄 뿐이다).
  그런데 worker 가 어느 시점에 실제로 보유한 1분봉 집합은
  ``merge_incremental_1m()`` 이 매 사이클 **최근 10개만** 가져오고 당일 전체
  페이지워크는 ``bootstrap()`` 시작 1회뿐이라, 그날 프로세스의 시작시각·
  네트워크 성패·재시작 여부에 따라 달라진다. 재시작하면 그대로 사라진다.

  2026-09-08 실측: 그 프레임을 KIS 사후조회로 복원하려 해도 불가능했다.
  프리마켓 시작시각을 1분 단위 61가지로 전수 탐색해도 그날 production
  신호원장을 재현하는 조합이 없었고, 사후조회 입력의 재현율은 42.9% 였다
  (``data/validation/signal_repro_20260908/README.md``). MACD gap 이 크로스
  지점에서 0 바로 위(+20.06 / +138.36)라 프리마켓 봉 몇 개 차이로 플래그가
  통째로 1봉 밀린다.

■ 이 모듈이 하는 일 / 하지 않는 일
  하는 일은 **관측 기록뿐**이다. 진입/청산/신호 판정/MACD 계산에 쓰이는 값을
  하나도 만들지 않고, 하나도 바꾸지 않는다. 여기서 예외가 나도 호출부는
  전부 try/except 로 감싸고 이 모듈 자신도 절대 raise 하지 않는다
  (``record_frame`` 은 실패를 카운터로만 돌려준다).

■ 재현의 핵심 — first_seen_at
  단순 "하루 끝 스냅샷"으로는 부족하다. 필요한 것은 "**시각 T 에 worker 가
  무엇을 보고 있었는가**" 이므로, 각 1분봉이 프레임에 **처음 나타난 시각**을
  같이 남긴다. 그러면 ``load_frame_as_of(date, T)`` 로 그 시점 프레임을 정확히
  복원할 수 있다.

  KIS 가 뒤늦게 값을 정정하는 경우를 위해 같은 봉이 다른 값으로 다시 관측되면
  ``revision`` 을 올려 **새 행을 append** 한다(기존 행은 절대 고치지 않는다).
  따라서 T 시점의 값은 "그 봉의 행들 중 ``last_seen_at <= T`` 인 것 가운데
  가장 최근 revision" 이다.

■ 저장 위치
  ``data_paths.DATA_ROOT / "bar_archive" / hynix_1m_YYYYMMDD.csv``.
  DATA_ROOT 는 ``AI_GAP_DATA_DIR``(Render Persistent Disk) 하위이므로
  재배포/재시작에도 살아남는다 — 컨테이너 로컬에 쓰면 그대로 유실된다
  (``app/utils/data_paths.py`` 참고).
"""
from __future__ import annotations

import csv
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from app.trading.macd2 import config
from app.utils.data_paths import DATA_ROOT

KST = config.KST

#: 아카이브 디렉터리. 테스트/격리 스크립트는 이 모듈 속성을 재지정한다
#: (``ledger.SIGNAL_LEDGER_PATH`` 를 conftest 가 재지정하는 것과 같은 관례).
ARCHIVE_DIR: Path = DATA_ROOT / "bar_archive"

ARCHIVE_COLUMNS = [
    "datetime", "open", "high", "low", "close", "volume",
    "first_seen_at", "last_seen_at", "revision", "source", "worker_instance_id",
]

_OHLCV = ("open", "high", "low", "close", "volume")

_LOCK = threading.RLock()

#: date_ymd -> {datetime_iso: {"ohlcv": tuple, "revision": int, "first_seen_at": str}}
_INDEX: dict[str, dict[str, dict[str, Any]]] = {}
#: 인덱스를 파일에서 이미 복구한 날짜(재시작 후 revision 이 0 으로 되돌아가지 않게)
_INDEX_LOADED: set[str] = set()

#: instance_id 는 **모듈 전역으로 두지 않는다**. 프로세스당 MACD2 Worker 가
#: 1개라는 보장이 없어(2026-09-03 dual-Worker 실사고, commit 790cea6) 전역이면
#: 나중에 시작한 Worker 가 앞선 Worker 의 값을 덮어쓴다 — 아카이브 행의 출처가
#: 조용히 뒤바뀌어 재시작 경계 식별이 무의미해진다. 호출부(MarketDataService)가
#: 자기 인스턴스 속성을 넘긴다.


def archive_path(date_ymd: str) -> Path:
    return ARCHIVE_DIR / f"hynix_1m_{date_ymd}.csv"


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return pd.Timestamp(value).isoformat()
    except Exception:
        return str(value)


def _ohlcv_tuple(row: Any) -> tuple:
    out = []
    for col in _OHLCV:
        try:
            out.append(float(row[col]))
        except Exception:
            out.append(None)
    return tuple(out)


def _load_index(date_ymd: str) -> dict[str, dict[str, Any]]:
    """이미 기록된 행에서 인덱스를 복구한다. 재시작 후에도 revision 이 이어진다."""
    idx: dict[str, dict[str, Any]] = {}
    path = archive_path(date_ymd)
    if not path.exists():
        return idx
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            for raw in csv.DictReader(fh):
                key = str(raw.get("datetime") or "")
                if not key:
                    continue
                try:
                    rev = int(raw.get("revision") or 0)
                except Exception:
                    rev = 0
                prev = idx.get(key)
                if prev is not None and prev["revision"] > rev:
                    continue
                vals = []
                for col in _OHLCV:
                    v = raw.get(col)
                    try:
                        vals.append(float(v) if v not in (None, "") else None)
                    except Exception:
                        vals.append(None)
                idx[key] = {
                    "ohlcv": tuple(vals),
                    "revision": rev,
                    "first_seen_at": str(raw.get("first_seen_at") or ""),
                }
    except Exception:
        # 손상/부분기록 파일이어도 아카이브가 거래를 막아서는 안 된다.
        return idx
    return idx


def _append_rows(date_ymd: str, rows: list[dict[str, Any]]) -> None:
    path = archive_path(date_ymd)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=ARCHIVE_COLUMNS)
        if is_new:
            writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in ARCHIVE_COLUMNS})


def record_frame(
    df_1m: Optional[pd.DataFrame],
    *,
    now: Optional[datetime] = None,
    source: str = "incremental",
    worker_instance_id: Optional[str] = None,
) -> dict[str, int]:
    """worker 가 메모리에서 실제로 보고 있는 프레임을 그대로 아카이브한다.

    ``now`` 의 KST 날짜분만 대상으로 한다(전일 warm-up 은 그날 파일에 이미
    기록돼 있거나 재현에 쓰이지 않는다). 신규 봉과 **값이 바뀐** 봉만 append
    하므로 사이클마다 파일이 커지지 않는다.

    절대 raise 하지 않는다. 실패는 ``errors`` 카운터로만 보고한다.
    """
    stats = {"new": 0, "revised": 0, "unchanged": 0, "errors": 0}
    try:
        if df_1m is None or len(df_1m) == 0:
            return stats
        now = now or datetime.now(KST)
        now_iso = _iso(now)
        date_ymd = now.astimezone(KST).strftime("%Y%m%d")
        wid = str(worker_instance_id or "")

        work = df_1m
        if "datetime" not in work.columns:
            return stats
        day = pd.DatetimeIndex(work["datetime"]).tz_convert(KST).strftime("%Y%m%d")
        work = work.loc[day == date_ymd]
        if work.empty:
            return stats

        with _LOCK:
            if date_ymd not in _INDEX_LOADED:
                _INDEX[date_ymd] = _load_index(date_ymd)
                _INDEX_LOADED.add(date_ymd)
            idx = _INDEX.setdefault(date_ymd, {})

            pending: list[dict[str, Any]] = []
            for _i, row in work.iterrows():
                key = _iso(row["datetime"])
                vals = _ohlcv_tuple(row)
                prev = idx.get(key)
                if prev is None:
                    entry = {"ohlcv": vals, "revision": 0, "first_seen_at": now_iso}
                    idx[key] = entry
                    stats["new"] += 1
                elif prev["ohlcv"] != vals:
                    entry = {
                        "ohlcv": vals,
                        "revision": int(prev["revision"]) + 1,
                        "first_seen_at": prev["first_seen_at"] or now_iso,
                    }
                    idx[key] = entry
                    stats["revised"] += 1
                else:
                    stats["unchanged"] += 1
                    continue
                pending.append({
                    "datetime": key,
                    "open": vals[0], "high": vals[1], "low": vals[2],
                    "close": vals[3], "volume": vals[4],
                    "first_seen_at": entry["first_seen_at"],
                    "last_seen_at": now_iso,
                    "revision": entry["revision"],
                    "source": source,
                    "worker_instance_id": wid,
                })
            if pending:
                _append_rows(date_ymd, pending)
    except Exception:
        stats["errors"] += 1
    return stats


def load_day(date_ymd: str) -> pd.DataFrame:
    """그날 아카이브 전체 행(모든 revision 포함). 없으면 빈 프레임."""
    path = archive_path(date_ymd)
    if not path.exists():
        return pd.DataFrame(columns=ARCHIVE_COLUMNS)
    df = pd.read_csv(path)
    for col in ("datetime", "first_seen_at", "last_seen_at"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
            try:
                df[col] = df[col].dt.tz_convert(KST)
            except Exception:
                pass
    return df


def load_frame_as_of(date_ymd: str, as_of: datetime) -> pd.DataFrame:
    """``as_of`` 시점에 worker 가 실제로 보유했던 1분봉 프레임을 복원한다.

    규칙:
      * ``first_seen_at > as_of`` 인 봉은 그 시점에 **존재하지 않았으므로** 제외
      * 남은 봉은 ``last_seen_at <= as_of`` 인 행들 중 가장 최근 revision 을 채택
        (그 시점까지 반영된 정정만 반영)

    반환 컬럼은 ``market_data._1M_COLUMNS`` 와 동일한 (datetime, open, high,
    low, close, volume) 이고 datetime 은 tz-aware KST 로 정렬돼 있다.
    """
    empty = pd.DataFrame(columns=list(_OHLCV))
    empty.insert(0, "datetime", pd.Series(dtype="datetime64[ns, Asia/Seoul]"))
    df = load_day(date_ymd)
    if df.empty:
        return empty
    ts = pd.Timestamp(as_of)
    if ts.tzinfo is None:
        ts = ts.tz_localize(KST)
    df = df[df["first_seen_at"].notna() & (df["first_seen_at"] <= ts)]
    df = df[df["last_seen_at"].notna() & (df["last_seen_at"] <= ts)]
    if df.empty:
        return empty
    df = (
        df.sort_values(["datetime", "revision", "last_seen_at"])
        .drop_duplicates(subset=["datetime"], keep="last")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    return df[["datetime", *_OHLCV]].reset_index(drop=True)


def stats(date_ymd: str) -> dict[str, Any]:
    """대시보드/진단용 요약. 실패해도 raise 하지 않는다."""
    out: dict[str, Any] = {
        "path": str(archive_path(date_ymd)), "exists": False, "rows": 0,
        "bars": 0, "revisions": 0, "oldest": None, "newest": None,
        "first_observed_at": None, "instances": [],
    }
    try:
        df = load_day(date_ymd)
        out["exists"] = archive_path(date_ymd).exists()
        if df.empty:
            return out
        out["rows"] = int(len(df))
        out["bars"] = int(df["datetime"].nunique())
        out["revisions"] = int((pd.to_numeric(df["revision"], errors="coerce") > 0).sum())
        out["oldest"] = df["datetime"].min().isoformat()
        out["newest"] = df["datetime"].max().isoformat()
        out["first_observed_at"] = df["first_seen_at"].min().isoformat()
        out["instances"] = sorted({str(x) for x in df["worker_instance_id"].dropna().unique()})
    except Exception:
        pass
    return out


def _reset_for_tests() -> None:
    """테스트가 ARCHIVE_DIR 을 옮긴 뒤 프로세스 내 인덱스를 비우기 위한 훅."""
    with _LOCK:
        _INDEX.clear()
        _INDEX_LOADED.clear()
