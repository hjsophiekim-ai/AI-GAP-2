# 재현 방법

## 전제

- production 코드는 **수정하지 않는다**. 전부 scratchpad 사본에서 돈다.
- 기준 커밋: `b75a197` (main-MACD2).
- 데이터: `data/cache/replay_*_{hynix,long,inverse}_1m.csv` 읽기 전용.
  공통일자 78개(20260527~20260918), 이 연구는 그중 **마지막 30일**을 쓴다.

## 왜 엔진을 다시 돌리지 않는가

사이징 변형은 기준 원장에서 해석적으로 정확히 계산된다 — 근거는
`research_20260921_p2_budget_cap/HOWTO_REPRODUCE.md` 와 동일하다
(수수료가 quantity 비례 + `min_commission_krw=0` → net_pct 가 수량 무관,
`w1a` 가 진입·청산 판정에 미개입).

**청산 변형(TP15)은 다르다.** 청산가가 바뀌므로 보유 중 1분봉 경로가 필요하다.
`t1_path.py` 가 `data/cache` 의 1분 OHLCV 로 진입 다음 봉 ~ 청산 봉까지를
재구성하고, 도달 판정은 **intrabar high**, 체결은 **도달봉 종가**(보수)로 둔다.
경로 재현은 원장 `peak_net_pct` 와 대조 검증돼 있다(평균 오차 −0.048%p).

## 작업공간

```bash
W=<scratchpad>/p2krw ; mkdir -p $W ; cd $W
P="C:/Users/FURSYS/Desktop/AI-GAP 2"

# 연구 엔진 + 훅 (P2 번들과 동일한 셋업)
cp "$P"/data/validation/macd2/safe_candidates_20260918/_engine/{hengine5.py,common.py} .
cp "$P"/data/validation/macd2/_research_engine_20260917/{tregime.py,adaptive.py} .
cp "$P"/research_20260920_entry_filter/scripts/{axlib.py,axval.py,ent_patch.py} .
cp "$P"/research_20260921b_weak_tp15_and_late_blue/scripts/*.py .
cat > hengine.py <<'EOF'
import sys, hengine5
sys.modules[__name__] = hengine5
EOF
# axlib 의 ce.CACHE_DIR 덮어쓰기 줄을 주석처리 (repo data/cache 를 그대로 읽는다)
python ent_patch.py
python slot_patch.py
python b0_ctx.py            # ctx(78) 빌드 -> _ctx_B.pkl  (약 25초)
```

## 실행

```bash
python w0_base.py    # 30일 BASE 앵커
python w1_weak.py    # 연구 1 — 약한 장 +1.5% 조기익절
python w2_blue.py    # 연구 2 — 13:30 이후 BLUE
```

출력 전량은 `data/*.txt`.

## 반드시 지킬 것

1. **30일 BASE 를 먼저 맞출 것** — 69거래 / 5,303,701 KRW / PF 2.439 /
   MDD −1.70% / 사용률 75.9%. 안 맞으면 그 뒤 숫자는 전부 의미 없다.
2. **약한 장 판정은 진입 시각 이전 정보만** 쓴다. `t2_regime` 의 `_bars_before`
   가 "봉 시작 + 3분 <= 진입시각" 으로 완성봉만 자른다. 이 경계를 풀면
   미래정보가 샌다.
3. **W2(첫 거래 MFE) 는 첫 거래 자신에게 적용하지 않는다.** 한 번에 한
   포지션이라 첫 거래는 두 번째 진입 전에 이미 닫혀 있다 — 그래서 합법이다.
   같은 거래에 자기 MFE 를 쓰면 즉시 미래정보다.
4. **배수 비교에서 uplift 가 배수와 무관하게 같으면 버그가 아니라 예산 cap 이다.**
   연구 2 에서 1.10/1.20/1.30 이 전부 +12,796 인 이유다 — 확인하려면
   `K.budget_stats(...)['cap_hits']` 와 영향일수를 같이 볼 것.
5. **지수/breadth 는 쓸 수 없다.** `data/cache/market_regime_last_kospi.json` 은
   2026-07-10 스냅샷 1건이고 advancers/decliners 가 `None` 이다. 78일 intraday
   이력이 없다. 지수 기반 regime 을 만들려면 데이터 수집부터 해야 한다.

## parked_etp 재현

```bash
python e1_scope.py    # ETP 발동 규모 (78일 6건)
python e2_probe.py    # N1 단독 vs N1+C1 의 ETP 거래집합 동일성
python e4_path.py     # 발동 이후 경로 (t=0 정렬)
python e3c_rerun.py   # 엔진 변형 A~F (floor/scope)  약 10분
python e3b_run.py     # 엔진 변형 G~K (delay/trigger) 약 5분
python e5_cmp.py      # 변형 KRW 비교
```
`e3b_run.py` 의 delay 노브는 `hengine5.ETP_DELAY_BARS` 모듈 전역을 쓴다
(`ExitParams` 필드가 아니다 — 처음에 필드로 넣었다가 `dataclasses.replace`
에서 터졌다).
