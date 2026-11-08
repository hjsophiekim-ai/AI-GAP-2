# 재현 — 왕복장 × MU−HY 괴리 차단 (3슬롯 경쟁 포함 재생)

## 전제

- **production 코드 무수정.** 재생은 격리 워크트리 + 하네스 **사본**으로만 돈다.
- 워크트리: `wt_chop` = **현재 HEAD `d0e7bef`** (오후 손절 −1.5% · `early_stop_defer` ·
  WAIT 선택진입 · regime_e 가 전부 들어간 상태). 10/30 시점 `wt_arm3`(368e4bf)를 쓰면
  청산 규칙이 과거 값으로 되돌아간다 — 쓰면 안 된다.
- 하네스: `harness/test_zz_cg.py` — `scripts/h0_make.py` 가 ARM3 하네스
  (`research_20261030_tick_arm3/harness/test_zz_arm3.py`)에 패치 2곳만 덧붙여 만든다.
  앵커가 1개가 아니면 생성 자체가 실패한다.

```bash
export WT=C:/Users/KIMHYU~1/AppData/Local/Temp/claude/wt_chop
git worktree add --detach $WT d0e7bef
```

## 데이터

| 변수 | 기본값 | 내용 |
|---|---|---|
| `REPLAY_SP` | `.../Temp/claude/b3t_sp` | 재생 1분봉 `data/replay_<날짜>_{hynix,long,inverse}_1m.csv` |
| `ARM3_RT` | `.../Temp/claude/arm3_sp/rt` | 로거 DATA_DIR — `snapshots/<날짜>/`, `mu/<날짜>/` |
| `ARM3_OUT` | `.../Temp/claude/arm3_sp/out` | 재생 산출 json |

**11월 재생 1분봉은 이 연구에서 처음 받았다.** 기존 데이터는 10/30 까지였다:

```bash
python research_20261020_h50_hold/scripts/fetch_replay_bars.py 20261102 20261103 20261104 20261105 20261106
cp research_20261020_h50_hold/data/replay_202611*_1m.csv $REPLAY_SP/data/
```

조회 전용(KIS `inquire-time-dailychartprice`)이다. 하이닉스는 NXT 코드로 08:00 프리마켓부터
받아야 한다 — 빼면 지표 seed 가 달라진다.

로거 데이터(1초 스냅샷 + MU 분봉)는 11/03~11/06 분을 R2 `ws_shadow` export 에서 풀어
`$ARM3_RT` 아래로 합친다(10/15~11/02 는 기존 `arm3_sp/rt` 재사용).

## 하네스 생성 + 파리티

```bash
python research_20261108_chop_gate/scripts/h0_make.py     # -> harness/test_zz_cg.py
```

`r1_run.py` 의 `HARNESS` dict 에 `"CG"` 가 등록돼 있어야 한다.

```bash
R=research_20261030_tick_arm3/scripts/r1_run.py
B="--harness CG --var ARM3 --env WK_MODE=CHOP --env WK_ARM=NONE --env WK_TICKPX=0 \
   --env WK_MUA=1 --env WK_XS=1 --env MACD2_T2_SHADOW_ENABLED=0 --env MACD2_P70R_SHADOW_ENABLED=0"

python $R $B --tag PROBE_A      --env WK_CGATE=0 -j 1 20261028   # ARM3 하네스와 같아야 한다
python $R $B --tag PROBE_CGOFF  --env WK_CGATE=0 -j 1 20261028
```

`orders` · `entries` · `acts` · `sig` 가 완전히 같고 `ex` 는 **벽시계 `timestamp` 만**
달라야 한다(재생 원장 timestamp 는 벽시계다). 2026-11-08 확인 완료.

## 게이트 구현 — 슬롯을 비우는 자리에서 막는다

`worker._resolve_tw2_3slot_candidate_body` **진입부**를 감싼다(2026-10-13 OPPQ 연구와 같은 자리).

```python
st = mu_assist.entry_status_for(signal_id)
if st in (ENTRY_CONFIRM, ENTRY_WAIT):
    er  = 최근 60분 하이닉스 효율비 (now 에 끝난 1분봉만)
    gap = mu_catchup.evaluate(...)["mu_hy_gap"]
    if er is not None and gap is not None and er < WK_CG_ER and gap >= WK_CG_GAP:
        state.processed_signal_ids.append(signal_id)
        return None            # 주문 없음 · 슬롯 점유 없음
```

여기서 막으면 **슬롯이 비고, 뒤 후보가 production 규칙 그대로 그 슬롯을 쓴다.**
`_finalize_tw2_3slot_entry` 는 이미 체결된 뒤의 후처리라 거기서 막으면 늦다.

| env | 뜻 | 기본 |
|---|---|---|
| `WK_CGATE` | `0` = OFF(= ARM3 하네스와 동일) · `1` = 차단 ON | 0 |
| `WK_CG_ER` | 왕복장 판정 — 최근 60분 효율비 하한 | 0.1223 |
| `WK_CG_GAP` | 큰 괴리 판정 — `mu_hy_gap` 하한 | 0.0 |

