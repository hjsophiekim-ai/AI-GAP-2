# REGIME_E — TREND 에서 E 돌파대기 생략 (2026-11-02, research only)

재생: 운영 코드(368e4bf) 격리 워크트리 + 하네스 사본(`harness/test_zz_rege.py`, `scripts/h1_make_rege.py`)
B = `worker._e_arm_pending_instead_of_entry` 를 감싸 승인 시점 `state.p3_last_regime == "TREND"` 이면 대기 생략.
기간 08/03~10/30 실데이터 58일 (06~07월 ETF 1분봉 합성/타종목이라 제외). MU Assist/XS 는 A·B 모두 OFF(과거 MU 데이터 없음).
회귀: WK_REGIME_E=0 → 10/29 주문 6건 A 와 동일. E 대기 없던 22일은 B≡A(코드 보장)라 A 사본(f1_fill_b.py).
비교: `scripts/d3_e_regime.py compare` → `out/rege_compare.txt`

| | A | B | Δ |
|---|---|---|---|
| 거래 | 102 | 105 | +3 |
| 총손익 | +4,267,898 | +5,053,729 | **+785,832** |
| PF | 1.707 | 1.798 | +0.09 |
| MDD | 636,248 | 647,970 | **+11,722 (악화)** |
| 최악일 | −351,414 | −351,414 | 0 |
| 승률 | 48.0% | 50.5% | +2.4%p |
| 상위1/2 제외 | +3.52M / +2.89M | +4.20M / +3.46M | +0.68M / +0.56M |

CHOP 57건 A≡B 완전 동일. TREND 45→48건 Δ +785,832. 월별 Δ: 08 +223k / 09 0 / 10 +563k (10/23 한 건 +586k).
분해: E→즉시 12건 진입가 평균 1.08% 개선 +1.76M / E 만료(미진입) TREND 신호 5건을 사게 됨 −0.76M / 슬롯 연쇄 −0.22M.
채택 기준(TREND 개선 + MDD·최악일 악화 없음): **MDD +11.7k 로 미충족** (근소). 10/23 제외 시 Δ ≈ +0.2M.

## 재현
```bash
SPD=<스크래치>; export WT=$SPD/wt REPLAY_SP=$SPD/sp ARM3_RT=$SPD/rt   # WT = git worktree 368e4bf
python research_20261102_reclaim/scripts/h1_make_rege.py               # 하네스 생성
C="--harness REGE --var ARM3 --env WK_MODE=CHOP --env WK_ARM=NONE --env WK_TICKPX=0 --env WK_MUA=0 --env WK_XS=0 --env MACD2_T2_SHADOW_ENABLED=0 --env MACD2_P70R_SHADOW_ENABLED=0"
ARM3_OUT=$SPD/out      python research_20261030_tick_arm3/scripts/r1_run.py --harness ARM3 ... --tag L85_D <58일>   # A
ARM3_OUT=$SPD/out_rege python research_20261102_reclaim/scripts/r1_run.py $C --tag REGE_B --env WK_REGIME_E=1 -j 4 <E 대기 있던 33일>
python research_20261102_reclaim/scripts/f1_fill_b.py --a-out $SPD/out --b-out $SPD/out_rege    # 나머지 25일 B≡A 채움
python research_20261102_reclaim/scripts/d3_e_regime.py compare --a L85_D --b REGE_B --out $SPD/out --bout $SPD/out_rege
```
재생 1분봉: data/cache + research_20261002_sept_strategy_replay/data + research_20261020_h50_hold/data 합본, 11/02 는 Naver(`data/naver_20261102_*`, ETF 는 KIS 와 100% 일치, 하이닉스는 KRX 라 NXT 와 다름).

## 같은 폴더의 중단된 연구 (사용자 요청으로 중단, 결론 없음)
- `scripts/d1_diag.py` T+3 직후 눌림→reclaim / 확장(ATR) 진단
- `scripts/d2_preflag.py` PRE-FLAG BURST / 단기고점(hi30) / 고점만 눌림대기(H1·H2) 근사
- 정정: 11/02 1번 거래의 플래그봉은 11:09(감지 11:12), 진입은 E 돌파 11:17 @6,770 → PRE-FLAG BURST 해당.
- ETF 1분봉 06/19~07/31 은 합성·타종목 데이터 → 85일 기준선의 6~7월 부분 신뢰 불가.
