# AR (Afternoon Re-entry) — "대박 플래그를 놓치지 않는" 진입 로직 연구
2026-09-23 · READ-ONLY · production/config/code 무수정 · commit/push 없음

발단: 2026-09-22 12:15 DOWN_BLUE (인버스 반사실 MFE +9.91%, 15:20 +8.93%) 가
`TW2_3SLOT_REJECT_SAME_DIRECTION_AFTERNOON_2ND` 로 차단된 것.

---

## 1. 왜 거부됐나 — 관문이 **둘**이었다

### 1단계 `time_window_3slot.resolve_slot` (원장에 남는 사유)
```python
# AFTERNOON
if (afternoon_count >= 1 and is_flat
        and direction == last_afternoon_direction):
    return REJECT_SAME_DIRECTION_AFTERNOON
```
9/22: 11:18 에 오후 slot2 를 DOWN_BLUE 로 이미 사용(11:42 OPPOSITE_SIGNAL 청산) →
12:15 에 FLAT + 또 DOWN_BLUE → 차단.

### 2단계 `teg_gate.evaluate_teg` (1단계를 제거하고 재실행해서 발견)
TEG 는 7개 조건 **전부(all)** 를 요구한다. 12:15 은 **정확히 하나만** 실패했다.

| 조건 | 판정 | 값 |
|---|---|---|
| tw2_confirmed | O | gap 66.70 -> 96.15 |
| recent_cross_le_1 | O | 30분내 교차 0 |
| macd_gap_signed_net_expanding | O | 2봉 **+179.32**, 3봉 **+304.90** |
| ema_spread_signed_net_expanding | O | 2봉 +247.39, 3봉 +378.81 |
| **price_ema_stack_aligned** | **X** | close 1,917,000 / ema10 **1,917,384** / ema20 **1,917,267** |
| vwap_favorable_side | O | close < vwap 1,922,602 |
| min_9min_since_opposite_flag | O | 33분 |

하락정렬은 `close < ema10 < ema20` 인데 **ema10 이 ema20 보다 117원(0.006%) 위**였다.
MACD gap 은 오히려 가장 강하게 확대되던 중이었다.

-> **"오후 동일방향" 룰만 풀어서는 이 플래그를 못 잡는다.** (실측 확인, §3)

---

## 2. 전수 조사 — 이 룰에 막힌 후보는 78거래일에 13건뿐

창 20260529~20260922 (ctx 78일). 차단 13건 / 11일.
전체 거절사유 분포: VWAP_VETO 82 · TEG 65 · SLOT_CAP 65 · MACD_GAP 46 · NOT_CONFIRMED 44 ·
LOW_QUALITY 30 · DUPLICATE 15 · SHORT_FLAG_INTERVAL 15 · **SAME_DIRECTION_AFTERNOON 13** · 기타 13.

의사결정 시점 특징(방향 정규화, +면 방향에 유리):

| 날짜 | 시각 | 방향 | ema20-50 | vwap | 신고가/신저가 | 시가대비 |
|---|---|---|---|---|---|---|
| 20260529 | 14:03 | B | −0.046 | +0.848 | X | +1.237 |
| 20260623 | 13:27 | B | +0.941 | +3.663 | X | +7.005 |
| 20260715 | 12:33 | R | +1.086 | +1.368 | **O** | +2.878 |
| 20260723 | 14:33 | R | +0.323 | +0.626 | X | −0.156 |
| 20260805 | 12:06 | R | +0.517 | −0.072 | X | +0.000 |
| 20260805 | 13:45 | R | +0.436 | +0.972 | X | +1.135 |
| 20260806 | 14:39 | B | +0.312 | +1.740 | X | +4.924 |
| 20260813 | 14:03 | R | +0.085 | +0.354 | X | +1.570 |
| 20260814 | 14:00 | B | −0.180 | +1.036 | X | +2.610 |
| 20260916 | 14:45 | R | +0.205 | +1.038 | X | +2.830 |
| 20260921 | 14:45 | R | −0.083 | −0.008 | X | −0.479 |
| **20260922** | **12:15** | **B** | **−0.087** | +0.291 | X | +0.364 |
| 20260922 | 14:21 | B | +0.905 | +2.832 | X | +3.898 |

