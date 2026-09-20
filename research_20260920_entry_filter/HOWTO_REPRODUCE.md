# 재현 방법

## 전제

- production 코드는 **수정하지 않는다**. 연구는 `hengine5.py` 사본에 훅만 덧붙여 돌린다.
- 기준 커밋: `c05ccb2` (main-MACD2, N1+C1 merge + MarketData hotfix 포함).
- 연구 엔진 사본과 ctx/memo 캐시는 scratchpad `axent/` 에 있다(용량이 커서 이 번들에는
  넣지 않았다). `axww/` → `axent/` 복사 후 `scripts/ent_patch.py` 를 적용해 만든다.

## 엔진 훅 (`scripts/ent_patch.py`)

`run_chain()` 에 네 가지를 추가한다. 전부 기본 off 이며 미지정이면 완전한 no-op:

| 인자 | 뜻 |
|---|---|
| `flag_log` | 모든 플래그의 판정 + 진입시점 특징 기록 (완성봉만, 미래값 없음) |
| `solo_idx` | 그 플래그 하나만 허용하고 강제진입 → 독립 반사실 측정 |
| `force` | 거절된 플래그를 승인으로 되돌리는 후보필터 훅 |
| `force_ignore_slot` | 슬롯 한도까지 무시할지 (기본 False = **슬롯 3회/일은 지킨다**) |

기존 `gate` 훅(승인된 진입을 거절)은 제거측 후보에 그대로 썼다.
청산 경로는 한 줄도 건드리지 않았다.

## 실행 순서

```bash
cd <scratchpad>/axent
python ent_patch.py        # 훅 적용 (멱등하지 않음 — 원본 사본에 1회만)
python e0_anchor.py        # 앵커 재현: N1 401.0853 / N1+C1 438.6268 / H50 230.6116
python e1_flags.py         # 551개 플래그 원장 -> e1.pkl, flag_ledger_n1.csv
python e3_solo.py          # 551회 독립 반사실 (약 16분) -> e3.pkl, solo_flags.csv
python e4_analyze.py       # 미진입 플래그 특징 분석 -> relaxable_pool.csv
python e5_entered.py       # 진입거래 승자/패자 판별력 -> entered_trades.csv
python e6_vwap.py          # VWAP veto 임계값 스캔 (약 20분) -> e6.pkl
python e7_combo.py -1.25   # 제거측 후보 + 완화 결합 -> e7.pkl
python e8_why.py           # 변위효과 거래단위 확인
python e9_bias.py          # solo 측정 편향 보정
python e10_r1.py           # R1 임계값 민감도 (약 8분) -> e10.pkl
python e11_r1val.py        # R1 정밀검증 (LOO/부트스트랩/위약/월별)
```

`e3_solo.py` 는 40건마다 `e3.pkl` 에 저장하므로 중단 후 재실행하면 이어서 돈다.

## 주의

- `A.run("N1", ...)` 경로는 `config.QUALITY_SCORE_THRESHOLD = 3` + `Q3_BASE` 메모를 쓰고,
  `d_variant="N1PROD"` 경로는 production `quality_score_threshold()` 를 쓴다.
  둘은 동일 결과(158건 / 401.0853)임이 확인돼 있지만 **메모 네임스페이스를 섞으면 안 된다**.
- `TW2_VWAP_VETO_THRESHOLD_PCT` 를 바꿀 때는 `H._MEMO["veto"]` 를 임계값별 dict 로
  교체해야 한다(`e6_vwap.py` 의 `run()` 참고). base/tq/teg/chop 메모는 영향 없다.
- solo 측정은 그날 다른 플래그를 모두 지우므로 **반대신호 청산이 사라진다**.
  순위용 근사치로만 쓰고, 결론은 항상 결합실행으로 확인한다(`e8_why.py` 가 그 차이를 보여준다).
