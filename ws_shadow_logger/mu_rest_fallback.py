"""MU REST fallback — DNASMU 종료 ~ RBAQMU 시작 사이 09:00대 공백만 KIS REST 분봉으로 보충 (2026-11-04).

왜: 미국 서머타임 해제(EST) 기간에는 RBAQMU(블루오션 주간거래)가 10:00 KST 에 열린다. 그런데 KIS 웹소켓
DNASMU(나스닥 시간외)는 현지 19:00:00 EST(= 09:00 KST)에 끊긴다 — 2026-11-04 Render raw frames_09
실측: DNASMU 프레임 3건(09:00:00~09:00:01)뿐, 이후 0. KIS 앱 차트에는 그 1시간 시세가 있다(나스닥
시간외는 20:00 ET 까지). 그래서 09:00~주간거래 시작 전 구간만 REST 해외주식분봉조회로 채운다.

원칙
  · 기존 웹소켓 수집은 그대로다. 이 모듈은 bars_1m.csv / ticks.csv 를 건드리지 않고
    ``mu/<날짜>/rest_bars_1m.csv`` 한 파일만 쓴다(15:30 finalize_day 가 bars_1m.csv 를 tick 으로
    재생성해도 지워지지 않는다). R2 export 는 mu/<날짜>/* 를 통째로 묶으므로 자동 포함.
  · 사용 구간: KST [09:00, 주간거래 시작) — 서머타임(EDT)이면 시작이 09:00 이라 구간이 비어 **아무것도
    하지 않는다**. 월요일도 같은 규칙(미국 일요일 저녁은 나스닥 시간외가 없어 REST 응답이 그날 KST
    날짜가 아니면 버린다).
  · 중복: 웹소켓 DNASMU 가 이미 쓴 분(<= 마지막 DNASMU 분), RBAQMU 가 이미 시작된 분(>= 첫 RBAQMU 분)은
    쓰지 않는다. 같은 분 REST 재응답은 마지막 값으로 덮는다(분당 1행). 파일은 매번 분 오름차순으로
    원자적 교체(tmp + os.replace)라 읽는 쪽이 반쯤 쓴 파일을 보지 않는다.
  · 확정: 분 끝 + REST_COMPLETE_GRACE_SEC 가 지난 분만 쓴다(진행 중 분봉 금지).
  · 키: 로거 전용 appkey 로만 REST 접근토큰을 발급한다(production 키는 config.load_credentials 가 거부).
    토큰은 state/rest_token.json 에 캐시해 만료 10분 전까지 재사용하고, 발급 시도는 70초에 1회로 묶는다.
    호출 경로는 tokenP 와 해외주식분봉조회 두 개뿐이다 — 주문/잔고 경로 없음.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import config
from .mu import BAR_COLUMNS, mu_dir
from .mu_watchdog import daytime_open_kst

TOKEN_PATH = "/oauth2/tokenP"
MINUTE_PATH = "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
MINUTE_TR = "HHDFS76950200"          # 해외주식분봉조회
EXCD = "NAS"
FEED = "NASMU_REST"                  # production mu_assist.MU_REST_FEED 와 같은 값
FILENAME = "rest_bars_1m.csv"
WINDOW_START = time(9, 0)
NREC = 120                           # 한 번에 최근 120분 — 재시작 직후에도 구간 전체를 한 번에 메운다
POLL_EVERY_SEC = 60.0
REST_COMPLETE_GRACE_SEC = 10.0
#: 구간 끝 뒤 이만큼 더 돌아 마지막 분(예: 09:59)까지 확정해 받는다.
TAIL_SEC = 180.0
#: REST 분봉 시각(khms)이 '분 시작'이면 0. --probe 가 웹소켓 DNASMU 분봉과 맞춰 확인한다.
BAR_TIME_OFFSET_MIN = 0
TOKEN_REFRESH_MARGIN = timedelta(minutes=10)
TOKEN_MIN_RETRY_SEC = 70.0
HTTP_TIMEOUT = 10


def enabled() -> bool:
    """기본 ON. KIS_LOGGER_MU_REST_FALLBACK=0 이면 끈다."""
    return (os.environ.get("KIS_LOGGER_MU_REST_FALLBACK") or "1").strip() != "0"


def window(day: date) -> tuple[datetime, datetime] | None:
    """보충 구간 [start, end) (naive KST). 평일이 아니거나 서머타임(주간거래 09:00 시작)이면 None."""
    if day.weekday() >= 5:
        return None
    start = datetime.combine(day, WINDOW_START)
    end = datetime.combine(day, daytime_open_kst(day))
    return (start, end) if end > start else None


def path_for(root: Path, day: str) -> Path:
    return mu_dir(root, day) / FILENAME


# ── 토큰 ───────────────────────────────────────────────────────────────────
def _token_file(root: Path) -> Path:
    return Path(root) / "state" / "rest_token.json"


def get_token(creds: config.Credentials, root: Path, now: datetime, post=None) -> str:
    p = _token_file(root)
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    if data.get("fp") == creds.fingerprint and data.get("access_token"):
        try:
            if now < datetime.fromisoformat(data["expires_at"]) - TOKEN_REFRESH_MARGIN:
                return data["access_token"]
        except (KeyError, ValueError):
            pass
    last = data.get("last_attempt")
    if last and data.get("fp") == creds.fingerprint:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < TOKEN_MIN_RETRY_SEC:
                raise RuntimeError("rest token: 발급 재시도 대기(70초에 1회)")
        except ValueError:
            pass
    p.parent.mkdir(parents=True, exist_ok=True)
    attempt = {"fp": creds.fingerprint, "last_attempt": now.isoformat()}
    _write_json(p, {**{k: v for k, v in data.items() if data.get("fp") == creds.fingerprint}, **attempt})
    if post is None:
        import requests
        post = lambda u, body: requests.post(u, data=json.dumps(body), timeout=HTTP_TIMEOUT,     # noqa: E731
                                             headers={"content-type": "application/json; charset=utf-8"})
    url = config.REST_BASE + TOKEN_PATH
    r = post(url, {"grant_type": "client_credentials", "appkey": creds.app_key, "appsecret": creds.app_secret})
    body = r.json()
    tok = body.get("access_token")
    if not tok:
        raise RuntimeError(f"rest token 발급 실패: status={getattr(r, 'status_code', '?')} {str(body)[:160]}")
    exp = now + timedelta(seconds=int(body.get("expires_in") or 86400))
    _write_json(p, {**attempt, "access_token": tok, "expires_at": exp.isoformat(), "issued_at": now.isoformat()})
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return tok


def _write_json(p: Path, d: dict):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, p)


# ── 조회 / 변환 ────────────────────────────────────────────────────────────
def fetch(creds: config.Credentials, token: str, get=None) -> list[dict]:
    """해외주식분봉조회 output2 원본 행 목록. 실패면 예외."""
    if get is None:
        import requests
        get = lambda u, headers, params: requests.get(u, headers=headers, params=params,      # noqa: E731
                                                      timeout=HTTP_TIMEOUT)
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}",
               "appkey": creds.app_key, "appsecret": creds.app_secret, "tr_id": MINUTE_TR, "custtype": "P"}
    params = {"AUTH": "", "EXCD": EXCD, "SYMB": config.MU_SYMBOL, "NMIN": "1", "PINC": "1", "NEXT": "",
              "NREC": str(NREC), "FILL": "", "KEYB": ""}
    r = get(config.REST_BASE + MINUTE_PATH, headers, params)
    body = r.json()
    if str(body.get("rt_cd")) not in ("0", "None") and body.get("rt_cd") is not None:
        raise RuntimeError(f"분봉조회 실패 rt_cd={body.get('rt_cd')} msg_cd={body.get('msg_cd')} "
                           f"msg1={str(body.get('msg1'))[:80]}")
    return list(body.get("output2") or [])


def _num(v):
    try:
        x = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def to_bars(rows: list[dict], fetched_at: datetime, offset_min: int = BAR_TIME_OFFSET_MIN) -> dict:
    """REST 원본 -> {minute_kst(naive datetime): bar dict}. 시각/가격이 없는 행은 버린다."""
    out = {}
    for it in rows:
        d, t = str(it.get("kymd") or ""), str(it.get("khms") or "")
        close = _num(it.get("last"))
        if len(d) != 8 or len(t) < 4 or close is None:
            continue
        try:
            m = datetime.strptime(d + t[:4], "%Y%m%d%H%M") + timedelta(minutes=offset_min)
        except ValueError:
            continue
        o, h, lo = (_num(it.get(k)) or close for k in ("open", "high", "low"))
        out[m] = {"minute_kst": m.isoformat(timespec="seconds"), "feed": FEED, "open": o,
                  "high": max(h, o, close), "low": min(lo, o, close), "close": close,
                  "volume": _num(it.get("evol")) or 0.0, "ticks": 0,
                  "first_kst": "", "last_kst": "", "first_recv": fetched_at.isoformat(timespec="seconds"),
                  "last_recv": fetched_at.isoformat(timespec="seconds"), "complete": 1}
    return out


def select(bars: dict, now: datetime, ws_last_dnas: str | None, ws_first_rbaq: str | None) -> dict:
    """구간·확정·웹소켓 중복 규칙을 통과한 분만."""
    w = window(now.date())
    if w is None:
        return {}
    start, end = w
    done_before = now - timedelta(seconds=60 + REST_COMPLETE_GRACE_SEC)
    keep = {}
    for m, b in bars.items():
        if not (start <= m < end) or m > done_before:
            continue
        iso = m.isoformat(timespec="seconds")
        if ws_last_dnas and iso <= ws_last_dnas:
            continue            # 웹소켓 DNASMU 가 이미 쓴 구간
        if ws_first_rbaq and iso >= ws_first_rbaq:
            continue            # 주간거래가 이미 들어온 구간
        keep[m] = b
    return keep


def read_file(p: Path) -> dict:
    out = {}
    if not p.exists():
        return out
    with open(p, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                out[datetime.fromisoformat(row["minute_kst"][:19])] = row
            except (KeyError, ValueError):
                continue
    return out


def write_merged(p: Path, new: dict) -> int:
    """기존 행 + 새 행(같은 분은 새 값) 을 분 오름차순으로 원자적 교체. 새로 생긴 분 수를 돌려준다."""
    cur = read_file(p)
    added = sum(1 for m in new if m not in cur)
    cur.update(new)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=BAR_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for m in sorted(cur):
            w.writerow(cur[m])
    os.replace(tmp, p)
    return added


def ws_bounds(root: Path, day: str) -> tuple[str | None, str | None]:
    """bars_1m.csv 에서 오늘 DNASMU 마지막 분 / RBAQMU 첫 분 (주간거래 시작 이후)."""
    p = mu_dir(root, day) / "bars_1m.csv"
    last_d = first_r = None
    if not p.exists():
        return None, None
    with open(p, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            m, f = row.get("minute_kst") or "", row.get("feed")
            if not m.startswith(day):
                continue
            if f == "DNASMU" and (last_d is None or m > last_d):
                last_d = m
            elif f == "RBAQMU" and (first_r is None or m < first_r):
                first_r = m
    return last_d, first_r


class MuRestFallback:
    """service 가 몇 초마다 ``due`` 를 묻고, True 면 스레드에서 ``poll`` 을 부른다."""

    def __init__(self, root: Path, creds: config.Credentials, *, get=None, post=None):
        self.root = Path(root)
        self.creds = creds
        self.get, self.post = get, post
        self.last_poll: datetime | None = None
        self.counts = {"polls": 0, "bars": 0, "errors": 0}
        self.last_error = ""
        self.last_minute = ""

    def due(self, now: datetime) -> bool:
        w = window(now.date())
        if w is None:
            return False
        start, end = w
        if not (start + timedelta(seconds=60 + REST_COMPLETE_GRACE_SEC) <= now < end + timedelta(seconds=TAIL_SEC)):
            return False
        return self.last_poll is None or (now - self.last_poll).total_seconds() >= POLL_EVERY_SEC

    def poll(self, now: datetime, ws_last_dnas: str | None = None, ws_first_rbaq: str | None = None) -> dict:
        """한 번 조회해 쓴다. 예외를 밖으로 내지 않고 결과 dict 를 돌려준다(로그용)."""
        self.last_poll = now
        self.counts["polls"] += 1
        day = now.strftime("%Y-%m-%d")
        try:
            if ws_last_dnas is None and ws_first_rbaq is None:
                ws_last_dnas, ws_first_rbaq = ws_bounds(self.root, day)
            tok = get_token(self.creds, self.root, now, post=self.post)
            rows = fetch(self.creds, tok, get=self.get)
            keep = select(to_bars(rows, now), now, ws_last_dnas, ws_first_rbaq)
            added = write_merged(path_for(self.root, day), keep) if keep else 0
            self.counts["bars"] += added
            if keep:
                self.last_minute = max(keep).strftime("%H:%M")
            return {"ok": True, "rows": len(rows), "kept": len(keep), "added": added,
                    "last_minute": self.last_minute, "ws_last_dnas": ws_last_dnas, "ws_first_rbaq": ws_first_rbaq}
        except Exception as e:  # noqa: BLE001 — 보충 실패가 웹소켓 수집을 멈추면 안 된다
            self.counts["errors"] += 1
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            return {"ok": False, "error": self.last_error}

    def health(self) -> str:
        return (f"polls={self.counts['polls']}/bars={self.counts['bars']}/err={self.counts['errors']}"
                f"/last={self.last_minute or '-'}")


# ── 수동 점검 (Render shell) ───────────────────────────────────────────────
def probe(root: Path, now: datetime, out=print) -> int:
    """조회 전용 점검: REST 원본 08:00~10:10 KST 분봉을 찍고, 웹소켓 DNASMU 분봉과 시각 정렬(offset)을 맞춰 본다.
    파일은 쓰지 않는다(토큰 캐시만). python -m ws_shadow_logger.mu_rest_fallback --probe"""
    creds = config.load_credentials()
    tok = get_token(creds, root, now)
    rows = fetch(creds, tok)
    day = now.strftime("%Y-%m-%d")
    out(f"REST rows={len(rows)} window={window(now.date())}")
    ws = {}
    p = mu_dir(root, day) / "bars_1m.csv"
    if p.exists():
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("feed") == "DNASMU":
                    ws[r["minute_kst"][:16]] = float(r["close"])
    for off in (0, -1, 1):
        b = to_bars(rows, now, off)
        both = [(k.isoformat()[:16], v["close"]) for k, v in b.items() if k.isoformat()[:16] in ws]
        same = sum(1 for k, c in both if abs(c - ws[k]) < 1e-6)
        out(f"offset {off:+d}: WS DNASMU 와 겹친 분 {len(both)} / close 일치 {same}")
    b = to_bars(rows, now, BAR_TIME_OFFSET_MIN)
    for m in sorted(b):
        if m.date() == now.date() and time(8, 50) <= m.time() <= time(10, 10):
            v = b[m]
            out(f"  {m:%H:%M} o={v['open']} h={v['high']} l={v['low']} c={v['close']} vol={v['volume']}")
    last_d, first_r = ws_bounds(root, day)
    keep = select(b, now, last_d, first_r)
    out(f"WS DNASMU 마지막 {last_d} / RBAQMU 첫 {first_r} -> fallback 이 쓸 분 {len(keep)}개"
        + (f" ({min(keep):%H:%M}~{max(keep):%H:%M})" if keep else ""))
    return 0


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="python -m ws_shadow_logger.mu_rest_fallback")
    ap.add_argument("--probe", action="store_true", required=True)
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    dc = config.check_data_dir()
    if not dc.ok:
        print(f"data dir 문제: {dc}")
        raise SystemExit(config.EXIT_DISK)
    from .service import now_kst
    raise SystemExit(probe(Path(dc.path), now_kst()))
