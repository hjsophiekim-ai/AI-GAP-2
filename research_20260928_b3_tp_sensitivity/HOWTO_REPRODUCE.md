# 재현 절차 — B3 TP 민감도 (2026-09-28)

Python 3.14.3 / pandas 2.3.3 / numpy 2.4.4 에서 수행. 1회 실행 약 8~10분(병렬 가능, 프로세스별 독립).

## 1. 작업공간 구성

```bash
W=<작업폴더>            # 저장소 밖(스크래치패드 등)
mkdir -p $W/proj $W/cache80
git archive 948c211 app scripts | tar -x -C $W/proj
cp config.yaml $W/proj/ ; cp -r config $W/proj/
cp scripts/_tmp_20260907_exitlab.py $W/proj/scripts/        # untracked 라 archive 에 없다
cp research_20260919_n1_exit/engine/*.py $W/                 # pkl 은 복사하지 말 것(78일 ctx/memo)
cp research_20260923c_x1_80d/scripts/zrelax.py $W/
cp research_20260928_b3_tp_sensitivity/scripts/hengine5_b3.py $W/hengine5.py   # b3 훅 포함판
cp research_20260928_b3_tp_sensitivity/scripts/b3*.py $W/
cp data/cache/replay_2026*_{hynix,long,inverse}_1m.csv $W/cache80/
rm $W/cache80/replay_20260923_*     # ★ 0922 이후 파일이 있으면 창이 0528~0923 으로 밀린다
```

`hengine5_b3.py` 의 `PROJECT_ROOT` 는 `Path(__file__).parent / "proj"` 로 되어 있다.
원본 엔진 대비 변경분은 `scripts/hengine5_b3.patch` (run_chain `b3=` 인자, Trade 필드 7개,
진입 시 regime 각인, 틱 루프 B3/P3/Y3 블록 — `b3=None` 이면 원본과 동일 경로).

## 2. 실행

```bash
cd $W
python b3run.py BASE                         # 먼저. out_BASE.pkl 이 섀도우(regime 입력)가 된다
# 기대: BASE 거래 171 복리 457.9459 PF 2.5867 MDD -8.906   ← 안 맞으면 중단
python b3run.py P3_T10                       # 기대: 175 / 513.2893 / PF 2.7938   ← P3 앵커
python b3run.py T10 & python b3run.py T11 & python b3run.py T12 &
python b3run.py P3A_T11 & python b3run.py P3B_T11 &
python b3stats.py > stats.txt
```

`--le` 를 붙이면 regime 경계를 `exit_time <= as_of` 로 판정한다(결과 동일 확인용).

## 3. 산출물

| 파일 | 내용 |
|---|---|
| `out/stats1.txt` | §1~§5 (B3 단독) |
| `out/stats2.txt` | §1~§6 (P3 계열 포함) |
| `out/log_*.txt` | 각 변형 요약 1줄 |
| `data/trades_<변형>.csv` | 거래 전량. `entry_regime`/`b3_*` 는 B3 변형에만 채워짐 (BASE 의 regime 은 `b3stats.py` 가 계산) |

## 4. 엔진 갱신 (2026-09-28 오후)

`scripts/hengine5_b3.py` 는 같은 날 후속 연구(late-promotion `b3["late"]`, 프리마켓 PM1 `gate="PM1_DEFER"`)
훅이 추가된 최신판으로 교체됐다. 추가 훅은 전부 opt-in 이라 **이 연구의 변형(T10/T11/T12/P3*)은 결과가 바뀌지 않는다**
(R0 = P3_T10 = 175 / 513.2893 재확인). `b3lib.py` 는 `B3_NDAYS` 환경변수로 창 길이를 바꿀 수 있다(기본 80).
9/1~9/23 시뮬레이션은 `sim_0901_0923/README.md`.
