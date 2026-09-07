# ⚠ 이 디렉터리의 수익률 수치는 무효 (2026-09-07)

이 폴더의 모든 백테스트는 **낙관적 체결모델**(확정봉 자기 종가 `etf_close[bar_ts]`
로 체결, 불완전 3분봉 미제거) 위에서 계산됐다. 이는
`scripts/tw_gate_corrected_clock_engine.py` (2026-08-20) 와 memory
`project_macd2_backtest_clock_semantics_fix` 가 "FILL-PRICE LOOK-AHEAD" 로 지목한
바로 그 모델이다.

faithful-fill 로 다시 계산하면 현행 A 의 최근 30영업일 복리가
**+59.37% -> +19.82%** (-39.56%p) 로 내려간다.

**대체 산출물: `data/validation/faithful_20260907/`** — 그쪽 수치를 쓸 것.

여기서 아직 유효한 것: 파라미터 축의 *상대적* 방향성 탐색 기록과 진입게이트
연구의 구조적 결론(하루 4번째 슬롯은 손해 등). 절대 수익률/PF/MDD 는 전부 무효다.