**주의**: 9/22 12:15 은 ema20-50 이 **음수**다. "상위추세 정렬" 류 게이트로는 절대 못 잡는다
(실제로 R1_trend/R2_extreme/R3_vwap/R5 전부 이 건을 통과시키지 못했다).

---

## 3. 실험 설계 (production 무수정, 런타임 래핑만)

`zrelax.patched` 가 `tw3.resolve_slot` 를 감싸고, **원본이
REJECT_SAME_DIRECTION_AFTERNOON 을 낼 때에만** 게이트를 본다. 그 외 분기는 원본 그대로.
통과시키면 일반 오후 경로와 동일하게 `requires_teg_gate=True` 로 돌려준다.

| 변형 | 정의 |
|---|---|
| BASE | 현행 N1+C1 |
| **AR0** | 오후 동일방향 차단만 해제 (TEG 그대로) |
| **AR1** | AR0 + **stack 면제**: TEG 탈락 조건이 `price_ema_stack_aligned` **하나뿐**이고 `macd_gap 확대` + `ema_spread 확대` + `vwap 우호` 가 전부 참이면 통과. **완화로 열린 후보에만 적용** |
| PLB | 같은 stack 면제를 **오후 TEG 후보 전체**에 적용 (blast radius 대조군) |

**새 임계값/새 상수를 하나도 만들지 않았다** — TEG 가 이미 계산하는 conditions 만 재조합했다.

### 무결성
- proj = `git archive fbc6ab4` (= origin/main-MACD2, production). a0 앵커 4종 전부 재현.
- 변형마다 `_MEMO` 전 버킷 + `Q3_BASE` 초기화. **BASE 를 맨 앞·맨 뒤 두 번 실행해 완전 일치 확인.**
- 스테일 `_q3base.pkl`(09-18 창, 580엔트리) 은 비활성화했다.

---

## 4. 결과 (78거래일 20260529~20260922)

| 변형 | 거래 | 복리% | PF | MDD | 승률 | 월% |
|---|---|---|---|---|---|---|
| BASE | 159 | 389.245 | 2.539 | **−8.906** | 52.20 | 53.34 |
| AR0 | 164 | 406.337 | 2.535 | −8.906 | 53.05 | 54.76 |
| **AR1** | **167** | **429.113** | **2.572** | **−8.906** | 52.69 | **56.60** |
| PLB | 176 | 465.853 | 2.631 | **−10.260** | 53.98 | 59.46 |
| BASE2(재실행) | 159 | 389.245 | 2.539 | −8.906 | 52.20 | 53.34 |

### 델타 vs BASE — **상위거래를 빼도 전부 양수**
| 변형 | 복리 | top5제외 | top10제외 | 최고일제외 | 상위3일제외 | MDD |
|---|---|---|---|---|---|---|
| AR0 | +17.09 | +10.96 | +7.48 | +15.50 | +10.00 | ±0 |
| **AR1** | **+39.87** | **+25.55** | **+17.45** | **+36.15** | **+27.36** | **±0** |
| PLB | +76.61 | +49.10 | +33.53 | +69.47 | +51.07 | **−1.35** |

### LOO (하루씩 제외한 78회)
| 변형 | min | median | max | 델타<=0 인 날 |
|---|---|---|---|---|
| AR0 | −2.750 | +16.689 | +24.201 | 1/78 |
| **AR1** | **+18.329** | +38.998 | +46.774 | **0/78** |
| PLB | +50.720 | +75.028 | +87.579 | 0/78 |

### 반기 분할
| 변형 | H1 (0529~0724) | H2 (0727~0922) |
|---|---|---|
| BASE | 154.540 | 92.207 |
| AR0 | 163.120 (+8.58) | 92.436 (+0.23) |
| **AR1** | **167.107 (+12.57)** | **98.090 (+5.88)** |
| PLB | 174.654 (+20.11) | 106.024 (+13.82) |

### bootstrap (일 단위 복원추출 2,000회, 복리 델타)
| 변형 | 평균 | 95%CI | P(>0) |
|---|---|---|---|
| AR0 | +6.274 | [−5.351, +17.112] | 78.3% |
| **AR1** | **+13.885** | **[−3.451, +31.169]** | **91.8%** |
| PLB | +26.742 | [−0.130, +56.354] | 97.4% |

---

