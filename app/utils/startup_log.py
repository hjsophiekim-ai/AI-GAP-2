"""startup_log.py — Render 등 배포 환경에서 앱 기동 단계를 추적하기 위한 로그 헬퍼.

STARTUP_STEP_START/DONE/FAILED 형식으로 남기며, 실패 시에도 예외 메시지 원문(비밀키/
계좌번호가 섞여 있을 수 있음)은 절대 로그에 남기지 않고 예외 타입명만 기록한다.
streamlit_app.py에서 분리한 이유: 이 모듈만 단독으로 import해 테스트할 수 있어야
Streamlit 스크립트 실행(백그라운드 스레드 기동 등 부작용 포함)을 트리거하지 않고
로그 동작만 검증할 수 있다.
"""

from __future__ import annotations


def log_step_start(step: str) -> None:
    try:
        from app.logger import logger
        logger.info("STARTUP_STEP_START: %s", step)
    except Exception:
        pass


def log_step_done(step: str) -> None:
    try:
        from app.logger import logger
        logger.info("STARTUP_STEP_DONE: %s", step)
    except Exception:
        pass


def log_step_failed(step: str, exc: Exception) -> None:
    try:
        from app.logger import logger
        # 비밀키/전체 계좌번호는 로그에 남기지 않는다 — 예외 메시지(str(exc))는 그런
        # 값을 포함할 수 있으므로 절대 로그하지 않고 예외 타입명만 남긴다.
        logger.error("STARTUP_STEP_FAILED: %s (%s)", step, type(exc).__name__)
    except Exception:
        pass
