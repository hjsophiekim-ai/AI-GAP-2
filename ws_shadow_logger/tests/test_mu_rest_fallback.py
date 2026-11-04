"""MU REST fallback (2026-11-04) — 09:00~주간거래 시작 공백만, 중복/정렬/서머타임/토큰 캐시."""
from __future__ import annotations

import asyncio
import csv
from datetime import date, datetime, time, timedelta

import pytest

from ws_shadow_logger import config, mu_rest_fallback as rf
from ws_shadow_logger.mu import BAR_COLUMNS

CREDS = config.Credentials("LOGKEY", "LOGSEC")


@pytest.mark.parametrize("day, expect", [
    (date(2026, 10, 26), None),                              # 월 EDT — 주간거래 09:00
    (date(2026, 10, 30), None),                              # 금 EDT
    (date(2026, 11, 2), (time(9, 0), time(10, 0))),          # 월 EST 첫날
    (date(2026, 11, 3), (time(9, 0), time(10, 0))),          # 화 EST
    (date(2026, 11, 7), None),                               # 토
    (date(2027, 3, 15), None),                               # 2027 서머타임 재시작 후
])
def test_window_follows_us_dst(day, expect):
    w = rf.window(day)
    assert (None if w is None else (w[0].time(), w[1].time())) == expect


def _item(kymd, khms, px, vol=5):
    return {"kymd": kymd, "khms": khms, "xymd": "", "xhms": "", "open": f"{px - 0.1:.4f}",
            "high": f"{px + 0.2:.4f}", "low": f"{px - 0.3:.4f}", "last": f"{px:.4f}", "evol": str(vol)}


def _rows(day="20261103", start=(8, 50), end=(10, 5), base=860.0):
    t = datetime(int(day[:4]), int(day[4:6]), int(day[6:]), *start)
    e = t.replace(hour=end[0], minute=end[1])
    out = []
    while t < e:
        out.append(_item(day, t.strftime("%H%M%S"), base + t.minute / 100))
        t += timedelta(minutes=1)
    return list(reversed(out))      # KIS 는 최신이 먼저 온다


def test_to_bars_parses_and_skips_bad_rows():
    rows = _rows() + [{"kymd": "", "khms": "090000", "last": "1"}, _item("20261103", "0901", 0)]
    b = rf.to_bars(rows, datetime(2026, 11, 3, 9, 30))
    m = datetime(2026, 11, 3, 9, 0)
    assert b[m]["feed"] == rf.FEED and b[m]["close"] == pytest.approx(860.0)
    assert b[m]["low"] <= min(b[m]["open"], b[m]["close"]) and b[m]["high"] >= max(b[m]["open"], b[m]["close"])
    assert rf.to_bars(rows, datetime(2026, 11, 3, 9, 30), offset_min=-1)[datetime(2026, 11, 3, 8, 59)]["close"] \
        == pytest.approx(860.0)


def test_select_window_complete_and_websocket_dedupe():
    now = datetime(2026, 11, 3, 9, 30, 5)
    b = rf.to_bars(_rows(), now)
    keep = rf.select(b, now, "2026-11-03T09:00:00", None)
    ms = sorted(keep)
    assert ms[0] == datetime(2026, 11, 3, 9, 1)               # WS 가 09:00 까지 썼다
    assert ms[-1] == datetime(2026, 11, 3, 9, 28)             # 09:29 는 아직 확정 전(분끝+10초 미경과)
    assert all(rf.WINDOW_START <= m.time() for m in ms)
    keep2 = rf.select(b, datetime(2026, 11, 3, 10, 2), "2026-11-03T08:59:00", "2026-11-03T09:50:00")
    assert max(keep2) == datetime(2026, 11, 3, 9, 49)         # RBAQ 가 09:50 에 들어오면 그 전까지만


