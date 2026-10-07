"""axval — 채택조건 검증 배터리. 전부 같은 ctx·같은 거래목록에서 창만 자른다."""
import numpy as np
from common import compound, excl_topn, pnl
import hengine as H


def wins(dates, k):
    """dates 를 k 등분 (앞에서부터, 나머지는 마지막 조각에)."""
    n = len(dates) // k
    return [dates[i * n: (i + 1) * n if i < k - 1 else len(dates)] for i in range(k)]


def m(ts, dates):
    mm = H.metrics(ts, dates)
    if not mm.get("trades"):
        return {"trades": 0, "compound_pct": 0.0, "pf": None, "mdd_pct": 0.0,
                "top10_excl_pct": 0.0}
    return mm


def diff_trades(a, b):
    """a(변형) vs b(기준) 거래 단위 차이. 키는 (일자, 진입시각)."""
    ka = {(t["date"], t["entry_time"]): t for t in a}
    kb = {(t["date"], t["entry_time"]): t for t in b}
    common = set(ka) & set(kb)
    d = [(k, pnl(ka[k]) - pnl(kb[k])) for k in common
         if abs(pnl(ka[k]) - pnl(kb[k])) > 1e-9]
    only_a = sorted(set(ka) - set(kb))
    only_b = sorted(set(kb) - set(ka))
    return sorted(d, key=lambda x: -abs(x[1])), only_a, only_b


def report(name, a, b, dates, *, label30=30):
    """a=후보, b=N1. 채택조건 점검표를 dict 로."""
    D78, D30 = dates, dates[-label30:]
    out = {"name": name}
    for tag, W in (("78d", D78), ("30d", D30)):
        ma, mbb = m(a, W), m(b, W)
        out[f"{tag}_a"] = ma["compound_pct"]; out[f"{tag}_b"] = mbb["compound_pct"]
        out[f"{tag}_d"] = ma["compound_pct"] - mbb["compound_pct"]
        out[f"{tag}_pf_a"] = ma["pf"]; out[f"{tag}_pf_b"] = mbb["pf"]
        out[f"{tag}_mdd_a"] = ma["mdd_pct"]; out[f"{tag}_mdd_b"] = mbb["mdd_pct"]
    # Top-N 제거
    for k in (1, 3, 10):
        out[f"top{k}_d"] = excl_topn(a, D78, k) - excl_topn(b, D78, k)
        out[f"top{k}_d30"] = excl_topn(a, D30, k) - excl_topn(b, D30, k)
    # 앞/뒤 분할
    h = wins(D78, 2)
    out["half"] = [compound(a, w) - compound(b, w) for w in h]
    # 5분할
    out["k5"] = [compound(a, w) - compound(b, w) for w in wins(D78, 5)]
    # walk-forward 6분할
    out["wf6"] = [compound(a, w) - compound(b, w) for w in wins(D78, 6)]
    out["wf6_win"] = sum(1 for x in out["wf6"] if x > 1e-9)
    out["wf6_lose"] = sum(1 for x in out["wf6"] if x < -1e-9)
    # 거래 단위
    d, oa, ob = diff_trades(a, b)
    out["chg"] = len(d); out["up"] = sum(1 for _, x in d if x > 0)
    out["dn"] = sum(1 for _, x in d if x < 0)
    out["sum_d"] = sum(x for _, x in d)
    out["only_a"] = len(oa); out["only_b"] = len(ob)
    out["diffs"] = d
    # 소수의존: 개선 상위 1/2/3 거래를 빼면 단순합 우위가 남는가
    ups = sorted((x for _, x in d if x > 0), reverse=True)
    for k in (1, 2, 3):
        out[f"sum_d_ex{k}"] = out["sum_d"] - sum(ups[:k])
    return out


def verdict(r):
    """채택조건: 30·78 비악화, PF/MDD 비악화, -Top10 비악화, WF>=4/6, 소수의존 아님."""
    c = {}
    c["30d 비악화"] = r["30d_d"] >= -1e-9
    c["78d 비악화"] = r["78d_d"] >= -1e-9
    c["PF 비악화(78)"] = (r["78d_pf_a"] or 0) >= (r["78d_pf_b"] or 0) - 1e-9
    c["PF 비악화(30)"] = (r["30d_pf_a"] or 0) >= (r["30d_pf_b"] or 0) - 1e-9
    c["MDD 비악화(78)"] = r["78d_mdd_a"] >= r["78d_mdd_b"] - 1e-9
    c["MDD 비악화(30)"] = r["30d_mdd_a"] >= r["30d_mdd_b"] - 1e-9
    c["-Top10 비악화(78)"] = r["top10_d"] >= -1e-9
    c["-Top10 비악화(30)"] = r["top10_d30"] >= -1e-9
    c["WF 4/6 이상"] = r["wf6_win"] >= 4
    c["앞/뒤 둘다 비악화"] = all(x >= -1e-9 for x in r["half"])
    c["상위3 개선거래 제외 후 양수"] = r["sum_d_ex3"] > 0
    return c