임계값은 2026-11-07 연구에서 고정한 값 그대로다 — **탐색하지 않는다.**

## 본 재생 (15 거래일 × 2안, 순차)

```bash
bash research_20261108_chop_gate/scripts/r_run.sh
```

`-j 1` 고정이다(메모리). 하루당 5~9분, 30회면 3~4시간 걸린다.

## 집계

```bash
python research_20261108_chop_gate/scripts/s1_compare.py
```

## 표본 한계

- 재생 가능 구간은 **MU 1분봉이 있는 10/19 부터**다(`project-mu-data-starts-1015`).
  그래서 15 거래일뿐이다.
- 재생은 체결지연이 0이라 라이브보다 1분 빠르다(`project-replay-has-no-execution-lag`).
- 재생의 regime 라벨은 라이브와 갈린다(`project-replay-regime-label-differs-from-live`) —
  A·B 를 **같은 하네스**로 돌려 그 차이가 양쪽에 똑같이 들어가게 했다.

## 데이터 함정 — 2026-11-08 확인분

### ① MU 09:00~10:00 공백은 **정상이다**

미국 서머타임이 끝난 **2026-11-01 이후**, MU 1분봉은 KST **09:00~10:00 이 비어 있는 것이 정상**이다.
장외(애프터마켓) 가격까지 전부 받아도 그 한 시간은 없다. `DNASMU`(애프터마켓)가 09:00 KST 에
끝나고 `RBAQMU`(주간거래)가 10:00 KST 에 열리기 때문이다.

| 날짜 | 09~10시 분봉 | feed 분포 |
|---|---|---|
| 10/29 (서머타임) | 58분 | RBAQMU 387 / DNASMU 60 |
| 11/02~11/06 | **0~1분** | RBAQMU 330 / DNASMU 60 |

production 은 `MU_REST_FALLBACK` 이 그 사이만 REST 해외주식분봉으로
`mu/<날짜>/rest_bars_1m.csv` 에 채운다. **그런데 ws_shadow export zip 에는 그 파일이 없다** —
재생에서는 빈 채로 남는다. 수집 버그로 오해해 재수집하거나 억지로 메우지 말 것.
(옛 `fetch_rt.py` 가 `rest_1m.csv` 라는 틀린 이름을 찾던 버그도 있었지만, 고쳐도 zip 에 없다.)

재생 충실도를 따질 때는 **production 은 REST 로 메웠고 재생은 비어 있다**는 비대칭만 명시한다.

### ② 2026-11-06 09:04~09:32 은 실제 시장 중단이다

1초 스냅샷에서 000660 / 0193T0 / 0197X0 **세 종목 모두 가격·누적거래량이 전혀 변하지 않았다.**
체결이 없어 KIS 분봉 API 도 봉을 주지 않는다(`NX`·`UN` 어느 코드로 받아도 같다).
라이브 production 은 직전가를 이어받으므로 **평탄봉(OHLC=직전종가, volume=0)으로 메워야** 한다
(`scripts/f1_fill_halt.py`). 15:20~15:29 결손은 종가 단일가 구간이라 **메우지 않는다**.

### ③ production 은 전일 08:00 부터 2일 프레임을 쓴다

`macd2_bar_ledger.csv` 의 `oldest_bar_at = 전일 08:00`, `bars_in_frame = 229`.
당일 1분봉만으로 MACD 를 재구성하면 오전 플래그가 전부 어긋난다.

①②③ 를 반영하면 11/05·11/06 의 **플래그가 production 과 완전히 일치**한다:

| | production | 보정 후 |
|---|---|---|
| 11/05 | 08:45 R · 10:18 B · 10:27 R · 11:42 B · 12:33 R · 12:45 B · 13:33 R · 14:06 B · 14:09 R · 14:12 B · 14:33 R · 14:51 B | **동일** |
| 11/06 | 08:39 R · 10:24 B · 10:27 R · 11:42 B · 11:45 R · 11:57 B · 13:30 R · 14:57 B | **동일** |

MU 진입상태도 재생이 판정한 구간에서는 production 과 전부 일치했다
(11/05: 10:18 BAD · 10:27 CONFIRM · 11:42 WAIT · 12:45 CONFIRM · 13:33 BAD · 14:33 BAD).

## 이 번들을 돌리기 전에 — 외부 의존 2개

`r1_run.py` 와 ARM3 하네스는 이 번들에 없다. `research/early-stop-2bar-20261105` 브랜치에서 꺼낸다:

```bash
B=research/early-stop-2bar-20261105
for f in research_20261030_tick_arm3/scripts/r1_run.py \
         research_20261030_tick_arm3/harness/test_zz_arm3.py; do
  mkdir -p "$(dirname $f)"; git show "$B:$f" > "$f"
done
```

그리고 `r1_run.py` 의 `HARNESS` dict 에 CG 를 한 줄 등록한다:

```python
"CG": ROOT / "research_20261108_chop_gate" / "harness" / "test_zz_cg.py",
```

재생 1분봉은 `bars_fix/` 의 11/06 보정본을 `$REPLAY_SP/data/` 로 덮어써야 한다.
