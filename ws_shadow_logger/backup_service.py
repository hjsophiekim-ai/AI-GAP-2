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
  · 15:40 이후 MACD2 원장 일별 R2 백업을 별도 프로세스(python -m ledger_backup --auto)로 1회 실행([DAILY_LEDGER_BACKUP]).
  · 1차본(2026-11-06): 15:00 강제청산 직후 바로 분석할 수 있도록 15:05~15:35 에 그날 export 1차본
    (ws_shadow_export_<날짜>_early.zip — 하이닉스·레버리지·인버스 스냅샷(호가·체결강도 cttr)·MU)을,
    15:05~15:40 에 원장 1차본(ledger_backup --auto-early, early/ 키)을 올린다. 1차본은 최종본과 파일명·키가 달라
    서로 덮어쓰지 않고 둘 다 R2 에 남는다. 상태파일도 따로라 최종본 일정·재시도에 영향이 없다.
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
EARLY_RUN_AFTER = time(15, 5)      # 1차본 export — 15:00 강제청산 직후
EARLY_SUFFIX = "_early"
EARLY_MAX_ATTEMPTS = 3
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
    if backup_on:
        r = early_export(root, now, client_factory=client_factory)
        if r:
            out.append(r)
    spawn_ledger_backup(root, now, env=env, early=True)
    spawn_ledger_backup(root, now, env=env)
    return out


def early_export(root: Path, now: datetime, *, client_factory) -> dict | None:
    """오늘 1차본 export·업로드 (15:05~15:35, 하루 OK 1회·실패 최대 3회·5분 간격). 예외를 밖으로 내지 않는다.
    zip·sidecar·R2 키 모두 _early 접미사라 최종본(15:35 이후)과 겹치지 않는다."""
    day = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5 or not (EARLY_RUN_AFTER <= now.time() < RUN_AFTER):
        return None
    sp = Path(root) / "state" / f"backup_early_{day}.json"
    try:
        st = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    if st.get("status") == "BACKED_UP" or int(st.get("attempts", 0)) >= EARLY_MAX_ATTEMPTS:
        return None
    if st.get("last_attempt_at") and (now - datetime.fromisoformat(st["last_attempt_at"])).total_seconds() < 300:
        return None
    att = int(st.get("attempts", 0)) + 1
    try:
        ex = export_day.export(root, day, now=now, suffix=EARLY_SUFFIX)
        if ex["status"] == "NO_DATA":
            return {"day": day, "action": "EARLY_NO_DATA"}
        man, zp = ex["manifest"], Path(ex["zip"])
        client = client_factory()
        key = client.t.key_for(day, zp.name)
        sha = man["zip_sha256"]
        if client.head_sha256(key) != sha:
            client.put_file(key, zp, sha)
            if client.head_sha256(key) != sha:
                raise IOError("업로드 후 HEAD sha256 불일치")
        body = {k: v for k, v in man.items() if k != "files"}
        body.update(uploaded_key=key, files=man.get("files", []))
        client.put_bytes(client.t.key_for(day, "manifest_early.json"),
                         json.dumps(body, ensure_ascii=False, indent=1).encode("utf-8"))
        st = {"status": "BACKED_UP", "key": key, "zip_sha256": sha, "attempts": att,
              "last_attempt_at": now.isoformat(timespec="seconds")}
        try:                                                 # R2 에 sha 검증된 사본이 있으니 로컬 zip 사본만 지운다
            zp.unlink()                                      # (원본 snapshots/mu/logs 는 건드리지 않음, 디스크 2배 방지)
        except OSError:
            pass
        _p(f"DAILY_EARLY_BACKUP_OK day={day} key={key} snapshot_rows={man.get('snapshot_rows')} "
           f"mu_ticks={man.get('mu_tick_rows')} bytes={man.get('zip_bytes')}")
        action = "EARLY_BACKED_UP"
    except Exception as e:                                   # noqa: BLE001 — 격리
        st = {"status": "FAIL", "attempts": att, "last_error": repr(e)[:300],
              "last_attempt_at": now.isoformat(timespec="seconds")}
        _p(f"DAILY_EARLY_BACKUP_FAIL day={day} attempt={att} error={repr(e)[:200]}")
        action = "EARLY_FAIL"
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        tmp = sp.with_name(sp.name + ".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, sp)
    except OSError:
        pass
    return {"day": day, "action": action}


# ── MACD2 원장 일별 R2 백업 (2026-11-05) ─────────────────────────────────────
# 원장 백업은 로거 패키지 밖(ledger_backup)에 있고, 여기서는 15:40 이후 그 모듈을 별도 프로세스로 실행만 한다
# (로거는 매매 상태 파일을 읽지 않는다는 격리 규칙 유지). 상태파일로 하루 최대 3회·5분 간격만 확인하고,
# 실패·타임아웃은 기록만 한다 — export/로거/UI/자동매매와 무관.
LEDGER_RUN_AFTER = time(15, 40)
LEDGER_MAX_ATTEMPTS = 3
LEDGER_TIMEOUT_SEC = 600


LEDGER_EARLY_RUN_AFTER = time(15, 5)


def _ledger_due(root: Path, now: datetime, env, early: bool = False) -> bool:
    if now.weekday() >= 5 or not backup.enabled(env):
        return False
    t = now.time()
    if (not (LEDGER_EARLY_RUN_AFTER <= t < LEDGER_RUN_AFTER)) if early else t < LEDGER_RUN_AFTER:
        return False
    if (env.get("LEDGER_BACKUP_ENABLED", "1") or "1").strip() == "0":
        return False
    name = f"ledger_backup_early_{now:%Y-%m-%d}.json" if early else f"ledger_backup_{now:%Y-%m-%d}.json"
    try:
        st = json.loads((Path(root) / "state" / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if st.get("status") == "OK" or int(st.get("attempts", 0)) >= LEDGER_MAX_ATTEMPTS:
        return False
    last = st.get("last_attempt_at")
    return not (last and (now - datetime.fromisoformat(last)).total_seconds() < 300)


def spawn_ledger_backup(root: Path, now: datetime, *, env=None, runner=None, early: bool = False):
    """due 이면 python -m ledger_backup --auto(early 면 --auto-early) 를 별도 프로세스로 1회 실행. 예외를 밖으로 내지 않는다."""
    env = env if env is not None else os.environ
    try:
        if not _ledger_due(root, now, env, early):
            return None
        import subprocess
        run = runner or subprocess.run
        r = run([sys.executable, "-m", "ledger_backup", "--auto-early" if early else "--auto"], cwd=str(config.REPO),
                env=dict(env),
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=LEDGER_TIMEOUT_SEC)
        for line in (getattr(r, "stdout", "") or "").splitlines()[-30:]:
            print(line, flush=True)
        return f"rc={getattr(r, 'returncode', '?')}"
    except Exception as e:                                   # noqa: BLE001 — 타임아웃 포함, 기록만
        _p(f"LEDGER_BACKUP_SPAWN_FAIL error={repr(e)[:200]}")
        return "error"


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
