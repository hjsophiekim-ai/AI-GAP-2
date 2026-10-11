"""axlib — position-level adaptive exit 연구 공용 셋업. READ-ONLY.

production(proj/ = HEAD c40f5f5 export) 무수정. ctx/memo 는 09-18 연구본을
그대로 재사용하고, 엔진은 milestone 관측 훅만 추가된 사본이다(앵커 재현 확인분).
"""
import pickle, sys, time
from pathlib import Path
from dataclasses import replace

HERE = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")

import hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = HERE / "cache"
H._CTX_CACHE = HERE / "_ctx_B.pkl"
H._MEMO_PATH = HERE / "_memo_B.pkl"

from app.trading.macd2 import config          # noqa: E402

_Q3_PATH = HERE / "_q3base.pkl"
H.load_memo()
PROD_BASE = H._MEMO["base"]
Q3_BASE = pickle.load(open(_Q3_PATH, "rb")) if _Q3_PATH.exists() else {}

X = H.X2LITE
NB = {"tp1": 3.5, "tp2": 8.0, "tp1_ratio": 0.0, "off_tp2": 4.0}

# 4 기준 전략 — 09-18 연구 정의 그대로
SPEC = {
    "X2lite_W1": (None,     {},                                        False, False),
    "H50":       (None,     {},                                        False, True),
    "N1":        (dict(NB), {"trail_stop_pct": 1.5, "aft_tp_pct": 4.0}, True, True),
    "N1_Safe":   (dict(NB), {"trail_stop_pct": 1.5},                    True, True),
}


def ctx(n=78):
    return H.build_ctx(n)


def run(name, c, dates, *, cfg=None, po=None, q3=None, h50=None, **extra):
    """SPEC 이름이면 그 정의로, 아니면 명시 인자로 실행."""
    if name in SPEC and cfg is None and po is None:
        cfg, po, q3, h50 = SPEC[name]
    q3 = bool(q3); h50 = bool(h50)
    if cfg is not None:
        H.D_VARIANTS[name] = dict(cfg)
        extra["d_variant"] = name
    p_ = replace(X, **po) if po else X
    extra["h50"] = h50
    if q3:
        H._MEMO["base"] = Q3_BASE
        old = config.QUALITY_SCORE_THRESHOLD
        config.QUALITY_SCORE_THRESHOLD = 3
        try:
            return H.run_chain(c, p_, dates=dates, **extra)
        finally:
            config.QUALITY_SCORE_THRESHOLD = old
            H._MEMO["base"] = PROD_BASE
    return H.run_chain(c, p_, dates=dates, **extra)


def save():
    H.save_memo()
    with open(_Q3_PATH, "wb") as fh:
        pickle.dump(Q3_BASE, fh)
