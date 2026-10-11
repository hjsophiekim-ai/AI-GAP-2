# 연구용 엔진 사본 보존 (2026-09-24)

`research_20260919_n1_exit/engine/` 의 보존 엔진에 **연구 전용 오버레이 3종**을 얹은 판이다.
production 은 이 파일을 import 하지 않는다 — scratchpad 연구 전용이다.

## 왜 파일째 보존하는가

`cx`(CHOP MODE) 와 `gx`(CHOP RESPONSE) 는 `patch_cx.py` / `patch_gx.py` 로 재생성할 수 있지만,
**`rx`(regime 방어) 오버레이는 패치 스크립트가 남아 있지 않다**(당시 인라인으로 적용했다).
따라서 패치 스크립트만으로는 최종 엔진을 복원할 수 없어 파일 자체를 보존한다.

## 계보

| 파일 | 내용 | 앵커 재현 |
|---|---|---|
| `hengine5.py.ANCHOROK` | 원본(= 0919 보존본 + PROJECT_ROOT 경로만 수정) | 확인됨 |
| `hengine5.py.BEFORE_CX` | + **rx** 오버레이 (regime 방어: lock/hold/size/partial) | 확인됨 |
| `hengine5.py.BEFORE_GX` | + **cx** 오버레이 (CHOP MODE 신규 진입전략 C1/C2/C3) | 확인됨 |
| `hengine5.py` | + **gx** 오버레이 (CHOP RESPONSE: skip 규칙 + 축B 청산) | **확인됨** |

원본 대비 총 변경 382줄. 네 단계 모두 **해당 파라미터가 None 이면 앵커 4종이 비트 단위로 재현**됨을
매 단계 확인했다(W70: X2lite_W1 144/176.3247 · H50 141/204.1759 · N1 142/396.6787 · N1_Safe 142/384.4773).

## 동반 파일

| 파일 | 용도 |
|---|---|
| `rlib.py` | BASE 셋업 (N1 + C1 ax + AR1 teg_wrapper + zrelax), memo 초기화 프로토콜 |
| `zrelax.py` | AR1 용 `resolve_slot` 런타임 래핑 (0923 번들 사본) |
| `cxlib.py` | CHOP MODE 전략 3종의 인과적 특징표와 진입판정 |

## 사용법

```bash
mkdir -p lab && cd lab
cp <이 폴더>/*.py .
cp ../research_20260919_n1_exit/engine/{_ctx_B.pkl,_memo_B.pkl,axlib.py,common.py,hengine.py,adaptive.py,axdefs.py,axval.py,tregime.py} .
mv _q3base.pkl _q3base.pkl.DISABLED 2>/dev/null || true   # q3 base 캐시 비활성화
mkdir -p proj && git archive c40f5f5 app scripts | tar -x -C proj
cp ../config.yaml proj/ && cp -r ../config proj/
cp ../scripts/_tmp_20260907_exitlab.py proj/scripts/      # untracked 라 archive 에 없다
python a0_anchor.py        # 반드시 먼저 — 앵커 4종이 어긋나면 이후 수치는 신뢰 불가
```

`hengine5.py` 의 `PROJECT_ROOT` 는 `Path(__file__).parent / "proj"` 로 되어 있다.

⚠ ctx 를 **재빌드하면** `Quotes` 의 `as_unit("ns")` 패치가 반드시 있어야 한다
(pandas 3 에서 `asi8` 가 us 일 수 있어 체결가가 조용히 상수가 된다). 이 사본에는 이미 들어 있다.

⚠ `_MEMO` 는 봉 인덱스를 키로 쓴다. 다른 ctx 로 돌릴 땐 memo 를 지울 것.
