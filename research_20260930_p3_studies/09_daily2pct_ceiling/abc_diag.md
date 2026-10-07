# 9월 '매일 최소 +2%' 가능성 — A/B/C 진단 (2026-09-30, READ-ONLY)

FILTER = R0 거래 중 수익 거래 최적 선택 / FULL-FLAG = 게이트 해제 전 플래그 후보(한도3·한도해제 두 풀 ∪ R0) 중 시간 비중복 수익 거래 최대 3건 · 모두 수량 1.0, 기존 청산 규칙

| 날짜 | R0 | FILTER | FULL-FLAG | 후보 수 | 분류 | +2% 가능 |
|---|---|---|---|---|---|---|
| 0903 | -2.82 | +0.00 | +0.00 | 3 | C | X |
| 0904 | -1.74 | +0.00 | +4.46 | 9 | A+B | O |
| 0907 | +2.51 | +2.51 | +2.51 | 6 | - | O |
| 0908 | +2.88 | +3.14 | +7.72 | 5 | - | O |
| 0909 | +4.95 | +4.95 | +7.17 | 4 | - | O |
| 0910 | +0.33 | +0.33 | +3.64 | 5 | A+B | O |
| 0911 | +2.47 | +2.47 | +2.47 | 6 | - | O |
| 0914 | -0.04 | +0.00 | +0.60 | 7 | C | X |
| 0915 | -0.10 | +1.19 | +3.04 | 10 | A+B | O |
| 0916 | +3.66 | +3.66 | +3.66 | 7 | - | O |
| 0917 | +1.43 | +2.02 | +2.02 | 6 | A | O |
| 0918 | -0.53 | +0.00 | +2.07 | 4 | A+B | O |
| 0921 | +1.30 | +1.35 | +1.97 | 10 | C | X |
| 0922 | +3.28 | +4.03 | +6.50 | 8 | - | O |
| 0923 | -1.30 | +1.01 | +1.36 | 9 | C | X |
| 0928 | +2.16 | +3.26 | +3.26 | 5 | - | O |
| 0929 | +1.27 | +1.28 | +2.94 | 9 | A+B | O |

- 9월 영업일 17 · R0 ≥2% 7 · FILTER ≥2% 8 · FULL-FLAG ≥2% 13
- A형(기회 있었는데 못 먹음) 6일 ['0904', '0910', '0915', '0917', '0918', '0929']
  - 그중 B형(필터론 불가, 게이트 해제 시 가능) 5일 ['0904', '0910', '0915', '0918', '0929']
  - 그중 필터만으로 가능(FILTER ≥2 ∧ R0 <2) 1일 ['0917']
- C형(현재 구조로 불가) 4일 ['0903', '0914', '0921', '0923']

## A형 날짜별 최적 선택 거래와 R0 가 놓친 이유
### 0904 (A+B) R0 -1.74 → FULL +4.46
R0: 10:03 DOWN GX_MAXHOLD -0.56 / 11:18 UP_R GX_MAXHOLD -0.57 / 11:42 DOWN OPPOSITE_SIGNAL -0.76
- 선택 12:45 UP_R → 13:48 SMALL_WHIPSAW_HOLD_EXI +1.98 (MFE +2.55) · TW:REJECT_NOT_CONFIRMED
- 선택 14:03 UP_R → 14:12 GX_TP +1.15 (MFE +1.15) · TW:REJECT_NOT_CONFIRMED
- 선택 14:36 DOWN → 14:48 GX_TP +1.32 (MFE +1.32) · VETO:TW2_REJECT_VWAP_VETO
### 0910 (A+B) R0 +0.33 → FULL +3.64
R0: 14:27 UP_R GX_MAXHOLD +0.33
- 선택 10:39 UP_R → 13:18 SMALL_WHIPSAW_HOLD_EXI +2.22 (MFE +3.85) · TW:REJECT_LOW_QUALITY_SCORE
- 선택 13:21 UP_R → 13:39 GX_TP +1.09 (MFE +1.09) · TEG:price_ema_stack_aligned,vwap_favorable_side
- 선택 14:27 UP_R → 14:47 GX_MAXHOLD +0.33 (MFE +0.91) · R0 도 진입
### 0915 (A+B) R0 -0.10 → FULL +3.04
R0: 09:27 UP_R GX_TP +1.19 / 09:54 DOWN GX_MAXHOLD -0.09 / 10:27 UP_R GX_SL -1.22
- 선택 09:27 UP_R → 09:42 GX_TP +1.19 (MFE +1.19) · R0 도 진입
- 선택 11:30 UP_R → 11:50 GX_MAXHOLD +0.74 (MFE +0.79) · SLOT:TW2_3SLOT_REJECT_DAILY_SLOT_CAP
- 선택 14:00 DOWN → 14:12 GX_TP +1.11 (MFE +1.11) · SLOT:TW2_3SLOT_REJECT_DAILY_SLOT_CAP
### 0917 (A) R0 +1.43 → FULL +2.02
R0: 09:12 DOWN OPPOSITE_SIGNAL -0.41 / 10:00 UP_R GX_TP +1.14 / 10:48 DOWN GX_MAXHOLD +0.88
- 선택 10:00 UP_R → 10:07 GX_TP +1.14 (MFE +1.14) · R0 도 진입
- 선택 10:48 DOWN → 11:08 GX_MAXHOLD +0.88 (MFE +0.88) · R0 도 진입
### 0918 (A+B) R0 -0.53 → FULL +2.07
R0: 09:06 DOWN WHIPSAW_WATCH_DETE -0.53
- 선택 11:51 UP_R → 12:54 SMALL_WHIPSAW_HOLD_EXI +2.07 (MFE +2.19) · TW:REJECT_NOT_CONFIRMED
### 0929 (A+B) R0 +1.27 → FULL +2.94
R0: 09:54 DOWN GX_MAXHOLD +0.24 / 10:36 UP_R GX_TP +1.03 / 12:15 DOWN GX_MAXHOLD -0.01
- 선택 09:54 DOWN → 10:14 GX_MAXHOLD +0.24 (MFE +0.49) · R0 도 진입
- 선택 10:36 UP_R → 10:52 GX_TP +1.03 (MFE +1.03) · R0 도 진입
- 선택 11:33 DOWN → 13:06 SMALL_WHIPSAW_HOLD_EXI +1.67 (MFE +1.75) · VETO:TW2_REJECT_RECENT_CROSSES

## 요약: R0 가 놓친 기회 거래의 원인 (A형 전체)
- TW: 4건
- VETO: 2건
- SLOT: 2건
- TEG: 1건

## B형에서 기회를 막은 게이트 (세부)
- TW:REJECT_NOT_CONFIRMED: 3건
- SLOT:TW2_3SLOT_REJECT_DAILY_SLOT_CAP: 2건
- VETO:TW2_REJECT_VWAP_VETO: 1건
- TW:REJECT_LOW_QUALITY_SCORE: 1건
- TEG:price_ema_stack_aligned,vwap_favorable_side: 1건
- VETO:TW2_REJECT_RECENT_CROSSES: 1건

## C형 날짜의 후보 거래 (최대 net)
- 0903 R0 -2.82 · FULL +0.00 · 후보 3건
- 0914 R0 -0.04 · FULL +0.60 · 후보 7건
- 0921 R0 +1.30 · FULL +1.97 · 후보 10건
- 0923 R0 -1.30 · FULL +1.36 · 후보 9건
