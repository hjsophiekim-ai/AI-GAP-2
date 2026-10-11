# 재현 절차

## 0. 전제

- Python 3.12 / pandas 3.0.3 / numpy 2.4.6 에서 수행했다.
- 엔진은 production 을 **import 해서** 호출한다. 그래서 `proj/` (HEAD `c40f5f5` export)
  가 필요하다. 용량 때문에 이 폴더에는 담지 않았다.

## 1. proj/ 복원

```bash
cd <repo>
mkdir -p /tmp/ax/proj
git archive c40f5f5 app scripts | tar -x -C /tmp/ax/proj
cp config.yaml /tmp/ax/proj/            # 없으면 수수료가 기본값으로 떨어진다
cp -r config     /tmp/ax/proj/
cp scripts/_tmp_20260907_exitlab.py /tmp/ax/proj/scripts/   # untracked 라 archive 에 없다
```

`hengine5.py` 의 `PROJECT_ROOT` 는 `Path(__file__).parent / "proj"` 이므로
`engine/*` 와 `proj/` 를 같은 폴더에 두면 된다.

## 2. cache/ 복원

`axlib.py` 가 `ce.CACHE_DIR = HERE/"cache"` 로 잡는다. 09-18 연구본
(`.../scratchpad/n1w/cache`)의 replay 1분봉 CSV 를 그대로 쓰면 된다.
**ctx 는 `engine/_ctx_B.pkl` 에 이미 들어 있어서, 창을 바꾸지 않으면 cache 없이도 돌아간다.**

## 3. 무결성 확인 (반드시 먼저)

```bash
python scripts/a0_anchor.py        # W70 앵커 4종
```

기대값 — 하나라도 어긋나면 이후 수치는 신뢰 불가:

```
X2-lite W1  144거래  176.3247
H50         141거래  204.1759
N1          142거래  396.6787
N1-Safe     142거래  384.4773
78일 N1     158거래  401.0853
```

⚠ **ctx 를 재빌드할 때는** `Quotes` 의 `as_unit("ns")` 패치가 반드시 있어야 한다
(pandas 3 에서 `asi8` 가 us 일 수 있어 체결가가 조용히 상수가 된다).
자세한 내용은 `ENGINE_README.md` §2.

⚠ `_MEMO` 는 **봉 인덱스(p_idx)** 를 키로 쓴다. ctx 시작일/봉 구성이 바뀌면 캐시된
판정이 조용히 틀린 값으로 재사용된다. 다른 ctx 로 돌릴 땐 memo 를 지울 것.

## 4. 라운드별 재실행

| 라운드 | 순서 |
|---|---|
| 1 | `a2_dataset.py` → `a3_features.py` → `a4_screen.py` → `a5_variants.py` → `a6_family_d.py` → `a10_verify_fast.py` → `a11_grid2.py` → `a8_e_placebo.py` → `a12_e35.py` → `a13_battery.py` → `a14_final.py` → `a15_trailcurve.py` |
| 2 | `p1_run.py` → `p2_report.py` → `p3_extra.py` |
| 3 | `s1_save.py` → `s2_diag.py` → `s3_newcand.py` → `s4_combo.py` → `s5_robust.py` → `s6_isolate.py` → `s7_final.py` → `s8_report.py` |

각 스크립트는 78일 N1 을 1회 이상 돌린다(warm memo 기준 약 30~50초/회).
`s5`/`s6`/`s7` 은 10~15분 걸린다.

## 5. 채택후보 C1 재현

```python
ax = {"decide": 99.0, "strong": {}, "weak": {},
      "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}
ts = A.run("N1", ctx, ctx.dates, cfg=cfg, po=po, q3=True, h50=True, ax=ax)
# 기대: 158거래 / 78일 438.63 / 30일 66.48 / PF 2.658 / MDD -8.906
```

`decide: 99.0` 은 "E 성분(레벨 확정) 사용하지 않음"을 뜻한다.
