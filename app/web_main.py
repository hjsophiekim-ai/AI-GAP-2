"""Render 웹 프로세스 진입점 (2026-11-16).

    python -m app.web_main --server.address 0.0.0.0 --server.port $PORT --server.headless true

``streamlit run app/ui/streamlit_app.py <args>`` 와 **같은 프로세스·같은 서버**를 띄운다
(streamlit 콘솔 스크립트가 하는 일 = ``streamlit.web.cli.main()`` 을 이 프로세스에서 호출).
차이는 하나 -- 그 전에 MACD2 headless 감시 스레드를 먼저 시작한다.

왜: Streamlit 은 브라우저 세션이 붙어야 streamlit_app.py 를 실행한다. 그래서 그 안에서만
돌던 worker 자동복구는 재배포 후 누군가 UI 를 열 때까지 멈춰 있었다(2026-11-13 09:19~10:17).
감시 스레드는 이 프로세스 안에서 ``service.get_service()`` 싱글턴만 쓰므로 UI 세션이
나중에 붙어도 같은 Macd2Service/worker 를 본다 -- 별도 worker 프로세스는 없다.

감시 스레드 시작 조건은 supervisor.supervisor_enabled() (Render 에서는 기본 ON,
로컬은 MACD2_SUPERVISOR_ENABLED=1 일 때만).
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STREAMLIT_SCRIPT = PROJECT_ROOT / "app" / "ui" / "streamlit_app.py"


def start_background_supervision(boot_reason: str = "process_start") -> bool:
    """감시 스레드 시작. 실패해도 웹 서버는 반드시 뜬다(예외 삼킴). 시작됐으면 True."""
    try:
        from app.trading.macd2 import supervisor
        return supervisor.ensure_running(boot_reason=boot_reason) is not None
    except Exception:
        try:
            from app.logger import logger
            logger.exception("[MACD2][SUPERVISOR] 부팅 감시 스레드 시작 실패 -- 웹은 계속 띄운다")
        except Exception:
            pass
        return False


def main(argv: list[str] | None = None) -> None:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    args = list(sys.argv[1:] if argv is None else argv)
    started = start_background_supervision("process_start")
    print(f"[web_main] macd2 supervisor started={started}", flush=True)
    from streamlit.web import cli as stcli

    sys.argv = ["streamlit", "run", str(STREAMLIT_SCRIPT), *args]
    sys.exit(stcli.main())


if __name__ == "__main__":
    main()