def test_select_drops_other_dates_monday_and_summer():
    mon = datetime(2026, 11, 2, 9, 30)
    fri_rows = _rows(day="20261030")                          # 월요일 아침 REST 가 금요일 봉만 주는 경우
    assert rf.select(rf.to_bars(fri_rows, mon), mon, None, None) == {}
    summer = datetime(2026, 10, 28, 9, 30)
    assert rf.select(rf.to_bars(_rows(day="20261028"), summer), summer, None, None) == {}


def test_write_merged_sorted_dedupe_atomic(tmp_path):
    p = tmp_path / "mu" / "2026-11-03" / rf.FILENAME
    now = datetime(2026, 11, 3, 9, 30)
    b = rf.to_bars(_rows(), now)
    part1 = {m: v for m, v in b.items() if time(9, 10) <= m.time() < time(9, 20)}
    part2 = {m: v for m, v in b.items() if time(9, 0) <= m.time() < time(9, 15)}     # 앞쪽 + 겹침
    assert rf.write_merged(p, part1) == 10
    changed = {m: {**v, "close": 1.0} for m, v in part2.items()}
    assert rf.write_merged(p, changed) == 10                   # 09:00~09:09 만 새 분
    rows = list(csv.DictReader(open(p, encoding="utf-8")))
    ms = [r["minute_kst"] for r in rows]
    assert ms == sorted(ms) and len(ms) == len(set(ms)) == 20
    assert list(rows[0].keys()) == BAR_COLUMNS
    assert float(next(r for r in rows if r["minute_kst"].endswith("09:12:00"))["close"]) == 1.0   # 같은 분 = 최신값
    assert not list(p.parent.glob("*.tmp"))
    assert rf.write_merged(p, changed) == 0                    # 멱등


class _Resp:
    def __init__(self, body, status=200):
        self._b, self.status_code = body, status

    def json(self):
        return self._b


def _fakes(rows_fn):
    calls = {"post": [], "get": []}

    def post(url, body):
        calls["post"].append((url, body))
        return _Resp({"access_token": "TOK", "expires_in": 86400})

    def get(url, headers, params):
        calls["get"].append((url, headers, params))
        return _Resp({"rt_cd": "0", "msg_cd": "MCA00000", "output2": rows_fn()})
    return calls, get, post


def _ws_bars(root, day, dnas_until="09:00", rbaq_from="10:00"):
    d = root / "mu" / day
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "bars_1m.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=BAR_COLUMNS)
        w.writeheader()
        t = datetime.fromisoformat(f"{day}T08:00:00")
        while t.strftime("%H:%M") < "11:00":
            hm = t.strftime("%H:%M")
            for feed, ok in (("DNASMU", hm < dnas_until), ("RBAQMU", hm >= rbaq_from)):
                if ok:
                    w.writerow({"minute_kst": t.isoformat(), "feed": feed, "open": 1, "high": 1, "low": 1,
                                "close": 1, "volume": 1, "ticks": 1, "complete": 1})
            t += timedelta(minutes=1)


def test_poll_day_simulation_fills_exact_gap_once(tmp_path):
    """11/03 모양: WS DNAS ~08:59, RBAQ 10:00~. 09:01:10 부터 10:03 까지 due 마다 poll -> 09:00~09:59 60행."""
    day = "2026-11-03"
    _ws_bars(tmp_path, day)
    clock = {"now": datetime(2026, 11, 3, 8, 59)}

    def rows_now():     # 그 시각까지 생긴 봉만(진행 중 분 포함) 돌려준다
        return [r for r in _rows() if datetime.strptime(r["kymd"] + r["khms"], "%Y%m%d%H%M%S") <= clock["now"]]
    calls, get, post = _fakes(rows_now)
    fb = rf.MuRestFallback(tmp_path, CREDS, get=get, post=post)
    t = clock["now"]
    while t < datetime(2026, 11, 3, 10, 5):
        clock["now"] = t
        if fb.due(t):
            res = fb.poll(t)
            assert res["ok"], res
        t += timedelta(seconds=5)
    rows = list(csv.DictReader(open(rf.path_for(tmp_path, day), encoding="utf-8")))
    ms = [r["minute_kst"][11:16] for r in rows]
    assert ms[0] == "09:00" and ms[-1] == "09:59" and len(ms) == 60 == len(set(ms)) and ms == sorted(ms)
    assert all(r["feed"] == rf.FEED and r["complete"] == "1" for r in rows)
    assert len(calls["post"]) == 1 and calls["post"][0][0].endswith("/oauth2/tokenP")     # 토큰 1회
    assert calls["post"][0][1]["appkey"] == "LOGKEY"
    assert 60 <= len(calls["get"]) <= 64                                                 # 분당 1회
    url, headers, params = calls["get"][0]
    assert url.endswith(rf.MINUTE_PATH) and headers["tr_id"] == "HHDFS76950200"
    assert params["EXCD"] == "NAS" and params["SYMB"] == "MU"
    assert not fb.due(datetime(2026, 11, 3, 10, 3, 1))


