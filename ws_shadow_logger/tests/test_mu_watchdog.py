"""MU watchdog (2026-10-21) — 국내 연결은 정상인데 RBAQMU 체결·호가만 멈추는 경우.

단위: MuWatchdog 규칙(시계를 직접 넣는다). 통합: LoggerService + 가짜 웹소켓 — 실제로 보낸 등록/해제 메시지 검사.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta

import pytest

from ws_shadow_logger import config, export_day, service, stub
from ws_shadow_logger.mu_watchdog import GAVE_UP, OK, QUOTE_TR, STALE, TRADE_TR, MuWatchdog

T0 = datetime(2026, 10, 21, 12, 0, 0)        # 수요일 장중


def _wd(**kw):
    w = MuWatchdog(**kw)
    w.on_connected(T0)
    return w


def _feed_both(w, t0, seconds, feed="RBAQMU", step=10):
    for s in range(0, seconds, step):
        w.note_data(TRADE_TR, feed, t0 + timedelta(seconds=s))
        w.note_data(QUOTE_TR, feed, t0 + timedelta(seconds=s + 1))


def _kinds(ev):
    return [e["kind"] for e in ev]


# ── 단위: 판정 ────────────────────────────────────────────────────────────
def test_both_trade_and_quote_silent_is_stale_and_resubscribes_immediately():
    w = _wd()
    _feed_both(w, T0, 600)
    last = T0 + timedelta(seconds=591)
    assert w.poll(last + timedelta(seconds=179)) == []
    ev = w.poll(last + timedelta(seconds=181))
    assert _kinds(ev) == ["STALE", "RESUBSCRIBE_ATTEMPT"]
    assert ev[0]["severity"] == "STRONG" and ev[0]["prev10_rate_per_min"] >= 1.0
    assert ev[1]["trs"] == [TRADE_TR, QUOTE_TR] and ev[1]["attempt"] == 1
    assert w.status == STALE


def test_only_trades_silent_but_quotes_flow_is_not_stale():
    w = _wd()
    _feed_both(w, T0, 120)
    for s in range(120, 1200, 20):                       # 호가만 계속
        w.note_data(QUOTE_TR, "RBAQMU", T0 + timedelta(seconds=s))
        assert w.poll(T0 + timedelta(seconds=s + 1)) == []
    assert w.status == OK and w.resubscribe_total == 0


def test_only_quotes_silent_but_trades_flow_is_not_stale():
    w = _wd()
    _feed_both(w, T0, 120)
    for s in range(120, 1200, 20):                       # 체결만 계속
        w.note_data(TRADE_TR, "RBAQMU", T0 + timedelta(seconds=s))
        assert w.poll(T0 + timedelta(seconds=s + 1)) == []
    assert w.status == OK and w.resubscribe_total == 0


def test_subscribe_ack_is_not_recovery_only_real_data_is():
    w = _wd()
    _feed_both(w, T0, 60)
    t = T0 + timedelta(seconds=300)
    assert "RESUBSCRIBE_ATTEMPT" in _kinds(w.poll(t))
    assert w.note_reply(TRADE_TR, "RBAQMU", True, "UNSUBSCRIBE SUCCESS", t) == []      # 해제 응답 무시
    assert w.note_reply(TRADE_TR, "RBAQMU", True, "SUBSCRIBE SUCCESS", t) == []
    ev = w.note_reply(QUOTE_TR, "RBAQMU", True, "SUBSCRIBE SUCCESS", t)
    assert _kinds(ev) == ["RESUBSCRIBE_OK"] and w.status == STALE                  # 응답만으로는 아직 STALE
    ev = w.note_data(QUOTE_TR, "RBAQMU", t + timedelta(seconds=5))
    assert _kinds(ev) == ["RECOVERED"] and ev[0]["by"] == "quote" and w.status == OK
    iv = w.summary()["mu_stale_intervals"][0]
    assert iv["recovery_success"] is True and iv["stale_end"] and iv["resubscribe_count"] == 1


def test_retry_schedule_immediate_60s_then_5min_and_gives_up_without_loop():
    w = _wd(max_attempts=4)
    _feed_both(w, T0, 60)
    t = T0 + timedelta(seconds=300)
    attempts = []
    for s in range(0, 3 * 3600, 5):                       # 3시간 동안 5초마다 poll, 응답 없음(전부 timeout)
        now = t + timedelta(seconds=s)
        for e in w.poll(now):
            if e["kind"] == "RESUBSCRIBE_ATTEMPT":
                attempts.append(s)
    assert len(attempts) == 4                                  # 무한루프 없음
    gaps = [b - a for a, b in zip(attempts, attempts[1:])]
    assert attempts[0] == 0 and gaps[0] == 60 and all(g == 300 for g in gaps[1:])
    assert w.status == GAVE_UP and w.health() == "MU_STALE"


def test_resubscribe_fail_reply_and_reply_timeout_are_reported():
    w = _wd()
    _feed_both(w, T0, 60)
    t = T0 + timedelta(seconds=300)
    w.poll(t)
    ev = w.note_reply(TRADE_TR, "RBAQMU", False, "SUBSCRIBE ERROR", t)
    assert _kinds(ev) == ["RESUBSCRIBE_FAIL"] and ev[0]["reason"].startswith("reply:")
    ev = w.poll(t + timedelta(seconds=60))                                # 2차 시도 -> 응답 없음
    assert "RESUBSCRIBE_ATTEMPT" in _kinds(ev)
    ev = w.poll(t + timedelta(seconds=91))
    assert any(e["kind"] == "RESUBSCRIBE_FAIL" and e["reason"] == "reply_timeout" for e in ev)


def test_no_new_attempt_while_lease_is_held():
    w = _wd(reply_timeout=500.0)
    _feed_both(w, T0, 60)
    t = T0 + timedelta(seconds=300)
    n = sum(_kinds(w.poll(t + timedelta(seconds=s))).count("RESUBSCRIBE_ATTEMPT") for s in range(0, 400, 5))
    assert n == 1                                    # 응답 대기(lease) 중엔 60초가 지나도 다시 보내지 않는다


def test_dnasmu_is_tracked_separately_and_never_triggers_or_recovers():
    w = _wd()
    _feed_both(w, T0, 600, feed="DNASMU")          # DNASMU 만 오고 RBAQMU 는 처음부터 조용
    assert w.last_at[(TRADE_TR, "DNASMU")] and w.counts[(QUOTE_TR, "DNASMU")] == 60
    ev = w.poll(T0 + timedelta(seconds=181))
    assert _kinds(ev)[0] == "STALE" and ev[0]["feed"] == "RBAQMU"
    assert w.note_data(TRADE_TR, "DNASMU", T0 + timedelta(seconds=200)) == []   # DNASMU 로는 복구 아님
    assert w.status == STALE


@pytest.mark.parametrize("t", [datetime(2026, 10, 21, 8, 30), datetime(2026, 10, 21, 15, 40),
                               datetime(2026, 10, 24, 12, 0)])               # 장전 / 장후 / 토요일
def test_watchdog_is_inactive_outside_korean_session(t):
    w = MuWatchdog()
    w.on_connected(t)
    assert w.poll(t + timedelta(seconds=1000)) == [] and w.status == OK


def test_open_interval_is_closed_without_recovery_at_window_end():
    w = _wd()
    _feed_both(w, T0, 60)
    w.poll(T0 + timedelta(minutes=10))
    ev = w.poll(datetime(2026, 10, 21, 15, 30, 5))
    assert _kinds(ev) == ["STALE_CLOSED"]
    iv = w.summary()["mu_stale_intervals"][0]
    assert iv["recovery_success"] is False and iv["closed_by"] == "watch_window_end"


def test_low_prior_rate_is_still_stale_but_marked_weak():
    w = _wd()
    w.note_data(TRADE_TR, "RBAQMU", T0)
    w.note_data(QUOTE_TR, "RBAQMU", T0 + timedelta(seconds=1))
    ev = w.poll(T0 + timedelta(seconds=200))
    assert ev[0]["kind"] == "STALE" and ev[0]["severity"] == "WEAK"


def test_config_defaults_need_no_env(monkeypatch):
    monkeypatch.delenv(config.ENV_MU_WATCHDOG, raising=False)
    monkeypatch.delenv(config.ENV_MU_STALE_SEC, raising=False)
    assert config.mu_watchdog_enabled() is True and config.mu_stale_sec() == 180.0
    monkeypatch.setenv(config.ENV_MU_STALE_SEC, "abc")
    assert config.mu_stale_sec() == 180.0
    monkeypatch.setenv(config.ENV_MU_STALE_SEC, "5")
    assert config.mu_stale_sec() == 30.0


# ── 통합: LoggerService + 가짜 웹소켓 ───────────────────────────────────────
def _sys_reply(tr, key, ok=True, unsub=False):
    return json.dumps({"header": {"tr_id": tr, "tr_key": key, "encrypt": "N"},
                       "body": {"rt_cd": "0" if ok else "1", "msg_cd": "OPSP0000" if ok else "OPSP9999",
                                "msg1": ("UNSUBSCRIBE SUCCESS" if unsub else "SUBSCRIBE SUCCESS") if ok
                                else "SUBSCRIBE ERROR"}})


class ScriptWS:
    """국내 프레임은 계속 흘리고(MU 는 처음 잠깐만), RBAQMU 재등록이 오면 응답(+선택: 실데이터)을 붙인다."""

    def __init__(self, start, *, recover=True, reply_ok=True, mu_seconds=20):
        self.start = start
        self.sent, self.pongs = [], []
        self.recover, self.reply_ok = recover, reply_ok
        dom = stub.session(start, seconds=600)
        mu = [f for t, f in stub.mu_session(start, minutes=1, per_min=6) if t < start + timedelta(seconds=mu_seconds)]
        mq = [f for t, f in stub.mu_quote_session(start, minutes=1, per_min=6) if t < start + timedelta(seconds=mu_seconds)]
        self.frames = mu + mq + [f for _, f in dom]
        self.replies = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, m):
        self.sent.append(json.loads(m))
        if len(self.sent) <= 10:
            return
        h, i = self.sent[-1]["header"], self.sent[-1]["body"]["input"]
        if i["tr_key"] == "RBAQMU":            # 응답은 보낸 순서대로, 실데이터는 마지막 등록 응답 뒤에
            self.replies.append(_sys_reply(i["tr_id"], "RBAQMU", ok=(self.reply_ok or h["tr_type"] == "2"),
                                           unsub=h["tr_type"] == "2"))
            if self.recover and h["tr_type"] == "1" and i["tr_id"] == "HDFSASP0":
                self.replies.append(stub.mu_quote("RBAQMU", self.start, 120.0, 120.02, 5, 7))

    async def pong(self, d):
        self.pongs.append(d)

    async def recv(self):
        if self.replies:
            await asyncio.sleep(0.002)
            return self.replies.pop(0)
        if self.frames:
            await asyncio.sleep(0.002)
            return self.frames.pop(0)
        await asyncio.sleep(3600)


def _clock(start, speed):
    t0 = time.monotonic()
    return lambda: start + timedelta(seconds=(time.monotonic() - t0) * speed)


def _run(root, ws, now_fn, until, timeout=15.0):
    lines = []
    svc = service.LoggerService(root, config.Credentials("LOGKEY", "LOGSEC"), max_rss_mb=500, connect=lambda u: ws,
                                now_fn=now_fn, issue=lambda c, r, n: "AK", rss_fn=lambda: 50.0, out=lines.append,
                                health_every=0.2, recv_timeout=0.01, idle_poll=0.02, backoff_max=0.05)

    async def go():
        task = asyncio.create_task(svc.run())
        t_end = time.monotonic() + timeout
        while not until(lines) and time.monotonic() < t_end and not task.done():
            await asyncio.sleep(0.02)
        svc.request_stop("test")
        return await task
    asyncio.run(go())
    return svc, lines


def _wd_lines(lines, kind):
    return [x for x in lines if f"[ws_shadow_logger][MU_WATCHDOG] {kind} " in x]


def test_service_mu_only_stall_resubscribes_rbaqmu_only_and_recovers(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_MU_STALE_SEC, "30")
    start = datetime(2026, 10, 21, 12, 40, 0)
    ws = ScriptWS(start, recover=True)
    svc, lines = _run(tmp_path / "lg", ws, _clock(start, 20.0), lambda ls: _wd_lines(ls, "RECOVERED"))
    for k in ("STALE", "RESUBSCRIBE_ATTEMPT", "RESUBSCRIBE_OK", "RECOVERED"):
        assert _wd_lines(lines, k), k
    assert "feed=RBAQMU" in _wd_lines(lines, "STALE")[0]
    first10, extra = ws.sent[:10], ws.sent[10:]
    assert [(m["body"]["input"]["tr_id"], m["body"]["input"]["tr_key"]) for m in first10] == list(config.all_subscriptions())
    # 재구독은 RBAQMU 체결·호가 2건의 해제->등록 뿐 — 국내 6건 / DNASMU 에는 아무것도 보내지 않는다
    assert [(m["header"]["tr_type"], m["body"]["input"]["tr_id"], m["body"]["input"]["tr_key"]) for m in extra] == [
        ("2", "HDFSCNT0", "RBAQMU"), ("1", "HDFSCNT0", "RBAQMU"), ("2", "HDFSASP0", "RBAQMU"), ("1", "HDFSASP0", "RBAQMU")]
    # 중복 등록 0: 모든 (tr, key) 의 등록-해제 = 1
    net = {}
    for m in ws.sent:
        k = (m["body"]["input"]["tr_id"], m["body"]["input"]["tr_key"])
        net[k] = net.get(k, 0) + (1 if m["header"]["tr_type"] == "1" else -1)
    assert set(net.values()) == {1} and len(net) == 10
    # 전체 재접속 없음, 국내 스냅샷은 계속 쌓였다
    c = json.loads((tmp_path / "lg" / "state" / "counters_2026-10-21.json").read_text())
    assert c.get("reconnect", 0) == 0 and c["snapshot"] > 30
    wdj = json.loads((tmp_path / "lg" / "mu" / "2026-10-21" / "watchdog.json").read_text(encoding="utf-8"))
    assert wdj["mu_stale_intervals"][0]["recovery_success"] is True and wdj["resubscribe_count"] == 1
    assert any("MU_WD=" in x for x in lines if "HEALTH" in x)


def test_service_resubscribe_failure_keeps_stale_and_no_reconnect(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_MU_STALE_SEC, "30")
    start = datetime(2026, 10, 21, 12, 40, 0)
    ws = ScriptWS(start, recover=False, reply_ok=False)
    svc, lines = _run(tmp_path / "lg", ws, _clock(start, 20.0), lambda ls: _wd_lines(ls, "RESUBSCRIBE_FAIL"))
    assert _wd_lines(lines, "RESUBSCRIBE_FAIL") and not _wd_lines(lines, "RECOVERED")
    c = json.loads((tmp_path / "lg" / "state" / "counters_2026-10-21.json").read_text())
    assert c.get("reconnect", 0) == 0
    # 세션 종료 시 열린 구간은 복구 실패로 닫혀 manifest 로 간다
    wdj = json.loads((tmp_path / "lg" / "mu" / "2026-10-21" / "watchdog.json").read_text(encoding="utf-8"))
    iv = wdj["mu_stale_intervals"][0]
    assert iv["recovery_success"] is False and iv["stale_end"]
    man = export_day.build_manifest(tmp_path / "lg", "2026-10-21",
                                    export_day.sources(tmp_path / "lg", "2026-10-21"), datetime(2026, 10, 21, 15, 40))
    assert man["mu_stale_intervals"] and man["mu_resubscribe_count"] >= 1 and man["mu_recovery_success"] is False


def test_service_before_0900_watchdog_does_not_fire(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_MU_STALE_SEC, "30")
    start = datetime(2026, 10, 21, 8, 10, 0)
    ws = ScriptWS(start)
    svc, lines = _run(tmp_path / "lg", ws, _clock(start, 20.0), lambda ls: False, timeout=3.0)
    assert not _wd_lines(lines, "STALE") and len(ws.sent) == 10


def test_service_watchdog_can_be_disabled_by_optional_env(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_MU_STALE_SEC, "30")
    monkeypatch.setenv(config.ENV_MU_WATCHDOG, "0")
    start = datetime(2026, 10, 21, 12, 40, 0)
    ws = ScriptWS(start)
    svc, lines = _run(tmp_path / "lg", ws, _clock(start, 20.0), lambda ls: False, timeout=3.0)
    assert not _wd_lines(lines, "STALE") and len(ws.sent) == 10
