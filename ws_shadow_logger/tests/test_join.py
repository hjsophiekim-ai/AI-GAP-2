import math
from datetime import datetime, timedelta

import pandas as pd
import pytest

from ws_shadow_logger import join
from ws_shadow_logger.aggregator import SNAPSHOT_COLUMNS

DAY = "20261015"
T0 = datetime(2026, 10, 15, 10, 0, 0)


def write(root, sym, prices, imb=0.2):
    rows = []
    for i, p in enumerate(prices):
        r = {c: None for c in SNAPSHOT_COLUMNS}
        r.update(ts=(T0 + timedelta(seconds=i)).isoformat(), symbol=sym, price=p, imb_5=imb, imb_1=imb, cttr=110.0,
                 buy_vol_1s=3, sell_vol_1s=1, trades_1s=2, spread_bp=5.0, buy_ratio_10s=0.75, trades_per_s_10=2)
        rows.append(r)
    d = root / "snapshots" / "2026-10-15"; d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=SNAPSHOT_COLUMNS).to_csv(d / f"{sym}.csv", index=False)


def dec(dp, sec, entry=100.0, dr="UP_RED"):
    return dict(eid=f"e{dp}", day=DAY, dp=dp, t=T0 + timedelta(seconds=sec), dir=dr, entry_price=entry)


def test_labels_and_no_lookahead(tmp_path):
    n = 1800
    path = [99.0] * 120 + [100.5] * 60 + [101.0] * (n - 180)          # 손실 → 회복
    write(tmp_path, "0193T0", path); write(tmp_path, "000660", [1.0] * n, imb=0.4)
    out = join.build(pd.DataFrame([dec("A", 60)]), tmp_path)
    r = out.iloc[0]
    assert r.data_ok and r.label == "recover" and r.net_t == pytest.approx(-1.0)
    assert r.e_imb_5 == pytest.approx(0.2) and r.h_imb_5 == pytest.approx(0.4)
    assert r.e_buy_ratio_30s == pytest.approx(0.75) and r.e_cttr_log == pytest.approx(math.log(1.1))
    write(tmp_path, "0193T0", path[:61] + [50.0] * (n - 61))           # t 이후만 바꿈 → feature 불변, 라벨만 변함
    out2 = join.build(pd.DataFrame([dec("A", 60)]), tmp_path)
    fcols = [c for c in out.columns if c.startswith(("e_", "h_"))]
    pd.testing.assert_series_equal(out[fcols].iloc[0], out2[fcols].iloc[0], check_names=False)
    assert out2.iloc[0].label == "collapse"


def test_profit_and_opposite_labels_and_short_sign(tmp_path):
    n = 1800
    write(tmp_path, "0197X0", [101.0] * 100 + [102.5] * 100 + [100.1] * (n - 200))
    write(tmp_path, "000660", [1.0] * n, imb=0.4)
    out = join.build(pd.DataFrame([dec("B", 50, dr="DOWN_BLUE"), dec("C", 50, dr="DOWN_BLUE")]), tmp_path)
    b, c = out.set_index("dp").loc["B"], out.set_index("dp").loc["C"]
    assert b.label == "cont2" and c.label == "true_reversal"
    assert b.h_imb_5 == pytest.approx(-0.4) and b.h_buy_ratio_10s == pytest.approx(0.25)   # 인버스 보유 → 하이닉스 부호 반전


def test_stale_snapshot_is_not_ok(tmp_path):
    write(tmp_path, "0193T0", [99.0] * 60); write(tmp_path, "000660", [1.0] * 60)
    out = join.build(pd.DataFrame([dec("A", 600)]), tmp_path)          # 데이터 끝난 뒤 9분
    assert not out.iloc[0].data_ok
