"""
price_watcher.py 테스트.

검증 항목:
  - 현재가 조회 실패 시 프로그램이 죽지 않고 안전한 dict를 반환한다.
  - KIS 실패 → 네이버 fallback.
  - 둘 다 실패 → 마지막 알려진 가격(stale=True) 또는 완전 실패 dict.
  - 연속 실패 임계값 도달 시 신규매수 차단 플래그.
"""

from unittest.mock import MagicMock, patch

from app.execution.price_watcher import fetch_price_once, PriceWatcher


def test_fetch_price_kis_success():
    kis = MagicMock()
    kis.get_current_price.return_value = {"current_price": 100000}
    result = fetch_price_once("000660", kis_client=kis)
    assert result["success"] is True
    assert result["price"] == 100000
    assert result["source"] == "kis"


def test_fetch_price_kis_fails_falls_back_to_naver():
    kis = MagicMock()
    kis.get_current_price.side_effect = Exception("network error")
    with patch("app.data.naver_stock_collector.fetch_naver_current_price") as mock_naver:
        mock_naver.return_value = {"status": "success", "current_price": 99000}
        result = fetch_price_once("000660", kis_client=kis)
    assert result["success"] is True
    assert result["price"] == 99000
    assert result["source"] == "naver"


def test_fetch_price_all_sources_fail_does_not_raise():
    kis = MagicMock()
    kis.get_current_price.side_effect = Exception("boom")
    with patch("app.data.naver_stock_collector.fetch_naver_current_price") as mock_naver:
        mock_naver.side_effect = Exception("naver down too")
        result = fetch_price_once("000660", kis_client=kis)  # 예외를 던지지 않아야 함
    assert result["success"] is False
    assert result["stale"] is True
    assert result["price"] is None


def test_fetch_price_uses_last_known_on_total_failure():
    kis = MagicMock()
    kis.get_current_price.side_effect = Exception("boom")
    last_known = {"price": 88000.0, "timestamp": "2026-01-01T09:00:00"}
    with patch("app.data.naver_stock_collector.fetch_naver_current_price") as mock_naver:
        mock_naver.side_effect = Exception("naver down too")
        result = fetch_price_once("000660", kis_client=kis, last_known=last_known)
    assert result["success"] is False
    assert result["stale"] is True
    assert result["price"] == 88000.0
    assert result["source"] == "last_known"


def test_price_watcher_tracks_consecutive_failures_and_blocks_new_entry():
    kis = MagicMock()
    kis.get_current_price.side_effect = Exception("boom")
    watcher = PriceWatcher(kis_client=kis)
    with patch("app.data.naver_stock_collector.fetch_naver_current_price") as mock_naver:
        mock_naver.side_effect = Exception("naver down too")
        for _ in range(5):
            watcher.get_price("000660")

    assert watcher.consecutive_failures == 5
    assert watcher.should_block_new_entries(threshold=5) is True


def test_price_watcher_resets_failures_on_success():
    kis = MagicMock()
    kis.get_current_price.side_effect = [Exception("boom"), {"current_price": 100000}]
    watcher = PriceWatcher(kis_client=kis)
    with patch("app.data.naver_stock_collector.fetch_naver_current_price") as mock_naver:
        mock_naver.side_effect = Exception("naver down too")
        watcher.get_price("000660")  # 실패
    assert watcher.consecutive_failures == 1

    watcher.get_price("000660")  # 성공
    assert watcher.consecutive_failures == 0
    assert watcher.is_data_fresh("000660") is True
