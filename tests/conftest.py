"""전체 테스트 공용 격리 설정.

이 파일이 생기기 전에는 개별 테스트가 각자 알아서 data/orders, data/state,
data/logs 등을 tmp_path로 patch해야 했다. 일부 테스트가 이를 빠뜨려서 실제
운영 로그(logs/ai_gap_YYYYMMDD.log)에 테스트 실행 로그가 섞이고, 실거래 상태
파일(data/orders/*.json)이 테스트 중 덮어써지는 사고가 있었다(2026-07-09).

아래 두 fixture는 모든 테스트에 자동(autouse) 적용되어 기본적으로 실제 파일을
건드리지 않도록 막는다. 개별 테스트가 자신만의 tmp_path로 다시 patch해도
(이 fixture들보다 나중에 테스트 본문에서 실행되므로) 그 값이 그대로 우선 적용된다.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_shared_ai_gap_logger(tmp_path_factory):
    """공용 'ai_gap' 로거(app/logger.py)가 실제 logs/ai_gap_YYYYMMDD.log에 쓰지
    않도록 테스트 세션 전체에 걸쳐 격리한다.

    `app.logger.logger`는 모듈 임포트 시점에 만들어지는 프로세스 전역 싱글턴이라
    개별 테스트에서 patch하기 어렵다 — 세션 시작 시 한 번, 실제 FileHandler를
    떼어내고 임시 파일을 가리키는 핸들러로 교체한다.
    """
    import app.logger as app_logger_module

    test_log_path = tmp_path_factory.mktemp("logs") / "ai_gap_test.log"

    real_file_handlers = [h for h in app_logger_module.logger.handlers if isinstance(h, logging.FileHandler)]
    for h in real_file_handlers:
        app_logger_module.logger.removeHandler(h)
        h.close()

    fh = logging.FileHandler(test_log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    app_logger_module.logger.addHandler(fh)

    yield

    app_logger_module.logger.removeHandler(fh)
    fh.close()


# (module_path, attribute_name) — 테스트가 실수로 건드리면 안 되는 실거래/상태 파일 경로.
# 새로운 날짜별 상태/로그 파일을 추가하는 모듈이 생기면 여기에도 추가할 것.
_ISOLATED_PATH_ATTRS = [
    ("app.services.hynix_switch_state", "_STATE_DIR"),
    ("app.services.hynix_switch_logger", "_PREDICTIONS_DIR"),
    ("app.services.hynix_switch_logger", "_LOGS_DIR"),
    ("app.trading.dry_run_broker", "_DATA_DIR"),
]


@pytest.fixture(autouse=True)
def _reset_exit_order_coordinator():
    """Exit Order Coordinator는 프로세스 전역 락/쿨다운 딕셔너리를 쓰므로, 한 테스트의
    매도 시도가 다음 테스트의 30초 쿨다운에 걸리지 않도록 매 테스트마다 초기화한다."""
    from app.trading.exit_order_coordinator import reset_for_tests

    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture(autouse=True)
def _isolate_ai_gap_data_paths(tmp_path, monkeypatch):
    """data/orders, data/state, data/logs, data/predictions 등 상태/로그 파일
    경로를 기본적으로 tmp_path 하위로 격리해 실제 파일을 건드리지 않게 한다."""
    for module_name, attr in _ISOLATED_PATH_ATTRS:
        try:
            module = __import__(module_name, fromlist=[attr])
        except ImportError:
            continue
        if hasattr(module, attr):
            monkeypatch.setattr(module, attr, tmp_path, raising=False)

    try:
        import app.trading.hynix_stop_loss_control as stop_loss_module
    except ImportError:
        pass
    else:
        if hasattr(stop_loss_module, "_FORCED_LIQUIDATION_LOG_PATH"):
            monkeypatch.setattr(
                stop_loss_module, "_FORCED_LIQUIDATION_LOG_PATH",
                tmp_path / "forced_liquidation_log.csv", raising=False,
            )
