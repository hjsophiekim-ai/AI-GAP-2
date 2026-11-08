# 재현 — MU 선행 플래그

production 무수정 · **재생 불필요**. 저장된 1분봉/스냅샷만 쓰는 순수 계산이다.

## 전제

- 하이닉스/ETF 재생 1분봉: 10월은 `b3t_sp/data`, 11월은 `research_20261020_h50_hold/data`,
  11/06 은 `research_20261108_chop_gate/bars_fix`(시장중단 평탄봉 보충본)를 우선한다.
- MU 1분봉 + 1초 스냅샷: `arm3_sp/rt` (`ARM3_RT`).
- 공용 모듈 `wx_lib.py` / `cx_lib.py` 는
  `research_20261107_cttr_and_mu_confirm/scripts/` 의 것을 쓴다(경로는 환경변수로 조정).

## 실행

```bash
python research_20261108b_mu_lead/scripts/ml_scan.py     # -> out_mulead.csv (437건, 약 8분)
python research_20261108b_mu_lead/scripts/ml_report.py   # 깔때기 · 괴리 분위수 · 11/06 사례
```

## 주의

- **MU 09:00~10:00 공백은 정상이다**(서머타임 종료 후). 결손으로 오해하지 말 것 —
  `research_20261108_chop_gate/HOWTO_REPRODUCE.md` ① 참조.
- 성과는 `pnl = (MAE ≤ −1.6% 면 −1.6, 아니면 30분 수익률)` 로 **production 손절을 반영**한
  값이다. 순진한 30분 전방수익률만 보면 결론이 뒤집힌다(괴리 상위가 양수로 보인다).
- "하이닉스 플래그가 30분 이내 뒤따름" 은 **사후 정보**다. 진입 규칙으로 쓸 수 없다.