def test_summer_day_never_polls(tmp_path):
    calls, get, post = _fakes(lambda: _rows(day="20261028"))
    fb = rf.MuRestFallback(tmp_path, CREDS, get=get, post=post)
    t = datetime(2026, 10, 28, 8, 0)
    while t < datetime(2026, 10, 28, 11, 0):
        assert not fb.due(t)
        t += timedelta(seconds=30)
    assert calls["get"] == [] and not rf.path_for(tmp_path, "2026-10-28").exists()


def test_token_cached_across_restart_and_retry_guard(tmp_path):
    calls, get, post = _fakes(_rows)
    now = datetime(2026, 11, 3, 9, 5)
    assert rf.get_token(CREDS, tmp_path, now, post=post) == "TOK"
    assert rf.get_token(CREDS, tmp_path, now + timedelta(hours=3), post=post) == "TOK"   # 새 프로세스여도 파일 캐시
    assert len(calls["post"]) == 1
    other = config.Credentials("OTHER", "S")                                             # 키가 바뀌면 재발급

    def bad_post(url, body):
        calls["post"].append((url, body))
        return _Resp({"error_code": "EGW00133"}, 403)
    with pytest.raises(RuntimeError):
        rf.get_token(other, tmp_path, now, post=bad_post)
    with pytest.raises(RuntimeError, match="재시도 대기"):
        rf.get_token(other, tmp_path, now + timedelta(seconds=30), post=bad_post)
    assert len(calls["post"]) == 2                                                       # 70초 안 재발급 시도 없음


def test_poll_error_is_contained(tmp_path):
    def get(url, headers, params):
        raise OSError("network down")
    _, _, post = _fakes(_rows)
    fb = rf.MuRestFallback(tmp_path, CREDS, get=get, post=post)
    res = fb.poll(datetime(2026, 11, 3, 9, 10))
    assert res["ok"] is False and "network down" in res["error"] and fb.counts["errors"] == 1


def test_service_runs_fallback_off_the_ws_loop(tmp_path, monkeypatch):
    """LoggerService._mu_rest_loop 가 시계 기준으로 poll 하고, 결과를 logs/ 와 stdout 에 남긴다."""
    from ws_shadow_logger.service import LoggerService
    day = "2026-11-03"
    _ws_bars(tmp_path, day)
    calls, get, post = _fakes(_rows)
    lines = []
    clock = {"t": datetime(2026, 11, 3, 9, 20)}
    svc = LoggerService(tmp_path, CREDS, max_rss_mb=100, now_fn=lambda: clock["t"], out=lines.append,
                        rest_get=get, rest_post=post, rest_every=0.01)

    async def go():
        task = asyncio.create_task(svc._mu_rest_loop())
        await asyncio.sleep(0.2)
        svc.request_stop("test")
        await task
    asyncio.run(go())
    assert any("MU_REST_FALLBACK" in ln and '"added": 19' in ln for ln in lines), lines   # 09:00~09:18
    assert len(calls["get"]) == 1                                                         # 같은 분 재호출 없음
    assert "MU_REST=polls=1/bars=19" in svc.health_line()[0]
