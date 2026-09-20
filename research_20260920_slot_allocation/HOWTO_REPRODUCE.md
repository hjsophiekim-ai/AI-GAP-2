# 재현 방법

## 전제

- production 코드는 **수정하지 않는다**. 연구는 `hengine5.py` 사본에 훅만 덧붙여 돌린다.
- 기준 커밋: `c05ccb2` (main-MACD2, N1+C1 + MarketData hotfix 포함).
- 이 연구는 `research_20260920_entry_filter/` 의 엔진 사본(`axent/`)을 그대로 복사해
  (`axslot/`) 시작한다. 즉 `scripts/ent_patch.py` 가 이미 적용된 상태가 전제다.
- ctx/memo 캐시(수십 MB)는 번들에 넣지 않았다 — `e1.pkl`/`e3.pkl` 은 entry_filter
  번들의 `results/` 에 있다(S1 진단에서만 쓴다).

## 추가 훅 (S6 에서 1줄 패치)

`slot_mult` 를 **callable 로도** 받게 했다. 딕셔너리면 기존대로 `{slot: 배수}`,
callable 이면 `slot_mult(slot_number, session, date)` 로 호출한다:

```python
_ex = float(slot_mult(slot_number, session, current_day)
            if callable(slot_mult)
            else slot_mult.get(slot_number, 1.0))
```

이 값은 production `position_sizing` 과 같은 식(`_w1a_multiplier`)의 `extra` 로 들어가고
MIN 0.25 / MAX 1.5 / 일노출상한 3.0 클리핑을 그대로 받는다. 청산은 건드리지 않는다.

## 실행 순서

```bash
cd <scratchpad>/axslot           # axent 사본
python s1_diag.py     # 3회 한도 진단 + 오라클 천장
python s2_alloc.py    # 선택(A)·크기(B) 후보 1차 스캔 (약 7분)
python s3_front.py    # 앞쪽 가중 사다리 — 총노출이 같이 오르는 것을 확인 (약 8분)
python s4_matched.py  # 레버리지 곡선 + 노출 일치 비교 (약 7분)  -> s4.pkl (필수)
python s5_grid.py     # 노출 일치 정밀격자 (약 9분)
python s6_mech.py     # 정체 규명: slot3 인가 오전3번째인가 (약 9분)
python s7_val.py      # 삭감 깊이 민감도 + 부트스트랩 (약 9분)
python s8_loo.py      # LOO 8건 + 월 제외 + '슬롯 남기기' 대안 (약 9분)
python s9_prod.py     # 실운용 형태(P0~P3), production 상한 그대로 (약 5분)
```

`s4.pkl` 이 레버리지 곡선을 담고 있어 s5~s8 이 전부 이걸 읽는다. 먼저 돌릴 것.

## 핵심 원칙 — 반드시 지킬 것

1. **노출을 맞추지 않은 비교는 무의미하다.** 앞 슬롯 배수를 올리면 총노출이 함께
   오르고, 그 이득의 대부분은 레버리지다. 반드시 스칼라 k 로 실현 총노출을
   기준(157.28)에 맞춘 뒤, **같은 노출의 무배분 레버리지**와 비교한다.
2. 노출을 맞추는 비교에서는 일노출상한(3.0)·개별상한(1.5)을 **풀어야 한다**
   (`cap=99`, `MAX_MULT=9`). 상한이 모양마다 다르게 물리면 그 자체가 교란이다.
3. 모든 후보에서 `V.report(...)["only_a"] == only_b == 0` 을 assert 한다 —
   사이징만 바꾸므로 진입집합이 바뀌면 버그다.
4. 실운용 형태(S9)는 반대로 **production 상한을 그대로 둔 채** 기준과 직접 비교한다.