## 5. AR1 이 실제로 추가한 거래 9건

| 날짜 | 진입 | 방향 | 청산 | 사유 | net |
|---|---|---|---|---|---|
| 20260529 | 14:03 | B | 15:00 | FORCED_LIQUIDATION | +1.894 |
| 20260623 | 13:27 | B | 13:59 | AFTERNOON_TP | **+4.101** |
| 20260715 | 12:33 | R | 13:27 | PROFIT_LOCK_STOP | +0.565 |
| 20260723 | 14:33 | R | 14:39 | STOP_LOSS | −1.592 |
| 20260805 | 13:45 | R | 14:30 | STOP_LOSS | −1.498 |
| 20260806 | 14:39 | B | 15:00 | FORCED_LIQUIDATION | +0.814 |
| 20260814 | 14:00 | B | 14:21 | BREAKEVEN_STOP | −0.028 |
| 20260921 | 14:45 | R | 15:00 | FORCED_LIQUIDATION | −0.257 |
| **20260922** | **12:15** | **B** | **13:19** | **AFTERNOON_TP** | **+4.026** |

밀려난 거래 1건: 20260723 14:48 (net −0.562, 즉 **손실 회피**).
net 합 **+8.025%p**, 5승 4패.
**단, 추가거래만 떼어보면 top2 제외 시 −0.102%p** — 산술적 우위는 2건(0623/0922)이 만든다.
전략 전체 복리 기준의 excl-top / LOO 가 전부 양수인 것과 함께 읽어야 한다.

---

## 6. 판정

### AR1 : **PROMISING** (production 반영은 별도 승인 필요)
근거
- LOO **78/78 전부 양수** (최악의 날을 빼도 +18.33%p)
- top5/top10 거래 제외, 최고일·상위3일 제외 **전부 양수**
- **MDD 불변** (−8.906), PF 개선 (2.539 -> 2.572)
- 양 반기 모두 양수 (H1 +12.57 / H2 +5.88)
- 새 상수 0개, 영향 범위 78일 13후보뿐 (OFF 시 BASE 와 완전 동일)
- **목표했던 9/22 12:15 을 실제로 포착** (+4.026%, AFTERNOON_TP 13:19)

한계 / 반대 근거
- bootstrap **P(>0)=91.8% < 95%**
- 추가거래 9건 중 top2 제외 시 net 합이 0 근처 -> 표본이 작다(78일 9건)
- [[project_macd2_afternoon_samedir_removal_rejected]] (2026-09-09) 는 **동일 룰의 전면 제거**를
  OOS 2승7패 −2.41%p 로 기각했었다. 이번 AR0(전면 해제)는 같은 방향의 완화인데 +17.09%p 로 반대 결과다.
  **창(0529~0922 vs 당시)과 전략 구성이 다르다.** 9/9 결과를 뒤집었다고 주장하지 말 것 —
  같은 창에서 9/9 설정을 재현해 대조하는 것이 채택 전 필수 절차다.

### PLB (stack 면제 전면 적용) : **별도 연구 필요**
수익은 가장 크지만(+76.61%p, P(>0)=97.4%) **MDD 가 −8.906 -> −10.260 로 악화**되고
진입 25건 추가 / 8건 소멸로 **행동 변화가 크다**. 다만
"`price_ema_stack_aligned` 가 오후 전반에서 과하게 엄격하다"는 **독립적인 가설**을 제시한다.
AR1 과 분리해서 전용 배터리(placebo/OOS/슬롯점유 상호작용)를 돌려야 한다.

---

## 7. 채택 전 남은 숙제
1. **9/9 설정 재현 대조** — 같은 78일 창에서 당시 실험을 다시 돌려 결과가 왜 뒤집혔는지 규명
2. **placebo** — 같은 크기의 무작위 후보군에 stack 면제를 적용했을 때도 이득이 나는지
3. **78일 BASE 절대값 불일치 미해결** — z1/z2(warm memo) 160거래/370.03 vs 완전초기화 159거래/389.24.
   20일 서브셋은 4회 연속 완전 일치(43거래/13.2791)라 엔진 자체는 결정적이다.
   **이 연구의 모든 판정은 단일 프로토콜 내부 델타로만 했다.** 절대값은 인용하지 말 것.
