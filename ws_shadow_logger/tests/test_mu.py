"""MU 실시간 체결(HDFSCNT0) 기록 계약 (2026-10-15).

  1. HDFSCNT0 프레임 파싱(26필드, 다건) -> mu/YYYY-MM-DD/ticks.csv, bars_1m.csv
  2. 분봉 기준은 KST(KYMD/KHMS). 하이닉스 스냅샷 ts(KST)와 같은 분 문자열로 join 된다
  3. 분봉 확정: 다음 분 체결 또는 시계(분 끝 + grace)
  4. 재시작: 같은 날 이어쓰기, 이미 쓴 분 중복 없음, 장 마감 후 ticks 로 재생성하면 정확
  5. 격리: MU 경로 예외/디스크 오류/구독 거부가 국내 스냅샷을 한 줄도 바꾸지 않는다
  6. 등록 순서(국내 먼저), KIS_LOGGER_MU_ENABLED=0, 짧은 연결 반복 시 그날 MU 제외
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import json
from datetime import datetime, timedelta

import pytest

from ws_shadow_logger import config, maintenance, mu, runner, service, stub
from ws_shadow_logger.engine import Engine
from ws_shadow_logger.parser import parse_frame
from ws_shadow_logger.storage import Store

from .test_service import FakeWS, _clock, _run_until, _svc

DAY = "2026-10-15"
T0 = datetime(2026, 10, 15, 10, 0, 0)


@pytest.fixture(autouse=True)
def _mu_env(monkeypatch):
    monkeypatch.delenv(config.ENV_MU_ENABLED, raising=False)


def _read(p):
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _assert_domestic_same(root, base):
    """국내 스냅샷은 MU 유무와 바이트 단위로 같다. MU 프레임이 더 늦게까지 오면 시계가 더 흘러
    뒤에 (체결 없는) 초가 더 붙을 수 있으므로, MU 없는 쪽 전체가 앞부분과 정확히 같아야 한다."""
    for s in ("000660", "0193T0", "0197X0"):
        a = (root / "snapshots" / DAY / f"{s}.csv").read_bytes()
        b = (base / "snapshots" / DAY / f"{s}.csv").read_bytes()
        assert len(b) > 1000 and a[:len(b)] == b, s


def _replay(tmp_path, frames, name="out"):
    src = tmp_path / f"{name}.txt"
    stub.write(src, frames)
    return runner.replay(str(src), root=tmp_path / name), tmp_path / name


def test_parse_hdfscnt0_multi_record():
    m = parse_frame(stub.mu_trade("RBAQMU", T0, 120.5, 7, 700, recs=3))
    assert m["type"] == "data" and m["tr_id"] == "HDFSCNT0" and len(m["records"]) == 3
    r = m["records"][0]
    assert len(r) == 26 and r["RSYM"] == "RBAQMU" and r["SYMB"] == "MU"
    assert r["KHMS"] == "100000" and r["LAST"] == "120.5"
    # 다건 프레임에서 **마지막** 레코드까지 밀리지 않는다 (2026-10-16 실측 결함의 회귀테스트)
    last = m["records"][-1]
    assert last["RSYM"] == "RBAQMU" and last["KHMS"] == "100000" and last["LAST"] == "120.5"


def test_ticks_and_bars_are_written_and_domestic_snapshots_unchanged(tmp_path):
    dom = stub.session(T0, seconds=150)
    mus = stub.mu_session(T0, minutes=3)
    _, base = _replay(tmp_path, dom, "dom_only")
    eng, root = _replay(tmp_path, sorted(dom + mus, key=lambda x: x[0]), "with_mu")
    assert eng.mu_errors == 0
    ticks = _read(root / "mu" / DAY / "ticks.csv")
    assert len(ticks) == len(mus)
    assert all(t["feed"] == "RBAQMU" and 0.29 <= float(t["lag_s"]) <= 0.31 for t in ticks)
    bars = _read(root / "mu" / DAY / "bars_1m.csv")
    expect = mu.build_bars(ticks)
    assert [b["minute_kst"] for b in bars] == ["2026-10-15T10:00:00", "2026-10-15T10:01:00", "2026-10-15T10:02:00"]
    for b, e in zip(bars, expect):
        assert (float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"]), float(b["volume"]),
                int(b["ticks"])) == (e["open"], e["high"], e["low"], e["close"], e["volume"], e["ticks"])
    assert [b["complete"] for b in bars] == ["1", "1", "0"] or [b["complete"] for b in bars] == ["1", "1", "1"]
    _assert_domestic_same(root, base)
    assert not (root / "snapshots" / DAY / "MU.csv").exists() and not (root / "snapshots" / DAY / "RBAQMU.csv").exists()


def test_bar_minute_is_kst_and_joins_hynix_snapshot_ts(tmp_path):
    dom = stub.session(T0, seconds=70)
    mus = stub.mu_session(T0, minutes=1)
    _, root = _replay(tmp_path, sorted(dom + mus, key=lambda x: x[0]))
    bar = _read(root / "mu" / DAY / "bars_1m.csv")[0]
    snaps = _read(root / "snapshots" / DAY / "000660.csv")
    same_minute = [s for s in snaps if s["ts"][:16] == bar["minute_kst"][:16]]
    assert bar["minute_kst"] == "2026-10-15T10:00:00" and len(same_minute) >= 59     # 10:00:01~10:00:59
    t = _read(root / "mu" / DAY / "ticks.csv")[0]
    assert t["kst_ts"].startswith("2026-10-15T10:00") and t["local_hms"].startswith("21")      # ET = KST-13h


def test_bar_is_finalized_by_clock_when_the_next_minute_is_quiet(tmp_path):
    rec = mu.MuRecorder(tmp_path)
    m = parse_frame(stub.mu_trade("RBAQMU", T0 + timedelta(seconds=10), 120.0, 5, 5))
    rec.on_records(m["records"], T0 + timedelta(seconds=10.3))
    rec.advance(T0 + timedelta(seconds=64)); rec.flush()
    assert len(_read(tmp_path / "mu" / DAY / "bars_1m.csv")) == 0
    rec.advance(T0 + timedelta(seconds=65.1)); rec.flush()
    rows = _read(tmp_path / "mu" / DAY / "bars_1m.csv")
    assert len(rows) == 1 and rows[0]["complete"] == "1" and rows[0]["volume"] == "5.0"
    rec.close()


def test_feeds_are_kept_apart(tmp_path):
    rec = mu.MuRecorder(tmp_path)
    for feed, px in (("DNASMU", 119.0), ("RBAQMU", 121.0)):
        rec.on_records(parse_frame(stub.mu_trade(feed, T0, px, 3, 3))["records"], T0)
    rec.advance(T0 + timedelta(seconds=70)); rec.close()
    rows = _read(tmp_path / "mu" / DAY / "bars_1m.csv")
    assert {(r["feed"], r["close"]) for r in rows} == {("DNASMU", "119.0"), ("RBAQMU", "121.0")}


def test_restart_mid_minute_then_finalize_rebuilds_exact_bars(tmp_path):
    frames = stub.mu_session(T0, minutes=3, per_min=10)
    cut = T0 + timedelta(minutes=1, seconds=30)
    for chunk in ([f for f in frames if f[0] < cut], [f for f in frames if f[0] >= cut]):
        st = Store(tmp_path); eng = Engine(st, runner.symbols())
        for t, f in chunk:
            eng.on_frame(f, t)
        eng.advance(chunk[-1][0]); eng.close(); st.close()
    live = _read(tmp_path / "mu" / DAY / "bars_1m.csv")
    mins = [b["minute_kst"] for b in live]
    assert len(mins) == len(set(mins)), "재시작 후 같은 분을 두 번 쓰지 않는다"
    assert any(b["complete"] == "0" for b in live), "재시작으로 끊긴 분은 complete=0"
    ticks = _read(tmp_path / "mu" / DAY / "ticks.csv")
    assert len(ticks) == len(frames), "tick 은 재시작 전후 모두 남는다"
    res = maintenance.run(tmp_path, datetime(2026, 10, 15, 15, 31))
    assert res["mu"] == [DAY]
    final = _read(tmp_path / "mu" / DAY / "bars_1m.csv")
    assert final == [{k: str(v) for k, v in b.items()} for b in mu.build_bars(ticks)]
    assert all(b["ticks"] == "10" and b["complete"] == "1" for b in final)
    assert not (tmp_path / "mu" / DAY / "ticks.csv").exists()
    with gzip.open(tmp_path / "mu" / DAY / "ticks.csv.gz", "rt", encoding="utf-8") as fh:
        assert sum(1 for _ in csv.DictReader(fh)) == len(frames)
    summ = json.loads((tmp_path / "mu" / DAY / "summary.json").read_text(encoding="utf-8"))
    assert summ["feeds"]["RBAQMU"]["bars"] == 3 and summ["feeds"]["RBAQMU"]["missing_ratio"] == 0.0


def test_observe_reports_feed_counts_missing_and_lag(tmp_path):
    rec = mu.MuRecorder(tmp_path)
    for m in (0, 1, 3):                                   # 10:02 체결 없음
        t = T0 + timedelta(minutes=m, seconds=5)
        rec.on_records(parse_frame(stub.mu_trade("RBAQMU", t, 120.0, 2, 2))["records"], t + timedelta(seconds=0.4))
    rec.advance(T0 + timedelta(minutes=5))
    ob = rec.observe(); rec.close()
    assert ob["feeds"]["RBAQMU"] == {"ticks": 3, "bars": 3, "span": 4, "missing_ratio": 0.25}
    assert ob["lag"]["p50"] == 0.4 and ob["lag"]["n"] == 3


def test_missing_ratio_counts_minutes_without_trades():
    bars = [{"feed": "RBAQMU", "minute_kst": f"2026-10-15T10:{m:02d}:00"} for m in (0, 1, 3, 4, 9)]
    s = mu.summarize_bars(bars)["RBAQMU"]
    assert (s["span_minutes"], s["missing_minutes"], s["missing_ratio"]) == (10, 5, 0.5)


def test_maintenance_never_touches_mu_during_session(tmp_path):
    rec = mu.MuRecorder(tmp_path)
    rec.on_records(parse_frame(stub.mu_trade("RBAQMU", T0, 120.0, 5, 5))["records"], T0); rec.close()
    assert maintenance.run(tmp_path, datetime(2026, 10, 15, 12, 0)) == {"skipped": "in_session"}
    assert (tmp_path / "mu" / DAY / "ticks.csv").exists()


# ── 격리 ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("err", [OSError(28, "No space left on device"), ValueError("bad"), RuntimeError("x")])
def test_mu_exceptions_never_reach_domestic(tmp_path, monkeypatch, err):
    dom = stub.session(T0, seconds=40)
    mus = stub.mu_session(T0, minutes=1, per_min=60)
    _, base = _replay(tmp_path, dom, "base")

    def boom(self, *a):
        raise err
    monkeypatch.setattr(mu.MuRecorder, "on_records", boom)
    monkeypatch.setattr(mu.MuRecorder, "advance", boom)
    eng, root = _replay(tmp_path, sorted(dom + mus, key=lambda x: x[0]), "broken")
    _assert_domestic_same(root, base)
    assert eng.mu_disabled and eng.mu_errors == config.MU_MAX_ERRORS
    events = [json.loads(x)["event"] for x in open(root / "logs" / f"{DAY}.jsonl", encoding="utf-8")]
    assert events.count("mu_error") == config.MU_MAX_ERRORS and "mu_disabled" in events


def test_unwritable_mu_dir_does_not_stop_domestic(tmp_path):
    dom = stub.session(T0, seconds=20)
    mus = stub.mu_session(T0, minutes=1)
    _, base = _replay(tmp_path, dom, "base")
    root = tmp_path / "blocked"
    root.mkdir(); (root / "mu").write_text("not a dir")             # mu/ 를 만들 수 없다
    src = tmp_path / "f.txt"; stub.write(src, sorted(dom + mus, key=lambda x: x[0]))
    eng = runner.replay(str(src), root=root)
    assert eng.mu_errors > 0 and eng.counts["error"] == 0
    _assert_domestic_same(root, base)


def test_mu_subscribe_rejection_is_logged_and_domestic_continues(tmp_path):
    rej = json.dumps({"header": {"tr_id": "HDFSCNT0", "tr_key": "RBAQMU", "encrypt": "N"},
                      "body": {"rt_cd": "1", "msg_cd": "OPSP8996", "msg1": "invalid tr_key"}})
    dom = stub.session(T0, seconds=20)
    _, base = _replay(tmp_path, dom, "base")
    eng, root = _replay(tmp_path, sorted(dom + [(T0, rej)], key=lambda x: x[0]), "rej")
    _assert_domestic_same(root, base)
    ev = [json.loads(x) for x in open(root / "logs" / f"{DAY}.jsonl", encoding="utf-8")]
    assert any(e["event"] == "mu_subscribe_failed" and e["tr_key"] == "RBAQMU" for e in ev)


# ── 등록 ──────────────────────────────────────────────────────────────────
def test_domestic_first_then_mu_and_env_off(monkeypatch):
    """국내 6 -> MU 체결 2 -> MU 호가 2 순서. 호가만 따로 끌 수도 있다 (2026-10-19)."""
    subs = config.all_subscriptions()
    assert subs[:6] == config.SUBSCRIPTIONS
    assert subs[6:8] == (("HDFSCNT0", "RBAQMU"), ("HDFSCNT0", "DNASMU"))
    assert subs[8:] == (("HDFSASP0", "RBAQMU"), ("HDFSASP0", "DNASMU"))
    monkeypatch.setenv(config.ENV_MU_QUOTE_ENABLED, "0")
    assert config.all_subscriptions() == config.SUBSCRIPTIONS + config.MU_SUBSCRIPTIONS
    monkeypatch.setenv(config.ENV_MU_ENABLED, "0")
    assert config.all_subscriptions() == config.SUBSCRIPTIONS
    monkeypatch.delenv(config.ENV_MU_QUOTE_ENABLED)
    assert config.all_subscriptions() == config.SUBSCRIPTIONS      # MU 가 꺼지면 호가도 안 붙는다


def _sub_reply(key, ok=True):
    return json.dumps({"header": {"tr_id": "HDFSCNT0", "tr_key": key, "encrypt": "N"},
                       "body": {"rt_cd": "0" if ok else "1", "msg_cd": "OPSP0000" if ok else "OPSP8996",
                                "msg1": "SUBSCRIBE SUCCESS" if ok else "invalid"}})


def test_live_session_records_mu_and_health_reports_it(tmp_path):
    """첫날 확인용 로그: 구독 성공/실패, tick 수, 분봉 수·누락률, 수신지연, RSS 가 stdout(Render Logs)에 찍힌다."""
    frames = sorted(stub.session(T0, seconds=20) + stub.mu_session(T0, minutes=3)
                    + [(T0, _sub_reply("RBAQMU")), (T0, _sub_reply("DNASMU", ok=False))], key=lambda x: x[0])
    ws = FakeWS(frames, idle=True)
    svc, lines = _svc(tmp_path, connect=lambda url: ws, now_fn=_clock(T0, speed=20.0))
    rc = _run_until(svc, lambda: not ws.frames and any("MU_BARS=" in x and "MU_BARS=0 " not in x for x in lines))
    assert rc == 0
    assert any("MU_SUBSCRIBE OK tr_key=RBAQMU" in x for x in lines)
    assert any("MU_SUBSCRIBE FAIL tr_key=DNASMU msg_cd=OPSP8996" in x for x in lines)
    h = [x for x in lines if "HEALTH" in x and "MU_BARS=0 " not in x][-1]
    for k in ("MU_SUB=DNASMU:FAIL(OPSP8996),RBAQMU:OK", "MU_FEEDS=RBAQMU:", "/miss=", "MU_LAG=p50=", "RSS_MB=50.0"):
        assert k in h, (k, h)
    sent = [json.loads(m)["body"]["input"] for m in ws.sent]
    assert len(sent) == 10
    assert [(s["tr_id"], s["tr_key"]) for s in sent[6:]] == [
        ("HDFSCNT0", "RBAQMU"), ("HDFSCNT0", "DNASMU"), ("HDFSASP0", "RBAQMU"), ("HDFSASP0", "DNASMU")]
    assert any("MU=ON" in x for x in lines)
    assert (tmp_path / "mu" / DAY / "ticks.csv").exists()


def test_repeated_short_connections_drop_mu_for_the_day(tmp_path):
    """MU 를 붙인 연결이 곧바로 끊기기를 3번 -> 그날은 국내 6건만 등록한다."""
    conns = []

    def connect(url):
        ws = FakeWS([] if len(conns) < 3 else stub.session(T0, seconds=5), idle=len(conns) >= 3)
        conns.append(ws)
        return ws
    svc, lines = _svc(tmp_path, connect=connect, now_fn=_clock(T0, speed=1.0))
    rc = _run_until(svc, lambda: len(conns) >= 4 and not conns[3].frames)
    assert rc == 0
    assert [len(c.sent) for c in conns[:4]] == [10, 10, 10, 6]      # 체결+호가 둘 다 빠진다
    assert any("MU_DISABLED_TODAY" in x for x in lines)
    assert any("MU=OFF_TODAY" in x for x in lines)
