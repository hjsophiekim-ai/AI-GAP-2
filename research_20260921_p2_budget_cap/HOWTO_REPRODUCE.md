# 재현 방법

## 전제

- production 코드는 **수정하지 않는다**. 전부 scratchpad 사본에서 돈다.
- 기준 커밋: `4f181fd` (main-MACD2).
- 데이터: `data/cache/replay_*_{hynix,long,inverse}_1m.csv` 를 읽기만 한다
  (공통일자 78개 = 20260527~20260918).

## 이 연구의 핵심 — 왜 엔진을 78번 돌리지 않아도 되는가

코드로 확인한 두 사실 때문에 **사이징 변형은 기준 원장에서 해석적으로 정확히**
계산된다:

1. `app/trading/trading_cost_engine.py` — `compute_net_pnl` 의 gross/fee/tax/
   clearing/slippage 가 전부 `quantity` 에 비례하고 `min_commission_krw = 0.0` 이다.
   -> `net_pct` 는 주문수량과 **완전히 무관**하다.
2. `hengine5.py` — `w1a` / `mult` / `w1a_exposure` 가 어떤 진입·청산 판정에도
   들어가지 않는다. `w1a_seq` / `w1a_first_stop` 도 사이징 규칙 전용이다.

그래서 `scripts/k1_core.py::size_chain` 이 production `position_sizing.evaluate`
체인(CHOP 0.80 / POST_STOP 1.20 / clip 0.25~1.50 / 일노출상한 3.00)을 그대로
재현하고 그 위에 KRW 예산 계약을 얹는다.

**검증 결과(반드시 먼저 확인할 것)**

| 항목 | 기대값 |
|---|---|
| 기준 원장 w1a 158건 재현 | 불일치 0 |
| 총노출 | 157.28 |
| 78일 복리 | 438.6268 |
| 엔진 N1 앵커 | 401.0853 |
| 연구원안 P2 | +54.39 / 30d +4.11 / PF 2.789 / MDD −8.38 |

## 작업공간 만들기

```bash
W=<scratchpad>/p2krw ; mkdir -p $W ; cd $W
P="C:/Users/FURSYS/Desktop/AI-GAP 2"

cp "$P"/data/validation/macd2/safe_candidates_20260918/_engine/hengine5.py .
cp "$P"/data/validation/macd2/safe_candidates_20260918/_engine/common.py .
cp "$P"/data/validation/macd2/_research_engine_20260917/{tregime.py,adaptive.py} .
cp "$P"/research_20260920_entry_filter/scripts/{axlib.py,axval.py,ent_patch.py} .
cp "$P"/research_20260921_p2_budget_cap/scripts/*.py .

# common.py / axval.py 는 `import hengine` 를 쓴다 -> 같은 모듈로 별칭
cat > hengine.py <<'EOF'
import sys, hengine5
sys.modules[__name__] = hengine5
EOF

# axlib 이 ce.CACHE_DIR 을 HERE/"cache" 로 덮어쓰는 줄을 주석처리해서
# repo 의 data/cache 를 그대로 읽게 한다 (읽기 전용)

python ent_patch.py      # 진입기준 연구 훅 (미지정 시 no-op)
python slot_patch.py     # slot_mult 훅 + KRW 진단필드
```

## 실행 순서

```bash
python b0_ctx.py        # ctx(78) 빌드 -> _ctx_B.pkl   (약 25초)
python b1_strats.py     # §11 용 X2lite/H50/N1/N1_Safe 엔진 런 (약 10분)
                        #   -> _strats.pkl

python k2_main.py       # §0,1,2,3,9,10  본체 + ledger_*.csv / daily_P2.csv
python k3_hyp.py        # §4 가설, §7 Top-N, §8 거래의존성
python k4_over.py 1.00  # §5 과최적화 (사용자정의: 오후slot3=1.00)
python k4_over.py 1.05  # §5 과최적화 (연구원안: 오후slot3=1.05)
python k5_sens.py       # §6 민감도 격자 (MIN_MULT 0.25 / 완화 0.05 둘 다)
python k6_stat.py       # §12 부트스트랩 + §13 placebo 1,000회
python k6b_placebo.py   # §13 placebo 5,000회 + 배분/레버리지 분해
python k7_stress.py     # §14 슬리피지 / 체결지연
python k8_caps.py       # cap 발동 전량 + 예산위반 점검 + 복리
python k9_all.py        # §11 전 전략 KRW 비교
python k10_decomp.py    # 배분 vs 레버리지 완전 분해
python k11_verdict.py   # §15 채택조건 20개 판정
```

출력 전량은 `data/*.txt` 에 있다.

## 반드시 지킬 것

1. **P2 정의를 먼저 고정하라.** 연구 원안 람다
   `MIN if (s==3 and ss==MORNING) else 1.05` 는 **오후 slot3 에도 1.05 를 준다.**
   "오후 slot3 기존 배수" 라는 서술과 다르다. 이 번들의 1차 정의는
   `make_extra(front=1.05, morning_slot3=0.25, afternoon_slot3=1.00)` 이다.
   `k4_over.py` 는 argv 로 둘 다 낸다.
2. **`_w1a_multiplier` 의 `extra` 는 clip 전에 들어간다**(pre_clip).
   명세 순서(post_clip)와 비교는 `size_chain(order=...)` 로 낸다 — 차이는 +1,500원.
3. **MIN_MULT 를 건드린 격자는 production 이 아니다.** 0.00/0.10 셀은 production
   에서 전부 0.25 로 클립된다. 참고용으로만 읽을 것.
4. `k1_core.MIN_MULT` 를 임시로 바꾸는 스크립트(`k5_sens`, `k10_decomp`)는
   끝에서 반드시 0.25 로 되돌린다.

## 알려진 재현 불가

- **X2-lite W1 / H50 공표앵커** (H50 230.6116). CHOP TEG 재게이트 훅이
  `run_chain` 에 미내장이고 그 패치가 저장소에 없다 — 이전 세션 scratchpad 에만
  있었다. `watch_seed_fix` 를 True/False 로 바꿔도 206.94 / 208.75 로 앵커에
  닿지 않는다. N1 / N1+C1 은 정확히 재현된다.
- **N1/C1 엔진 훅**(`H.N1_PROD`, `ax=`) 도 저장소에 없다. 그래서 N1+C1 기준은
  엔진 재실행이 아니라 번들된 원장
  `data/validation/macd2/n1_production_20260920/trades_N1_plus_C1.csv` 를 쓴다
  (앵커 438.6268 로 검증됨).
