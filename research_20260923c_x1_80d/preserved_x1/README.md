# 보존본 — X1 CONTEXT 판정 모듈 (연구 전용, 2026-09-24)

`app/trading/macd2/` 에서 **production path 를 비우면서** 이리로 옮겼다.
production 은 더 이상 이 두 파일을 import 하지 않는다.

| 파일 | 원래 위치 |
|---|---|
| `x1_context.py` | `app/trading/macd2/x1_context.py` |
| `x1_shadow.py`  | `app/trading/macd2/x1_shadow.py` |

## 왜 옮겼나

2026-09-23 두 차례 80영업일 검증 결과:

| 모듈 | 80일 결과 | 판정 |
|---|---|---|
| X1-3 AR1 | +35.03%p · LOO 80/80 양수 · MDD 불변 | **채택 — production 에 내장** |
| X1-2 FLIP EXIT | −0.92%p · LOO 76/80 음수 · 이익거래 조기절단 | REJECT |
| X1-1 MORNING WATCH | BLOCK −120.06 / SIZE CUT −47.33 / DELAY −189.22 · LOO 80/80 음수 | REJECT |
| X1-4 LATE ENTRY | 실엔진 진입 8건, 이익이 사실상 1건(20260611)에 의존 · OOS −2.78 | REJECT |
| X1-5 오후 BLUE 가점 | 진입 변화 0건 | REJECT (무효) |

채택된 AR1 은 이 파일이 아니라
`app/trading/macd2/time_window_3slot.evaluate_afternoon_reentry` 에 들어 있다
(SAME_DIRECTION_AFTERNOON 거절을 되돌리는 조건부 예외 — 토글 없음).
조건/순서는 여기 `x1_context.evaluate_afternoon_reentry` 와 동일하다.

## 연구에서 다시 쓰려면

```python
import sys; sys.path.insert(0, "research_20260923c_x1_80d/preserved_x1")
import x1_context   # app.trading.macd2 네임스페이스가 아니다
```
`x1_shadow.py` 는 `app.trading.macd2.config.X1_SHADOW_LEDGER_FILENAME` 등
**삭제된 config 상수**를 참조하므로 그대로는 import 되지 않는다(기록 보존용).
