"""일별 export + 외부 백업 상주 프로세스 — 로거 본체와 **별도 프로세스** (2026-10-16).

  python -m ws_shadow_logger.backup_service            # 상주 (scripts/render_start_with_logger.sh 가 감독)
  python -m ws_shadow_logger.backup_service --once     # 지금 한 번만 (수동 점검용)

동작 (KST):
  · 15:35 이후, 그날 데이터가 있으면 export zip 을 만든다(export_day). 15:30 maintenance 가 끝났다는 표식
    (daily/<날짜>.json) 을 기다리되, 16:00 이 지나면 그대로 진행한다.
  · LOGGER_BACKUP_ENABLED=1 이면 S3/R2 로 업로드(backup.upload_day). 아니면 export 만 한다.
  · 실패하면 상태파일(state/backup_<날짜>.json)에 남기고 5분→15분→30분→60분 간격으로 재시도한다.
    프로세스가 재시작되거나 다음 날이 돼도 상태파일을 읽어 이어서 재시도한다(최근 LOOKBACK_DAYS 일).
  · Render Logs: DAILY_EXPORT_OK / DAILY_BACKUP_OK / DAILY_BACKUP_FAIL / DAILY_BACKUP_SKIP 한 줄씩.
격리: 로거·UI·자동매매 코드를 import 하지 않고, 로거의 상태파일·raw 파일을 건드리지 않는다. 원본 데이터는 삭제하지 않는다.
어떤 예외도 이 프로세스 안에서 잡아 기록만 한다.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path

from . import backup, config, export_day

TAG = "[ws_shadow_backup]"
RUN_AFTER = time(15, 35)
FORCE_AFTER = time(16, 0)          # maintenance 표식(daily json)을 이 시각까지만 기다린다
LOOKBACK_DAYS = 10
RETRY_MIN = (5, 15, 30, 60)        # 실패 n 회째 다음 재시도까지 분 (마지막 값 반복)
TICK_SEC = 60
EXIT_OK, EXIT_DISK = 0, 3


def _p(msg: str):
    print(f"{TAG} {msg}", flush=True)


def state_path(root: Path, day: str) -> Path:
    return Path(root) / "state" / f"backup_{day}.json"


def load_state(root, day) -> dict:
    try:
        return json.loads(state_path(root, day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(root, day, st: dict):
    p = state_path(root, day)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def candidate_days(root: Path, now: datetime) -> list[str]:
    days = set()
    for sub in ("snapshots", "mu"):
        d = Path(root) / sub
        if d.exists():
            days |= {x.name for x in d.iterdir() if x.is_dir() and len(x.name) == 10}
    lo = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    out = []
    for day in sorted(days):
        if day < lo or day > today:
            continue
        if day == today:
            if now.time() < RUN_AFTER:
                continue
            if not (Path(root) / "daily" / f"{day}.json").exists() and now.time() < FORCE_AFTER:
                continue
        out.append(day)
    return out


def _next_retry(now: datetime, attempts: int) -> str:
    m = RETRY_MIN[min(attempts - 1, len(RETRY_MIN) - 1)]
    return (now + timedelta(minutes=m)).isoformat(timespec="seconds")


def process_day(root: Path, day: str, now: datetime, *, backup_on: bool, client_factory) -> dict:
    """한 날짜 처리. 상태를 갱신하고 결과 dict 를 반환. 예외는 밖으로 내보내지 않는다."""
    st = load_state(root, day)
    if st.get("next_retry_at") and now.isoformat() < st["next_retry_at"]:
        return {"day": day, "action": "WAIT", "next_retry_at": st["next_retry_at"]}
    try:
        ex = export_day.export(root, day, now=now)
    except Exception as e:                                   # noqa: BLE001 — 격리
        att = int(st.get("attempts", 0)) + 1
        st.update(status="EXPORT_FAIL", attempts=att, last_error=repr(e)[:300], updated_at=now.isoformat(),
                  next_retry_at=_next_retry(now, att))
        save_state(root, day, st)
        _p(f"DAILY_BACKUP_FAIL day={day} stage=export attempt={att} error={repr(e)[:200]} next_retry_at={st['next_retry_at']}")
        return {"day": day, "action": "EXPORT_FAIL"}
    if ex["status"] in ("NO_DATA", "NOT_FINISHED"):
        return {"day": day, "action": ex["status"]}
    man = ex["manifest"]
    sha = man.get("zip_sha256")
    if ex["status"] == "CREATED":
        _p(f"DAILY_EXPORT_OK day={day} zip={ex['zip']} files={man['file_count']} snapshot_rows={man['snapshot_rows']} "
           f"mu_ticks={man['mu_tick_rows']} bytes={man.get('zip_bytes')}")
    if not backup_on:
        if st.get("status") != "EXPORTED" or st.get("zip_sha256") != sha:
            st = {"status": "EXPORTED", "zip_sha256": sha, "updated_at": now.isoformat(), "attempts": 0}
            save_state(root, day, st)
        return {"day": day, "action": "EXPORTED"}
    if st.get("status") == "BACKED_UP" and st.get("zip_sha256") == sha:
        return {"day": day, "action": "ALREADY_BACKED_UP"}
    prov = "?"
    try:
        client = client_factory()
        prov = client.t.provider
        r = backup.upload_day(client, Path(ex["zip"]), day, man)
        st = {"status": "BACKED_UP", "provider": prov, "zip_sha256": sha, "key": r["key"], "result": r["status"],
              "uploaded_at": now.isoformat(), "updated_at": now.isoformat(), "attempts": 0,
              "target": client.t.describe()}
        save_state(root, day, st)
        _p(f"DAILY_BACKUP_OK provider={prov} day={day} result={r['status']} key={r['key']} sha256={sha[:12]} "
           f"bytes={man.get('zip_bytes')}")
        return {"day": day, "action": "BACKUP_" + r["status"]}
    except Exception as e:                                   # noqa: BLE001 — 격리 (키 원문은 메시지에 없다)
        att = int(st.get("attempts", 0)) + 1
        st.update(status="BACKUP_FAIL", provider=prov, zip_sha256=sha, attempts=att, last_error=repr(e)[:300],
                  updated_at=now.isoformat(), next_retry_at=_next_retry(now, att))
        save_state(root, day, st)
        _p(f"DAILY_BACKUP_FAIL provider={prov} day={day} stage=upload attempt={att} error={repr(e)[:200]} "
           f"next_retry_at={st['next_retry_at']}")
        return {"day": day, "action": "BACKUP_FAIL"}


def run_once(root: Path, now: datetime, *, env=None, client_factory=None) -> list[dict]:
    env = env if env is not None else os.environ
    backup_on = backup.enabled(env)
    if backup_on and client_factory is None:
        try:
            target = backup.load_target(env)
        except backup.BackupConfigError as e:
            prov = (env.get(backup.ENV["PROVIDER"]) or "s3").strip().lower()
            _p(f"DAILY_BACKUP_SKIP reason=config provider={prov} error={e}")    # 설정 오류: export 만 하고 업로드는 건너뛴다
            backup_on = False
        else:
            client_factory = lambda: backup.S3Client(target)  # noqa: E731
    out = []
    for day in candidate_days(root, now):
        try:
            out.append(process_day(root, day, now, backup_on=backup_on, client_factory=client_factory))
        except Exception as e:                               # noqa: BLE001
            _p(f"DAILY_BACKUP_FAIL day={day} stage=internal error={repr(e)[:200]}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ws_shadow_logger.backup_service")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    dc = config.check_data_dir()
    if not dc.ok:
        _p(f"FAIL-SAFE: data dir not usable ({dc.reason}) — backup service exits, logger/UI/auto-trade unaffected")
        return EXIT_DISK
    root = Path(dc.path)
    if backup.enabled():
        try:
            _p(f"READY backup=ON {backup.load_target().describe()} run_after={RUN_AFTER:%H:%M} KST")
        except backup.BackupConfigError as e:
            _p(f"READY backup=CONFIG_ERROR ({e}) — export only")
    else:
        _p(f"READY backup=OFF (export only) run_after={RUN_AFTER:%H:%M} KST root={root}")
    if a.once:
        for r in run_once(root, export_day.now_kst()):
            _p(f"ONCE {r}")
        return EXIT_OK
    stop = {"v": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: stop.__setitem__("v", True))
        except (ValueError, OSError):
            pass
    while not stop["v"]:
        try:
            run_once(root, export_day.now_kst())
        except Exception as e:                               # noqa: BLE001
            _p(f"loop error {repr(e)[:200]}")
        for _ in range(TICK_SEC):
            if stop["v"]:
                break
            _time.sleep(1)
    _p("stopped")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
