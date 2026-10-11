# 재현 절차 — FLIP-BOX BREAKOUT (2026-09-23)

READ-ONLY 연구. production 무수정. 이 번들은 커밋되지 않았다(untracked).

## 0. 전제
- Python 3.14.3 / pandas 2.3.3 (이 세션 환경)
- 원천 데이터: 저장소의 `data/cache/replay_YYYYMMDD_{hynix,long,inverse}_1m.csv`
- production 함수를 **import 해서** 쓴다 (`app.trading.macd2.signal_engine` 등). 저장소 루트에서 실행.

## 1. 플래그 + 클러스터 + ENTRY 성과

```bash
cd research_20260923_flipbox_breakout/scripts
python - <<'EOF'
import pickle, fb_build as B
ba,dra = B.build_frame(False)   # CHART 프레임 = 전시간 봉 포함 (필수, 아래 §주의 1)
bs,drs = B.build_frame(True)    # SESSION 프레임 (민감도용)
pickle.dump({"ba":ba,"bs":bs,"fa":B.flags_from(ba),"fs":B.flags_from(bs),
             "cov":B.coverage()}, open("frames.pkl","wb"))
EOF

python - <<'EOF'
import fb_run as R, pickle
days, fbd, cov = R.load_all()
res = {}
for W in (12,15,18,21):
    for M in (2,3):
        for inc in (True,False):
            res[(W,M,inc)] = R.run(W,M,inc,days,fbd)
pickle.dump(res, open("grid.pkl","wb"))
EOF
```

`frames.pkl` / `grid.pkl` / `union_breakouts.pkl` 은 `../data/` 에 완성본이 들어 있다.

## 2. EXIT 실매칭 (실제 N1+C1 진입/청산 시각 필요)

`../engine_out/n1c1_trades.csv` 가 이미 있으면 바로:

```bash
python fb_exit.py
```

없으면 엔진 사본을 먼저 복원한다 (`x1_trades.py` 가 그 스크립트다):

```bash
mkdir -p /tmp/ax/proj
git archive c40f5f5 app scripts | tar -x -C /tmp/ax/proj
cp config.yaml /tmp/ax/proj/ ; cp -r config /tmp/ax/proj/
cp -r research_20260919_n1_exit/engine/* /tmp/ax/
cp research_20260919_n1_exit/scripts/a0_anchor.py /tmp/ax/
# hengine5.py 의 PROJECT_ROOT 를 Path(__file__).parent/"proj" 로 교체
mkdir -p /tmp/ax/cache && cp data/cache/replay_2026*_{hynix,long,inverse}_1m.csv /tmp/ax/cache/
rm -f /tmp/ax/cache/replay_20260921_*.csv /tmp/ax/cache/replay_20260922_*.csv   # ★ 주의 2
rm -f /tmp/ax/_ctx_B.pkl                                                        # ctx 재빌드 강제
cp research_20260919_n1_exit/engine/_memo_B.pkl /tmp/ax/                        # ★ 주의 3
python /tmp/ax/a0_anchor.py      # 반드시 "무결성: 전부 일치" 확인
python /tmp/ax/x1_trades.py      # -> n1c1_trades.csv
```

## 주의 (다음 세션용)

1. **프레임에 프리마켓/시간외 봉을 반드시 포함**해야 KIS 차트와 맞는다. 09:00 컷하면
   2026-09-22 플래그가 6개 -> 2개로 준다. `fb_lib.load_1m(..., session_only=False)`.
2. **cache 에 0921/0922 CSV 를 넣으면** `build_ctx(78)` 창이 `20260527~20260918` ->
   `20260529~20260922` 로 밀려 공표 앵커가 전부 깨진다.
3. **`_memo_B.pkl` 은 봉 인덱스(p_idx) 키다.** 2번으로 ctx 가 한 번 바뀌면 memo 가 오염되므로
   창을 되돌린 뒤 **원본 memo 를 다시 복사**해야 한다.
   (1차 실행 불일치폭: X2lite +2.5 / H50 +12.8 / N1 -21.1 — 앵커를 안 봤으면 그대로 썼을 값)
4. `research_20260919_n1_exit/engine/*.pkl` 은 **pandas 3 피클이라 pandas 2.3.3 에서 로드 불가**
   (`NotImplementedError: datetime64[us, UTC+09:00]`).
5. `calculate_macd_series(...)` 결과에서 `ser.hist` 는 DataFrame 의 `.hist` **메서드**와 충돌한다.
   `ser["hist"]` 로 접근할 것.
6. `calculate_macd_series` 는 prefix `calculate_macd` 와 값이 같다(EMA causal) — O(n^2) 루프 불필요.

## 재현 기대값

| 항목 | 값 |
|---|---|
| 68거래일 CHART 프레임 플래그 | 492 |
| 68거래일 SESSION 프레임 플래그 | 461 |
| replay flag 재현율 (0615~0918, tol=0, 방향일치) | 447/464 = 96.3% |
| 16설정 union 클러스터 | 34 / 22일 |
| 고유 breakout 이벤트 | 25 |
| N1+C1 엔진 앵커 | 158거래 / 438.6268 / PF 2.6582 / MDD -8.906 |
| EXIT 실매칭 | 1건 (20260903, uplift +0.66%p) |
| ENTRY 제약통과(FLAT+슬롯3) | 16건, +30분 평균 +0.056% |
