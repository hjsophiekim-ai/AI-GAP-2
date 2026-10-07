# MACD2 N1 청산 개선 연구 — 2026-09-18 ~ 09-19

**READ-ONLY 연구 산출물.** production 코드·config 무수정, commit/push 없음.
이 폴더 자체도 git 미추적(untracked)이며 커밋 대상이 아니다.

## 먼저 읽을 것

1. **[MASTER_RESULTS.md](MASTER_RESULTS.md)** ← 3라운드 종합. 결론·채택후보·강건성·약점.
2. [README_ADAPTIVE_EXIT.md](README_ADAPTIVE_EXIT.md) — 라운드1: position-level adaptive exit (98후보, 전부 기각)
3. [README_PEAK_PROTECTION.md](README_PEAK_PROTECTION.md) — 라운드2: peak-relative profit protection (22후보, 전부 기각)
4. [HOWTO_REPRODUCE.md](HOWTO_REPRODUCE.md) — 재현 절차
5. [ENGINE_README.md](ENGINE_README.md) / [WINDOW_COMPARE_README.md](WINDOW_COMPARE_README.md) — 09-18 선행연구(엔진 보존본·4전략 창별 비교)

## 한 줄 결론

164개 후보 중 **`N1 + PP5.0` 단 하나**가 채택조건 14개를 전부 통과했다.
MFE ≥ +5.0% 도달 후 완성봉에서 보유방향 MACD gap ≤ 0 이고 MFE 대비 1.5%p 반납 시 전량청산.
78일 401.09 → 438.63, 30일 65.12 → 66.48, PF·MDD·−Top10 비악화, 변경 7건 전부 개선,
진입집합 무변경, TP2 8% runner 손상 0. 약점은 MASTER_RESULTS.md §약점 참조.

## 구성

| 경로 | 내용 |
|---|---|
| `results/all_candidates_78d.csv` | 라운드1·2 후보 122개 × 전 지표 |
| `results/round3_candidates.csv` | 라운드3 후보 83개 × 전 지표 |
| `results/trades_*.csv` | 기준·후보별 거래 전량 (w1a 반영 pnl 포함) |
| `results/ms78_features.csv` | +3/+4/+5% 도달시점 인과 특징표 120건 |
| `results/FINAL_TRADES.pkl` / `ALL_TRADES.pkl` | 전 후보 거래 원본 |
| `engine/` | 연구엔진 + ctx/memo 캐시 (`hengine5.py.orig` = 훅 추가 전 09-18 판) |
| `scripts/` | 실행·검증 스크립트 전량 (a=라운드1, p=라운드2, s=라운드3) |
| `logs/` | 각 실행의 원시 stdout |

`engine/proj/`(HEAD c40f5f5 export)와 `cache/`(분봉 replay 캐시)는 용량 때문에 제외했다.
재현 방법은 HOWTO_REPRODUCE.md 참조.
