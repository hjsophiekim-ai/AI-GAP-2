"""READ-ONLY: KIS 국내주식 분봉(통합 UN = KRX+NXT) 수집 -> scratchpad.
저장소 replay_*_1m.csv 와 같은 소스(08:00~20:00 프리/애프터 포함)를 재현하기 위함.
시세 조회만 하며 주문/계좌 API 는 호출하지 않는다.
"""
import datetime, json, os, sys, time
import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
ROOT = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
BASE = "https://openapi.koreainvestment.com:9443"
SYM = {"hynix": "000660", "long": "0193T0", "inverse": "0197X0"}


def creds():
    env = {}
    for line in open(ROOT + "/.env", encoding="utf-8", errors="replace"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env["KIS_APP_KEY"], env["KIS_APP_SECRET"]


def token(ak, sk):
    p = W + "/_kis_tok.json"
    if os.path.exists(p):
        t = json.load(open(p))
        if datetime.datetime.fromisoformat(t["exp"]) > datetime.datetime.now():
            return t["tok"]
    for i in range(8):
        r = requests.post(BASE + "/oauth2/tokenP", timeout=20,
                          json={"grant_type": "client_credentials", "appkey": ak, "appsecret": sk})
        if r.status_code == 200:
            break
        print("token retry", i, r.status_code, r.text[:80]); time.sleep(20)
    r.raise_for_status()
    tok = r.json()["access_token"]
    json.dump({"tok": tok, "exp": (datetime.datetime.now() + datetime.timedelta(hours=6)).isoformat()},
              open(p, "w"))
    return tok


def chunk(h, code, hhmmss):
    p = {"FID_ETC_CLS_CODE": "", "FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": code,
         "FID_INPUT_HOUR_1": hhmmss, "FID_PW_DATA_INCU_YN": "Y"}
    r = requests.get(BASE + "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                     headers=h, params=p, timeout=20)
    r.raise_for_status()
    return r.json().get("output2") or []


def day_bars(h, code, day):
    """08:00~20:00 을 30분씩 뒤로 훑어 모은다 (API 는 지정시각 이전 30봉 반환)."""
    rows, seen = {}, set()
    cur = datetime.datetime.strptime(day + " 20:00", "%Y%m%d %H:%M")
    end = datetime.datetime.strptime(day + " 07:59", "%Y%m%d %H:%M")
    while cur > end:
        out = chunk(h, code, cur.strftime("%H%M%S"))
        if not out:
            break
        got = 0
        for x in out:
            d, t = x.get("stck_bsop_date"), x.get("stck_cntg_hour")
            if d != day or not t:
                continue
            k = t
            if k in seen:
                continue
            seen.add(k)
            got += 1
            rows[k] = dict(datetime=f"{day[:4]}-{day[4:6]}-{day[6:]} {t[:2]}:{t[2:4]}:00",
                           open=float(x["stck_oprc"]), high=float(x["stck_hgpr"]),
                           low=float(x["stck_lwpr"]), close=float(x["stck_prpr"]),
                           volume=int(x["cntg_vol"]))
        oldest = min(seen) if seen else None
        if oldest is None:
            break
        nxt = datetime.datetime.strptime(day + " " + oldest, "%Y%m%d %H%M%S") - datetime.timedelta(minutes=1)
        if nxt >= cur:
            break
        cur = nxt
        time.sleep(0.15)
    df = pd.DataFrame([rows[k] for k in sorted(rows)])
    return df.reset_index(drop=True)


if __name__ == "__main__":
    ak, sk = creds()
    tok = token(ak, sk)
    h = {"authorization": f"Bearer {tok}", "appkey": ak, "appsecret": sk,
         "tr_id": "FHKST03010200", "custtype": "P"}
    for day in sys.argv[1:]:
        for tag, code in SYM.items():
            p = f"{W}/data/kis_{day}_{tag}_1m.csv"
            if os.path.exists(p):
                print("skip", p); continue
            df = day_bars(h, code, day)
            df.to_csv(p, index=False)
            print("%s %-8s %4d bars  %s ~ %s" % (day, tag, len(df),
                  df.datetime.iloc[0][11:16] if len(df) else "-",
                  df.datetime.iloc[-1][11:16] if len(df) else "-"))
