"""common.py 는 `import hengine as H` 후 H.metrics 만 쓴다.
연구 당시 디렉터리에는 hengine.py(= hengine5 의 모체)가 같이 있었다.
여기서는 같은 모듈 상태를 공유하도록 hengine5 를 그대로 재노출한다."""
from hengine5 import *  # noqa: F401,F403
from hengine5 import metrics, run_chain, build_ctx, X2LITE, D_VARIANTS  # noqa: F401
