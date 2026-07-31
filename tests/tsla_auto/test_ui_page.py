"""Render regression test for the TSLA_AUTO Streamlit page.

Uses streamlit.testing.v1.AppTest against the real page file. All TSLA_AUTO
state/ledger paths are isolated to tmp_path via conftest.py's autouse
fixtures — this test never touches real data/ paths, never calls real KIS,
and never starts a real background Worker (the page only ever calls
service.get_snapshot()/service.start()/service.stop(); we don't click "시작"
here, so no broker/market-data construction is attempted at all).
"""
from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

_APP_PATH = str(Path(__file__).parent.parent.parent / "app" / "ui" / "pages" / "12_TSLA_AUTO.py")


def _fresh_app() -> AppTest:
    at = AppTest.from_file(_APP_PATH, default_timeout=30)
    at.session_state["app_auth_authenticated"] = True
    return at


def test_page_renders_with_no_ledger():
    at = _fresh_app()
    at.run()
    assert not at.exception
    assert any("TSLA_AUTO" in t.value for t in at.title)


def test_page_renders_with_empty_ledger():
    from app.trading.tsla_auto import ledger

    ledger.ensure_paths()
    ledger.SIGNAL_LEDGER_PATH.write_text(",".join(ledger.SIGNAL_LEDGER_COLUMNS) + "\n", encoding="utf-8")
    ledger.EXECUTION_LEDGER_PATH.write_text(",".join(ledger.EXECUTION_LEDGER_COLUMNS) + "\n", encoding="utf-8")

    at = _fresh_app()
    at.run()
    assert not at.exception


def test_start_stop_buttons_render():
    at = _fresh_app()
    at.run()
    assert not at.exception
    labels = [b.label for b in at.button]
    assert "TSLA_AUTO 시작" in labels
    assert "TSLA_AUTO 중지" in labels
    assert "중지 및 일괄매도" in labels


def test_strong_filter_toggle_renders_and_defaults_off():
    at = _fresh_app()
    at.run()
    assert not at.exception
    checkboxes = {c.label: c.value for c in at.checkbox}
    assert "강한 플래그만 거래" in checkboxes
    assert checkboxes["강한 플래그만 거래"] is False


def test_real_mode_shows_disabled_warning():
    at = _fresh_app()
    at.run()
    at.radio(key="tsla_auto_mode").set_value("REAL").run()
    assert not at.exception
    assert any("지원하지 않는다" in e.value for e in at.error)
