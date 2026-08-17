"""
minute_bar_archiver.py — 하이닉스(000660)/레버리지(0193T0)/인버스(0197X0) 1분봉을
날짜별 replay_<date>_{hynix,long,inverse}_1m.csv로 영구 저장하는 공용 로직.

SK MACD2와 MU_MACD는 이 세 종목을 동일하게 사용한다(MU_MACD가 macd2.config의
LONG_SYMBOL/INVERSE_SYMBOL을 그대로 import) — 그래서 시간대별 최적거래 필터를
검증할 시장 데이터는 두 모듈에 대해 별도로 모을 필요 없이 이 한 세트로 충분하다.

Render 배포는 컨테이너 로컬 파일시스템이 기본 ephemeral이라(docs/deploy_render.md),
반드시 app.utils.data_paths.CACHE_DIR(AI_GAP_DATA_DIR 환경변수 → Render Persistent
Disk 마운트 경로)를 통해서만 저장한다 — 이 파일 안에서 "data/cache"를 직접
하드코딩하지 않는다.

app/ui/streamlit_app.py 시작 시 실행되는 백그라운드 스레드
(app.services.minute_bar_archive_scheduler)와, 수동/백필용 CLI
(scripts/save_daily_minute_bars.py) 양쪽이 이 모듈의 함수만 호출한다 — KIS
페이징-워크 로직이 두 곳에 중복되지 않도록 이 파일이 유일한 구현이다.

읽기 전용 과거 시세 조회만 수행한다(주문 없음). 요청한 날짜와 KIS가 실제로
반환한 날짜가 다르면(휴장일에 조회하면 KIS가 가장 최근 거래일 데이터를 조용히
반환하는 경우가 있음, 실측됨) 저장하지 않는다. 3개 종목 중 하나라도 조회에
실패하면 그 날짜 전체를 저장하지 않는다(부분 저장으로 기존 파일을 덮어써
데이터가 섞이는 일이 없도록).
"""

from __future__ import annotations

import json
import time as time_mod
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

from app.trading.macd2 import config
from app.utils.data_paths import CACHE_DIR, STATE_DIR

KST = config.KST
LOOKBACK_CALENDAR_DAYS = 10  # 인자 없이 실행 시, 최근 이 기간 안의 누락 거래일을 자동 보충
ARCHIVE_LOG_PATH = STATE_DIR / "minute_bar_archive_log.json"
_MAX_LOG_ENTRIES = 500

SYMBOLS = {
    "hynix": config.WATCH_SYMBOL,      # 000660 -- 신호 계산용 원종목, 실제 매수 없음
    "long": config.LONG_SYMBOL,        # 0193T0 -- KODEX SK하이닉스단일종목레버리지 (실제 매수 종목)
    "inverse": config.INVERSE_SYMBOL,  # 0197X0 -- SOL 인버스2X (실제 매수 종목)
}


def candles_to_df(candles: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for c in candles or []:
        ds = str(c.get("date") or "").strip()
        ts = str(c.get("time") or "").replace(":", "").strip()
        if len(ds) != 8 or len(ts) < 6:
            continue
        dt = datetime.strptime(ds + ts[:6], "%Y%m%d%H%M%S").replace(tzinfo=KST)
        rows.append({
            "datetime": dt, "open": float(c.get("open") or 0), "high": float(c.get("high") or 0),
            "low": float(c.get("low") or 0), "close": float(c.get("close") or 0),
            "volume": int(float(c.get("volume") or 0)),
        })
    if not rows:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    return pd.DataFrame(rows).drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)


