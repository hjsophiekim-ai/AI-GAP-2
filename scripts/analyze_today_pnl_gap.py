"""오늘 분봉 기준 수정 전후 손익 차이 원인 분석."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from scripts.replay_today_weighted_range import (  # noqa: E402
    INITIAL_CASH,
    _merge_fixture_hynix,
    fetch_full_day_1min,
    run_replay,
)
from app.trading.hynix_symbols import LONG_SYMBOL, SHORT_SYMBOL as INVERSE_SYMBOL, SIGNAL_SYMBOL  # noqa: E402


def _session_moves(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ret_pct"] = out["close"].pct_change() * 100
    out["cum_ret_pct"] = (out["close"] / out["close"].iloc[0] - 1) * 100
    return out


def _segment_return(df: pd.DataFrame, start_hm: str, end_hm: str) -> float | None:
    today = df["datetime"].iloc[0].strftime("%Y-%m-%d")
    s = pd.Timestamp(f"{today} {start_hm}")
    e = pd.Timestamp(f"{today} {end_hm}")
    seg = df[(df["datetime"] >= s) & (df["datetime"] <= e)]
    if len(seg) < 2:
        return None
    return (seg["close"].iloc[-1] / seg["close"].iloc[0] - 1) * 100


def main() -> int:
    print("=" * 72)
    print("오늘 분봉 데이터 수집 및 손익 갭 분석")
    print("=" * 72)
    hynix = _merge_fixture_hynix(fetch_full_day_1min(SIGNAL_SYMBOL))
    long_df = fetch_full_day_1min(LONG_SYMBOL)
    inv_df = fetch_full_day_1min(INVERSE_SYMBOL)
    print(f"하이닉스: {len(hynix)}봉  레버리지: {len(long_df)}봉  인버스: {len(inv_df)}봉")

    long_m = _session_moves(long_df)
    inv_m = _session_moves(inv_df)
    hyn_m = _session_moves(hynix)

    print("\n[1] 당일 ETF 방향별 구간 수익률 (분봉 close 기준)")
    segments = [
        ("09:00", "10:30", "오전 상승"),
        ("10:30", "12:00", "오전~점심 횡보/상승"),
        ("12:00", "13:30", "점심 하락"),
        ("13:30", "14:30", "오후 반등 전"),
        ("14:30", "15:30", "마감"),
    ]
    for s, e, label in segments:
        lr = _segment_return(long_m, s, e)
        ir = _segment_return(inv_m, s, e)
        hr = _segment_return(hyn_m, s, e)
        print(f"  {label:16s} {s}~{e}  레버={lr:+.2f}%  인버스={ir:+.2f}%  하이닉스={hr:+.2f}%")

    print(f"\n  장전체(09:01~15:30)  레버={(long_m['close'].iloc[-1]/long_m['close'].iloc[0]-1)*100:+.2f}%"
          f"  인버스={(inv_m['close'].iloc[-1]/inv_m['close'].iloc[0]-1)*100:+.2f}%")

    result = run_replay(hynix, long_df, inv_df)
    trades = result["trades"]

    print("\n[2] 수정 후 실제 체결 손익 (라운드트립별)")
    buys = [e for e in result["events"] if e["action"] == "매수"]
    sell_by_time = [t for t in trades if t["side"] == "SELL" and t.get("reason") != "EOD"]
    # aggregate sells per buy cycle using events order
    cycle_pnl = []
    for i, b in enumerate(buys):
        sym = LONG_SYMBOL if b["symbol"] == "레버리지" else INVERSE_SYMBOL
        # all sells until next buy
        next_buy_time = buys[i + 1]["time"] if i + 1 < len(buys) else "99:99:99"
        related = [
            t for t in sell_by_time
            if t["symbol"] == sym
            and b["time"] <= t["time"].strftime("%H:%M:%S") < next_buy_time
        ]
        pnl = sum(t["net_pnl"] for t in related)
        cycle_pnl.append({
            "time": b["time"],
            "symbol": b["symbol"],
            "path": b.get("path"),
            "entry": b["price"],
            "pnl": pnl,
            "sells": len(related),
        })
    for c in cycle_pnl:
        print(f"  {c['time']} {c['symbol']:6s} {c['path']:12s} 진입 {c['entry']:,.0f}  "
              f"순손익 {c['pnl']:+,.0f}  (매도 {c['sells']}회)")

    total = sum(c["pnl"] for c in cycle_pnl)
    print(f"  합계(부분매도 포함): {total:+,.0f} KRW  vs 리플레이 요약 {result['net_pnl_krw']:+,.0f}")

    print("\n[3] 수정 후 놓친 주요 구간 (episode 잠금/전환 미충족 추정)")
    missed = [
        ("12:51~13:00", "인버스", "하이닉스 급락 → 인버스 상승", "12:51", "13:00"),
        ("14:18~14:30", "레버리지", "반등 구간", "14:18", "14:30"),
        ("09:30~10:00", "인버스/레버", "오전 초반 추가 왕복", "09:30", "10:00"),
    ]
    for label, sym_name, desc, s, e in missed:
        df = inv_m if sym_name == "인버스" else long_m
        r = _segment_return(df, s, e)
        alloc = 0.59 * INITIAL_CASH
        est = alloc * (r / 100) if r is not None else None
        print(f"  {label} {desc}: {sym_name} 구간수익 {r:+.2f}%  "
              f"→ 59% 배분 이론상 {est:+,.0f} KRW (비용 전)")

    print("\n[4] 수정 전 +373K가 과대평가된 이유")
    print("  - entry_done 매도 후 즉시 리셋 → 동일 episode 20초 MACD 실패 probe 17회 반복")
    print("  - 1분봉 선형보간 5초 체결: 분봉 내 최적 타이밍을 매 틱마다 잡아 비현실적 체결")
    print("  - 32회 왕복 중 53%가 20초 미만 → 비용·슬리피지 반영 시 실전 수익 대부분 소멸")
    print("  - 보수적 체결 기준은 수정 전에도 측정 안 됨; 수정 후 -10K가 더 현실적 베이스라인")

    print("\n[5] 손익 갭 구조적 원인")
    winners = [c for c in cycle_pnl if c["pnl"] > 0]
    losers = [c for c in cycle_pnl if c["pnl"] <= 0]
    print(f"  수익 사이클: {len(winners)}건  합 {sum(c['pnl'] for c in winners):+,.0f}")
    print(f"  손실 사이클: {len(losers)}건  합 {sum(c['pnl'] for c in losers):+,.0f}")
    print("  a) 거래 기회 32→5: 구조 이벤트 없이 episode 잠금 → 오후 인버스·반등 미참여")
    print("  b) REVERSAL 1회 제한: probe 실패 후 같은 방향 재시도 차단 (의도된 과매매 방지)")
    print("  c) 45초 홀드 후 MACD 미확인 청산: 09:09 REVERSAL은 +0.39%에서 청산(소폭 이익)")
    print("  d) UP episode 장시간 유지: opposite_episode(swing돌파) 없으면 DOWN 전환 불가")

    print("\n[6] 과매매 줄이면서 수익 극대화 방향 (제안)")
    proposals = [
        ("PROBE_FAILED는 REVERSAL만 잠금", "CONTINUATION은 구조 확인 후 허용 → 오후 인버스 참여 가능"),
        ("episode 전환: swing돌파 + 5/10 confirm", "VWAP 재돌파만으로도 opposite episode 인정 (오늘 12:51 인버스)"),
        ("REVERSAL 45초 홀드 유지", "MACD 확인 시 scale-in 40~60% 유지 → 수익 사이클 확대"),
        ("probe 실패 청산", "45초 후 구조·ETF 유지 + 미실현이익>비용이면 CONTINUATION 전환(청산 대신)"),
        ("리플레이 체결", "선형보간 대신 분봉 open/close 보수·낙관 양쪽 유지해 의사결정만 평가"),
        ("청산 후 재진입", "CONTINUATION 완료 후 swing/VWAP unlock만 적용, REVERSAL 1회 규칙은 유지"),
    ]
    for title, detail in proposals:
        print(f"  • {title}")
        print(f"    → {detail}")

    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
