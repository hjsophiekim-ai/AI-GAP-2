# MACD2 고정 ±1.5% 청산 토글 (2026-10-15)

브랜치 `feature/macd2-fixed-15-exit` — 기본 OFF. main merge/push 는 사용자 승인 후.

## 동작
- UI: 전략 선택 아래 `고정 ±1.5% 청산` 토글. state 파일에 저장 → 재시작·모드 전환·날짜 변경 후에도 유지.
- ON 이면 N1 / P3 / E / CHOP 모두 청산만 바뀐다.
  - 틱 순수익률 ≥ +1.5% → 즉시 전량익절 `FIXED_TP15`
  - 완성 3분봉 종가 순수익률 ≤ −1.5% → 전량손절 `FIXED_SL15` (진입봉·형성 중인 봉 제외)
- 꺼지는 것: 시간대 래더(TP1 부분익절/TP2/오후TP/손절/after-TP1 stop/trailing, N1 override, SLOW-TP2), ETP, P3 B3/rescue/max-hold, C1.
- 그대로: 진입·사이징, 15:00 강제청산, 반대 플래그 T+3 switch/sell-only, whipsaw-watch, H50 HOLD·해제, safety/SAFE EXIT.
- 사이징 "그날 첫 거래 손절 뒤 ×1.20" 규칙은 `FIXED_SL15` 도 손절로 본다(`position_sizing.note_full_exit`).
- 보유 중 토글을 바꾸면 그 포지션에 즉시 적용된다. ON→OFF 면 기존 전략 규칙이 다시 관리한다(예: P3 보유 20분 경과면 즉시 B3 max-hold 가능).

## 검증 (23영업일 2026-09-08 ~ 10-14, 워커 실재생 REAL 체결)
| 항목 | 결과 |
|---|---|
| OFF parity vs ebe7988 (주문·원장·신호원장) | E 110 / N1 105 / P3 121 / CHOP 85 주문, 불일치 0 |
| ON vs 연구 하네스 A안(독립 구현) | E 101 / N1 113 주문, 불일치 0 |
| ON 금지 사유 / 부분매도 | 4모드 모두 0 / 0 |
| TP 체결 순수익률 최소 | +1.584% |
| SL 체결 순수익률 | −1.33% ~ −3.17% (완성봉 판정 후 다음 시세 체결) |

23일 손익(원): E ON +226,396 vs OFF +461,653 · N1 ON +1,036,999 vs OFF +1,211,116 · P3 ON +1,036,999 vs OFF +1,908,474 · CHOP ON +221,286 vs OFF +1,037,800.
N1 과 P3 는 진입이 같고 청산만 다르므로 ON 결과가 같다.

테스트: `tests/macd2/test_fixed_exit_15.py`.
