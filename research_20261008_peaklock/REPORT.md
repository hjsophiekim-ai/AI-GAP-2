# PEAK-LOCK 85영업일 REAL replay (2026-10-08)

판정: **KEEP E** (PEAK-LOCK·PARTIAL+PEAK-LOCK 모두 NOT USEFUL). production/config 무수정, threshold 추가탐색 없음.

## 변형
- A 기존 E (production UPFASTRS = EARLY-PASS + BRK15 + RS125). SLOW-TP2 는 production 미반영이라 미포함.
- B PEAK-LOCK: tick peak net ≥2→floor +1, ≥3→+2, ≥4→+2.5, floor 하회 시 전량청산. 3분봉 하향래더(evaluate_position) 단계에서 기존 exit 가 없을 때만 발동 → TP/반대신호/C1 우선.
- C: net +2.5% 최초 도달 시 25% 익절(tick TP 경로, 기존 exit 없을 때만) + 잔량 B.

## 결과 (1,000만원, REAL 체결)
| 지표 | A | B | C | B−A | C−A |
|---|---|---|---|---|---|
| 총손익 | +18,435,036 | +15,809,946 | +14,853,451 | −2,625,090 | −3,581,585 |
| PF | 3.56 | 3.41 | 3.26 | −0.15 | −0.29 |
| MDD% | −6.36 | −4.97 | −4.97 | +1.40 | +1.40 |
| 최대1일손실 | −433,778 | −433,778 | −433,778 | 0 | 0 |
| 손실일 | 18 | 18 | 17 | 0 | −1 |
| 발동 | — | LOCK 26 | LOCK 26 + PART25 55 | | |

- 반납 방어: peak≥2→최종<1% 10건 B +955k / C +1,218k, 손실전환 6건 B +930k / C +1,040k. peak≥3→<2% 4건 +106k/+185k, peak≥4→<2.5% 3건 −43k/−5k.
- 큰 승자 훼손: A net≥4% 26건 B −3.23M / C −4.56M, net≥6% 7건 −1.31M / −2.13M. 0720 09:54 (A +9.47%) 는 peak 2.11 후 3분봉 종가 −0.05 로 floor 1 하회 → −0.54% 청산 (−1.31M 단일).
- 상위1~3일 제외 B−A −2.94/−3.25/−3.45M, C−A −3.94/−4.25/−4.52M.
- train 5~7월 B−A −1.81M, C−A −2.96M / test 8~10월 B−A −0.82M, C−A −0.62M. 양 구간 모두 음수.
- bootstrap P(B−A>0)=2.2%, P(C−A>0)=0.3%.

## 무결성
- 신규 A vs 기존 E 저장본 84/85일 주문 동일, 0824 는 기존 앵커 집계오류(+22,814)와 일치 → 신규 A +18,435,036 을 기준선으로.
- 비트리거 대조일(0604/0605) B=C=A, 트리거일 55일 전부 실행.

## 파일
harness/test_zz_wk22.py (tests/macd2 에 복사해 실행), job.sh/run.sh, mkbc.py (A peak≥2 일 → jobsBC), an.py, an_out.txt, daily.csv, out/REAL_{UPFASTRS,PEAKLOCK,PEAKPART}_*.json