def fetch_full_day(client, symbol: str, date_ymd: str) -> pd.DataFrame:
    """지정된 거래일의 1분봉을 뒤에서부터 페이징으로 전부 모은다. KIS가 요청한
    날짜와 다른 날짜의 봉을 반환하면(휴장일 등) 그 행들은 날짜 필터에서 걸러져
    빈 결과가 되고, 그대로 빈 DataFrame을 반환한다 — 호출자가 "요청 날짜 != 실제
    반환 날짜"를 그냥 빈 데이터로 취급하면 되므로 별도 비교 로직이 필요 없다."""
    pages: list[pd.DataFrame] = []
    cursor = ""
    seen: set[tuple] = set()
    for _page in range(24):
        candles: list[dict[str, Any]] = []
        for attempt in range(5):
            candles = client.get_minute_candles_for_date(symbol, date_ymd, period_min=1, count=120, hour1=cursor) or []
            if candles:
                break
            time_mod.sleep(0.5 + attempt * 0.5)
        df = candles_to_df(candles)
        if df.empty:
            break
        df = df[df["datetime"].dt.strftime("%Y%m%d") == date_ymd]
        if df.empty:
            break
        pages.append(df)
        merged = pd.concat(pages, ignore_index=True).drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)
        oldest, newest = merged["datetime"].iloc[0], merged["datetime"].iloc[-1]
        key = (oldest, newest, len(merged))
        if key in seen or oldest.strftime("%H:%M") <= "09:00":
            return merged
        seen.add(key)
        pages = [merged]
        cursor = (oldest - timedelta(minutes=1)).strftime("%H%M%S")
        time_mod.sleep(float(getattr(config, "KIS_PAGE_FETCH_PACING_SEC", 0.4)))
    if not pages:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    return pd.concat(pages, ignore_index=True).drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)


def _existing_files(date_ymd: str) -> dict[str, Any]:
    return {tag: CACHE_DIR / f"replay_{date_ymd}_{tag}_1m.csv" for tag in SYMBOLS}


def is_complete(date_ymd: str) -> bool:
    return all(p.exists() for p in _existing_files(date_ymd).values())


def candidate_dates(explicit: Optional[list[str]] = None, *, now: Optional[datetime] = None) -> list[str]:
    if explicit:
        return list(explicit)
    now = now or datetime.now(KST)
    today = now.date()
    market_closed_today = now.time() >= config.FORCE_LIQUIDATE_AT  # 15:00 -- 실제 트리거는 16:00 스케줄러가 담당, 여기선 "오늘도 후보에 넣을지"만 판단
    start_delta = 0 if market_closed_today else 1
    dates = []
    for delta in range(LOOKBACK_CALENDAR_DAYS, start_delta - 1, -1):
        d = today - timedelta(days=delta)
        if d.weekday() >= 5:  # 주말 skip -- 공휴일은 조회 결과가 비어(또는 날짜 불일치로) 자동 skip됨
            continue
        dates.append(d.strftime("%Y%m%d"))
    return dates


def save_one_date(client, date_ymd: str) -> dict[str, Any]:
    if is_complete(date_ymd):
        return {"date": date_ymd, "status": "already_saved"}
    frames = {}
    counts = {}
    for tag, symbol in SYMBOLS.items():
        df = fetch_full_day(client, symbol, date_ymd)
        frames[tag] = df
        counts[tag] = len(df)
    result = {"date": date_ymd, "counts": counts}
    if all(c == 0 for c in counts.values()):
        result["status"] = "no_data (holiday_or_not_yet_closed_or_date_mismatch)"
        return result
    if any(c == 0 for c in counts.values()):
        result["status"] = "partial_fetch_not_saved"
        return result
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    paths = _existing_files(date_ymd)
    for tag, df in frames.items():
        df.to_csv(paths[tag], index=False)
    result["status"] = "saved"
    return result


def _append_log(entries: list[dict[str, Any]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    history = []
    if ARCHIVE_LOG_PATH.exists():
        try:
            history = json.loads(ARCHIVE_LOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            history = []
    history.extend(entries)
    history = history[-_MAX_LOG_ENTRIES:]
    ARCHIVE_LOG_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def run_archive(client, explicit_dates: Optional[list[str]] = None, *, source: str = "manual") -> list[dict[str, Any]]:
    """dates가 없으면 최근 LOOKBACK_CALENDAR_DAYS 안의 누락 거래일을 전부 시도
    (자동 보충). 실행 결과는 매번 ARCHIVE_LOG_PATH에 영구 기록된다(성공/실패
    모두, Render 재배포로 다음 실행 로그와 이어붙여진다)."""
    dates = candidate_dates(explicit_dates)
    run_at = datetime.now(KST).isoformat()
    results = []
    for d in dates:
        if is_complete(d):
            results.append({"date": d, "status": "already_saved"})
            continue
        results.append(save_one_date(client, d))
    _append_log([{"run_at": run_at, "source": source, "result": r} for r in results])
    return results
