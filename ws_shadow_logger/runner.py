"""WS SHADOW LOGGER 실행기 — READ-ONLY. 주문/REST 토큰/production 모듈 없음.

  python -m ws_shadow_logger.runner --check            설정·키·데이터경로·RSS 한도 검사 (네트워크 없음)
  python -m ws_shadow_logger.runner --live             상주 서비스 (KIS_LOGGER_ENABLED=1 일 때만). 평일 08:00~15:30 수집
  python -m ws_shadow_logger.runner --replay FILE --out DIR   원문/stub 프레임 재생 → 같은 저장 포맷 (네트워크 없음)
  python -m ws_shadow_logger.runner --maintain         장외 정리(gzip/일별 요약)를 지금 한 번 (수집시간이면 건너뜀)

--live 종료 코드: 0 정상/비활성, 2 키 문제, 3 데이터 경로 문제, 4 RSS 한도 초과 — 래퍼는 이 경우 재시작하지 않는다.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import config, maintenance
from .engine import Engine
from .service import TAG, LoggerService, now_kst, run_service, symbols
from .storage import Store


def replay(path: str, root) -> Engine:
    """'<ISO ts>\\t<frame>' 줄 파일(raw 저장 포맷과 동일, .gz 가능) 재생."""
    import gzip
    store = Store(Path(root))
    eng = Engine(store, symbols())
    op = gzip.open if path.endswith(".gz") else open
    last = None
    with op(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            ts, frame = line.rstrip("\n").split("\t", 1)
            last = datetime.fromisoformat(ts)
            eng.on_frame(frame, last)
    if last:
        eng.advance(last + timedelta(seconds=1))
    store.close()
    return eng


def _startup_checks(out=print):
    """(code, root, creds, (max_rss, src)). code != 0 이면 로거만 종료한다."""
    dc = config.check_data_dir()
    out(f"{TAG} DATA_DIR check: ok={dc.ok} path={dc.path or '-'} reason={dc.reason} "
        f"mount_point={dc.mount_point or '-'} on_render={dc.on_render} persistent={dc.persistent}")
    if not dc.ok:
        out(f"{TAG} FAIL-SAFE: data dir not usable — logger exits, UI/auto-trade keep running")
        return config.EXIT_DISK, None, None, None
    try:
        creds = config.load_credentials()
    except config.ConfigError as e:
        out(f"{TAG} FAIL-SAFE: {e} — logger exits, UI/auto-trade keep running")
        return config.EXIT_CONFIG, None, None, None
    rss = config.max_rss_mb()
    out(f"{TAG} READY key_fp={creds.fingerprint} subs={len(config.SUBSCRIPTIONS)} "
        f"session={config.SESSION_START:%H:%M}-{config.SESSION_END:%H:%M} KST max_rss_mb={rss[0]:.0f} ({rss[1]})")
    return config.EXIT_OK, Path(dc.path), creds, rss


def main(argv=None, out=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    out = out or (lambda line: print(line, flush=True))
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--live", action="store_true")
    g.add_argument("--replay")
    g.add_argument("--maintain", action="store_true")
    ap.add_argument("--out", help="--replay 저장 위치")
    a = ap.parse_args(argv)
    if a.replay:
        root = a.out or config.check_data_dir().path
        if not root:
            out("--out 또는 KIS_LOGGER_DATA_DIR 필요")
            return 2
        eng = replay(a.replay, root)
        out(str(eng.counts))
        return 0
    if a.maintain:
        dc = config.check_data_dir()
        if not dc.ok:
            out(f"{TAG} data dir not usable: {dc.reason}")
            return config.EXIT_DISK
        out(str(maintenance.run(Path(dc.path), now_kst())))
        return 0
    if a.live and not config.enabled():
        out(f"{TAG} DISABLED ({config.ENV_ENABLED}!=1) — not started")
        return config.EXIT_OK
    code, root, creds, rss = _startup_checks(out)
    if a.check or code != config.EXIT_OK:
        return code
    svc = LoggerService(root, creds, max_rss_mb=rss[0], rss_source=rss[1], out=out)
    return run_service(svc)


if __name__ == "__main__":
    sys.exit(main())
