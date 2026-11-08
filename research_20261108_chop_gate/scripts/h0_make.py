"""CG 하네스 생성 — ARM3 하네스에 '왕복장 × MU-HY 괴리 신규진입 차단' 패치 2곳만 덧붙인다.

production 코드는 건드리지 않는다. 패치는 전부 monkeypatch 범위 안이고,
`WK_CGATE=0`(기본)이면 ARM3 하네스와 **코드상 완전히 동일**하다.

차단 지점은 `worker._resolve_tw2_3slot_candidate_body` 진입부다 — 2026-10-13 OPPQ 연구가
쓴 자리와 같다. 거기서 막으면 주문도 슬롯 점유도 일어나지 않아, **뒤 후보가 그 슬롯을
production 규칙대로 그대로 쓴다**(슬롯 연쇄효과가 재생에 반영된다).

앵커가 1개가 아니면 생성 자체를 실패시킨다(조용한 어긋남 방지).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "research_20261030_tick_arm3" / "harness" / "test_zz_arm3.py"
DST = ROOT / "research_20261108_chop_gate" / "harness" / "test_zz_cg.py"

A1 = "    if _MODE:\n"
A2 = "q2=Q2LOG, oppq=OPPQLOG,"

PATCH = '''    # ── 2026-11-08 CG: 왕복장 × MU-HY 괴리 신규진입 차단 (연구용, 기본 OFF) ──
    # WK_CGATE=1 일 때만 동작한다. MU 상태가 CONFIRM 또는 WAIT 인 후보에 대해
    #   최근 60분 하이닉스 효율비 < WK_CG_ER  ∧  mu_hy_gap >= WK_CG_GAP
    # 이면 **신규진입만** 막는다. 둘 중 하나라도 값이 없으면 통과(fail-open).
    # 차단해도 슬롯을 쓰지 않으므로 뒤 후보가 production 규칙대로 그 슬롯을 쓴다.
    # ── 2026-11-08 WG: WAIT 선택진입의 반대gap 상한만 바꾼다 (연구용, 기본 OFF) ──
    # WK_WGAP 를 주면 config.MU_WAIT_OPPOSITE_GAP_MAX 를 그 값으로 덮는다. 아주 큰 값을 주면
    # 사실상 "체결강도 MATCH 만" 으로 승인된다(g_prev 결측은 production 그대로 불통과).
    _WGAP = os.environ.get("WK_WGAP", "")
    if _WGAP:
        monkeypatch.setattr(config, "MU_WAIT_OPPOSITE_GAP_MAX", float(_WGAP))

    _CG = os.environ.get("WK_CGATE", "0") == "1"
    _CG_ER = float(os.environ.get("WK_CG_ER", "0.1223"))
    _CG_GAP = float(os.environ.get("WK_CG_GAP", "0.0"))
    CGLOG = []
    if _CG:
        from app.trading.macd2 import mu_assist as _cg_mua, mu_catchup as _cg_cu

        def _cg_er60(df_1m, now):
            """[now-60분, now) 에 **끝난** 1분봉 종가의 효율비. 미래 데이터를 쓰지 않는다."""
            if df_1m is None or len(df_1m) == 0 or "datetime" not in df_1m:
                return None
            ts = pd.to_datetime(df_1m["datetime"], errors="coerce")
            if getattr(ts.dt, "tz", None) is not None:
                ts = ts.dt.tz_convert(config.KST).dt.tz_localize(None)
            n = now.astimezone(config.KST).replace(tzinfo=None) if now.tzinfo else now
            m = ((ts + pd.Timedelta(minutes=1)) <= n) & (ts >= n - pd.Timedelta(minutes=60))
            c = pd.to_numeric(df_1m.loc[m.values, "close"], errors="coerce").dropna()
            if len(c) < 5:
                return None
            tot = float(c.diff().abs().sum())
            if tot <= 0:
                return None
            return abs(float(c.iloc[-1]) - float(c.iloc[0])) / tot

        _o_cgbody = worker._resolve_tw2_3slot_candidate_body

        def _cg_body(**kw):
            sid = str(kw["signal_id"])
            st = _cg_mua.entry_status_for(sid)
            if st in (_cg_mua.ENTRY_CONFIRM, _cg_mua.ENTRY_WAIT):
                er = _cg_er60(kw.get("df_1m"), kw["now"])
                gap = None
                try:
                    ev = _cg_cu.evaluate(kw["direction"], kw["flag_bar_dt"], kw["now"], kw.get("df_1m"),
                                         mu_series=_cg_mua.load_mu_1m(kw["now"]))
                    gap = ev.get("mu_hy_gap")
                except Exception:          # noqa: BLE001 — 판정 불가는 fail-open
                    gap = None
                hit = (er is not None and gap is not None
                       and er < _CG_ER and float(gap) >= _CG_GAP)
                CGLOG.append(dict(t=kw["now"].isoformat(), sid=sid, mu=st,
                                  dir=getattr(kw["direction"], "value", str(kw["direction"])),
                                  flag=kw["flag_bar_dt"].isoformat(),
                                  er60=None if er is None else round(float(er), 5),
                                  gap=None if gap is None else round(float(gap), 3),
                                  blocked=bool(hit)))
                if hit:
                    kw["state"].processed_signal_ids.append(kw["signal_id"])
                    return None
            return _o_cgbody(**kw)
        monkeypatch.setattr(worker, "_resolve_tw2_3slot_candidate_body", _cg_body)
'''


def main():
    src = SRC.read_text(encoding="utf-8")
    for anchor in (A1, A2):
        if src.count(anchor) != 1:
            print(f"!! 앵커가 1개가 아니다({src.count(anchor)}): {anchor!r}")
            sys.exit(1)
    out = src.replace(A1, PATCH + A1, 1)
    out = out.replace(A2, A2 + " cg=CGLOG, cgate=bool(_CG), cg_er=_CG_ER, cg_gap=_CG_GAP, wgap=_WGAP,", 1)
    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_text(out, encoding="utf-8")
    print(f"생성: {DST}  ({len(out.splitlines())}행, 원본 {len(src.splitlines())}행)")


if __name__ == "__main__":
    main()
