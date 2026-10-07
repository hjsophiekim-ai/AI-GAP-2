import os,sys,json
sys.path.insert(0,os.getcwd())
from dotenv import load_dotenv; load_dotenv(".env")
from app.trading import kis_client as kc
c=kc.KISClient(os.environ["KIS_REAL_APP_KEY"],os.environ["KIS_REAL_APP_SECRET"],os.environ["KIS_ACCOUNT_NO"],os.environ.get("KIS_REAL_ACCOUNT_PRODUCT_CODE","01"),mode="real")
assert c._load_token_cache(), "no cached token - abort (no tokenP)"
url=f"{c.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
p={"CANO":c.account_no,"ACNT_PRDT_CD":c.product_code,"INQR_STRT_DT":"20260930","INQR_END_DT":"20260930","SLL_BUY_DVSN_CD":"00","INQR_DVSN":"00","PDNO":"","CCLD_DVSN":"00","ORD_GNO_BRNO":"","ODNO":"","INQR_DVSN_3":"00","INQR_DVSN_1":"","CTX_AREA_FK100":"","CTX_AREA_NK100":""}
r=c._request_with_token_retry("GET",url,kc.TR_ORDER_HISTORY_REAL,params=p,timeout=(3,15)); d=r.json()
print("orders rt_cd",d.get("rt_cd"),d.get("msg1"))
for o in d.get("output1") or []:
    print({k:o.get(k) for k in ["ord_tmd","odno","orgn_odno","pdno","prdt_name","sll_buy_dvsn_cd_name","ord_dvsn_name","ord_qty","tot_ccld_qty","avg_prvs","rmn_qty","rjct_qty","cncl_yn","infm_tmd"]})
b=c.get_balance()
print("balance err",b.get("error"),"positions",json.dumps(b.get("positions"),ensure_ascii=False))
