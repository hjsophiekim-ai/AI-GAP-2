"""MACD2 경보 채널 테스트 -- 웹훅으로 테스트 경보 1건을 동기 전송한다 (2026-11-20).

Render Shell:  cd /opt/render/project/src && python scripts/macd2_send_test_alert.py
- MACD2_ALERT_WEBHOOK_URL 이 없으면 전송하지 않고 미설정이라고만 출력한다.
- supervisor 원장(data/logs/macd2_supervisor_ledger.csv)에도 TEST_ALERT 1행을 남긴다.
- 주문·신호·runtime state 는 건드리지 않는다. URL 은 출력하지 않는다.
"""
import sys
from datetime import datetime

sys.path.insert(0, ".")
from app.trading.macd2 import config, supervisor  # noqa: E402

now = datetime.now(config.KST)
text = f"[MACD2 경보 테스트] {now:%Y-%m-%d %H:%M:%S} KST -- 이 메시지가 보이면 경보 채널 정상"
ok, info = supervisor.send_webhook_sync(text, {"event": "TEST_ALERT", "at": now.isoformat()})
supervisor.record_event("TEST_ALERT", level="WARNING", detail={"sent": ok, "info": info}, now=now)
print(("SENT " if ok else "NOT SENT ") + info)
sys.exit(0 if ok else 1)
