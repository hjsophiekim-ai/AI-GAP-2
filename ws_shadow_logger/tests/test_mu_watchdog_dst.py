"""MU watchdog 감시 시작 = RBAQMU 주간거래 시작(미국 전일 20:00 ET) 날짜별 환산 (2026-11-02).

서머타임 해제 첫날(11/02) 전에는 09:00 고정이라 09:00~10:00 RBAQMU 무체결 구간에서
STALE -> 재구독 6회 -> GAVE_UP 이 났다. 서머타임 기간 동작(09:00)은 그대로여야 한다.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from ws_shadow_logger import config
from ws_shadow_logger.mu_watchdog import GAVE_UP, OK, QUOTE_TR, STALE, TRADE_TR, MuWatchdog, daytime_open_kst
from ws_shadow_logger.tests.test_mu_watchdog import ScriptWS, _clock, _run, _wd_lines


@pytest.mark.parametrize("day, expect", [
    (date(2026, 10, 26), time(9, 0)),     # 월, EDT (미국 일요일 20:00 EDT)
    (date(2026, 10, 30), time(9, 0)),     # 금, EDT
    (date(2026, 11, 2), time(10, 0)),     # 월, EST 첫날 (미국 11/01 일요일 해제)
    (date(2026, 11, 3), time(10, 0)),     # 화, EST
    (date(2027, 3, 15), time(9, 0)),      # 월, 2027-03-14 서머타임 재시작 이후
])
def test_daytime_open_follows_us_dst(day, expect):
    assert daytime_open_kst(day) == expect
    assert MuWatchdog().start_for(datetime.combine(day, time(12, 0))) == expect


def test_config_has_no_fixed_watch_start():
    assert not hasattr(config, "MU_WATCH_START")


def _day_sim(w, day, rbaq_from=None, rbaq_until=None, dnas=True, step=5):
    """08:00 접속 ~ 15:31, step 초마다 poll. 재구독 응답은 즉시 성공. 이벤트 [(HH:MM:SS, kind)]."""
    t, end = datetime.combine(day, time(8, 0)), datetime.combine(day, time(15, 31))
    w.on_connected(t)
    out = []
    while t < end:
        if dnas and t.time() < (rbaq_from or time(23, 59)):
            w.note_data(TRADE_TR, "DNASMU", t)
        if rbaq_from and rbaq_from <= t.time() < (rbaq_until or time(23, 59)):
            out += [(t.strftime("%H:%M:%S"), e["kind"]) for e in w.note_data(TRADE_TR, "RBAQMU", t)]
            w.note_data(QUOTE_TR, "RBAQMU", t)
        for e in w.poll(t):
            out.append((t.strftime("%H:%M:%S"), e["kind"]))
            if e["kind"] == "RESUBSCRIBE_ATTEMPT":
                for tr in (TRADE_TR, QUOTE_TR):
                    w.note_reply(tr, "RBAQMU", True, "SUBSCRIBE SUCCESS", t)
        t += timedelta(seconds=step)
    return out


@pytest.mark.parametrize("day", [date(2026, 11, 2), date(2026, 11, 3)])
def test_est_no_false_stale_before_1000(day):
    """비서머타임: RBAQMU 가 10:00 에 열리면 09:00~10:00 STALE/재구독/GAVE_UP 0건, 하루 종일 stale 구간 0."""
    w = MuWatchdog()
    ev = _day_sim(w, day, rbaq_from=time(10, 0), dnas=day.weekday() != 0)   # 월요일은 DNASMU 도 없다
    assert [e for e in ev if e[0] < "10:00:00"] == []
    assert not any(k in ("GAVE_UP",) for _, k in ev)
    assert all(k != "RESUBSCRIBE_ATTEMPT" for t, k in ev if t < "10:00:00")
    assert w.status == OK


def test_est_old_fixed_0900_reproduces_the_bug():
    """회귀 기준: 예전 09:00 고정이면 11/02 에 거짓 STALE + 재구독 6회 + GAVE_UP."""
    ev = _day_sim(MuWatchdog(watch_start=time(9, 0)), date(2026, 11, 2), rbaq_from=time(10, 0), dnas=False)
    kinds = [k for _, k in ev]
    assert kinds[0] == "STALE" and kinds.count("RESUBSCRIBE_ATTEMPT") == 6 and "GAVE_UP" in kinds


@pytest.mark.parametrize("day", [date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 30)])
@pytest.mark.parametrize("rbaq_from, rbaq_until", [(time(9, 0), None), (time(9, 0), time(12, 46)), (None, None)])
def test_edt_identical_to_old_fixed_0900(day, rbaq_from, rbaq_until):
    """서머타임 날짜: 새 기본값과 예전 09:00 고정이 정상/장중정지/무수신 모두 이벤트까지 동일."""
    new = _day_sim(MuWatchdog(), day, rbaq_from, rbaq_until)
    old = _day_sim(MuWatchdog(watch_start=time(9, 0)), day, rbaq_from, rbaq_until)
    assert new == old


@pytest.mark.parametrize("day", [date(2026, 11, 2), date(2026, 11, 3)])
def test_est_real_stall_after_1000_resubscribes_like_before(day):
    """비서머타임 10:00 이후 실제 정지: 예전(10:00 고정과 동일한 감시창) 재구독 일정·GAVE_UP 그대로."""
    new = _day_sim(MuWatchdog(), day, time(10, 0), time(12, 46))
    old = _day_sim(MuWatchdog(watch_start=time(10, 0)), day, time(10, 0), time(12, 46))
    assert new == old
    att = [t for t, k in new if k == "RESUBSCRIBE_ATTEMPT"]
    assert [k for _, k in new][0] == "STALE" and new[0][0] == "12:48:55"     # 마지막 수신 12:45:55 + 180초
    assert att == ["12:48:55", "12:49:55", "12:54:55", "12:59:55", "13:04:55", "13:09:55"]
    assert ("13:10:00", "GAVE_UP") in new


def test_est_no_data_at_all_still_detected_from_1000():
    """비서머타임에 RBAQMU 가 하루 종일 안 오면 10:00 부터 STALE (감시 자체는 살아 있다)."""
    w = MuWatchdog()
    ev = _day_sim(w, date(2026, 11, 3), rbaq_from=None)
    assert ev[0] == ("10:00:00", "STALE") and w.summary()["mu_stale_intervals"][0]["stale_start"].endswith("10:00:00")


# ── 통합: LoggerService ────────────────────────────────────────────────────
def test_service_est_0910_watchdog_does_not_fire(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_MU_STALE_SEC, "30")
    start = datetime(2026, 11, 2, 9, 10, 0)
    ws = ScriptWS(start)
    svc, lines = _run(tmp_path / "lg", ws, _clock(start, 20.0), lambda ls: False, timeout=3.0)
    assert not _wd_lines(lines, "STALE") and len(ws.sent) == 10


def test_service_edt_0910_watchdog_still_fires(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_MU_STALE_SEC, "30")
    start = datetime(2026, 10, 30, 9, 10, 0)
    ws = ScriptWS(start, recover=True)
    svc, lines = _run(tmp_path / "lg", ws, _clock(start, 20.0), lambda ls: _wd_lines(ls, "RECOVERED"))
    assert _wd_lines(lines, "STALE") and _wd_lines(lines, "RECOVERED")


def test_service_est_after_1000_stall_resubscribes_rbaqmu_only(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_MU_STALE_SEC, "30")
    start = datetime(2026, 11, 3, 10, 40, 0)
    ws = ScriptWS(start, recover=True)
    svc, lines = _run(tmp_path / "lg", ws, _clock(start, 20.0), lambda ls: _wd_lines(ls, "RECOVERED"))
    assert _wd_lines(lines, "STALE") and _wd_lines(lines, "RECOVERED")
    assert [(m["header"]["tr_type"], m["body"]["input"]["tr_id"], m["body"]["input"]["tr_key"]) for m in ws.sent[10:]] == [
        ("2", "HDFSCNT0", "RBAQMU"), ("1", "HDFSCNT0", "RBAQMU"), ("2", "HDFSASP0", "RBAQMU"), ("1", "HDFSASP0", "RBAQMU")]