4. feature 브랜치에서 OFF parity diff 0 확인 후에만 merge ([[feedback_macd2_filter_branch_governance]])

---

## 8. OFF parity
래퍼(`zrelax.patched` + teg wrapper)를 설치하고 게이트를 OFF 로 둔 실행이
무패치 실행과 **거래 단위로 완전 일치(diff 0)** 했다 (20일 서브셋, 43거래 / 13.2791 / PF 1.5873).
`out/z8_parity_out.txt`.

---

## 9. AR1 을 production 에 옮긴다면 (참고용 스케치 — 적용하지 않았다)

두 파일에 **분기 하나씩**만 추가된다. 새 상수 없음. 기본 OFF 플래그로 감싸는 것을 전제로 한다.

**(1) `app/trading/macd2/time_window_3slot.py` — resolve_slot 의 AFTERNOON 분기**
```python
    # AFTERNOON
    if (afternoon_count >= 1 and is_flat and direction_obj is not None
            and last_afternoon_direction is not None
            and direction_obj.value == last_afternoon_direction):
        if not config.AR_AFTERNOON_REENTRY_ENABLED:          # 기본 False
            return SlotDecision(slot_allowed=False, ..., reject_reason=REJECT_SAME_DIRECTION_AFTERNOON)
        # AR: 슬롯만 열고, TEG 는 그대로 요구한다(면제는 TEG 안에서 별도 판정)
        return SlotDecision(slot_allowed=True, slot_number=slots_used_today + 1,
                            session=session, requires_quality_gate=False,
                            requires_teg_gate=True, ar_reentry=True)
```

**(2) `app/trading/macd2/teg_gate.py` — 마지막 approved 계산부**
```python
    approved = all(conditions.get(c, False) for c in ALL_CONDITIONS)
    if (not approved) and ar_reentry and config.AR_AFTERNOON_REENTRY_ENABLED:
        failing = [c for c in ALL_CONDITIONS if not conditions.get(c, False)]
        if (failing == [COND_EMA_STACK]
                and conditions.get(COND_MACD_GAP_EXPANDING)
                and conditions.get(COND_EMA_SPREAD_EXPANDING)
                and conditions.get(COND_VWAP)):
            approved = True
            reasons.append("AR_STACK_EXEMPT")
```

`ar_reentry` 플래그를 SlotDecision -> worker -> evaluate_teg 로 전달하는 배선이 필요하다.
**영향 범위가 이 플래그로 완전히 닫히므로 OFF 일 때 parity diff 0 은 구조적으로 보장된다.**
원장에는 `AR_STACK_EXEMPT` 를 남겨 사후 감사가 가능하게 할 것.

---

## 10. 재현
```bash
# proj = production 트리
mkdir -p /tmp/ax922/proj && git archive fbc6ab4 app scripts | tar -x -C /tmp/ax922/proj
cp config.yaml /tmp/ax922/proj/ ; cp -r config /tmp/ax922/proj/
cp -r research_20260919_n1_exit/engine/* /tmp/ax922/
mv /tmp/ax922/_q3base.pkl /tmp/ax922/_q3base.pkl.stale_disabled     # ★ 스테일 q3 memo
rm -f /tmp/ax922/_ctx_B.pkl /tmp/ax922/_ctx_922.pkl /tmp/ax922/_memo_922.pkl
mkdir -p /tmp/ax922/cache922 && cp data/cache/replay_2026*_{hynix,long,inverse}_1m.csv /tmp/ax922/cache922/
# hengine5.py 의 PROJECT_ROOT -> Path(__file__).parent/"proj"
python /tmp/ax922/a0_anchor.py     # 앵커 4종 일치 확인 (cache 는 <=0918 짜리로)
cp research_20260923b_afternoon_reentry_AR/scripts/*.py /tmp/ax922/
python /tmp/ax922/z1_collect.py    # 차단 13건 수집
python /tmp/ax922/z5_ar.py         # BASE/AR0/AR1/PLB/BASE2  (약 35분)
python /tmp/ax922/z7_robust.py     # LOO / excl-top / bootstrap
python /tmp/ax922/z8_parity.py     # OFF parity
```
주의: `zlib` 이라는 이름을 쓰지 말 것(표준 모듈과 충돌) — `zrelax.py` 로 뒀다.
