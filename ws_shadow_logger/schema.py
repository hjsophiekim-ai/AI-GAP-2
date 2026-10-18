"""KIS 실시간 TR 컬럼 정의 — 공식 예제(open-trading-api examples_llm/domestic_stock/*)에서 그대로 옮김."""

CCNL_KRX = [  # H0STCNT0 국내주식 실시간체결가(KRX)
    "MKSC_SHRN_ISCD", "STCK_CNTG_HOUR", "STCK_PRPR", "PRDY_VRSS_SIGN",
    "PRDY_VRSS", "PRDY_CTRT", "WGHN_AVRG_STCK_PRC", "STCK_OPRC",
    "STCK_HGPR", "STCK_LWPR", "ASKP1", "BIDP1", "CNTG_VOL", "ACML_VOL",
    "ACML_TR_PBMN", "SELN_CNTG_CSNU", "SHNU_CNTG_CSNU", "NTBY_CNTG_CSNU",
    "CTTR", "SELN_CNTG_SMTN", "SHNU_CNTG_SMTN", "CCLD_DVSN", "SHNU_RATE",
    "PRDY_VOL_VRSS_ACML_VOL_RATE", "OPRC_HOUR", "OPRC_VRSS_PRPR_SIGN",
    "OPRC_VRSS_PRPR", "HGPR_HOUR", "HGPR_VRSS_PRPR_SIGN", "HGPR_VRSS_PRPR",
    "LWPR_HOUR", "LWPR_VRSS_PRPR_SIGN", "LWPR_VRSS_PRPR", "BSOP_DATE",
    "NEW_MKOP_CLS_CODE", "TRHT_YN", "ASKP_RSQN1", "BIDP_RSQN1",
    "TOTAL_ASKP_RSQN", "TOTAL_BIDP_RSQN", "VOL_TNRT",
    "PRDY_SMNS_HOUR_ACML_VOL", "PRDY_SMNS_HOUR_ACML_VOL_RATE",
    "HOUR_CLS_CODE", "MRKT_TRTM_CLS_CODE", "VI_STND_PRC",
]
# H0UNCNT0 (통합) 은 CCLD_DVSN 자리가 CNTG_CLS_CODE 인 것만 다르다(같은 의미: 1 매수 / 5 매도).
CCNL_TOTAL = [("CNTG_CLS_CODE" if c == "CCLD_DVSN" else c) for c in CCNL_KRX]

_ASK = [f"ASKP{i}" for i in range(1, 11)]
_BID = [f"BIDP{i}" for i in range(1, 11)]
_ASKQ = [f"ASKP_RSQN{i}" for i in range(1, 11)]
_BIDQ = [f"BIDP_RSQN{i}" for i in range(1, 11)]
ASP_KRX = ["MKSC_SHRN_ISCD", "BSOP_HOUR", "HOUR_CLS_CODE", *_ASK, *_BID, *_ASKQ, *_BIDQ,
           "TOTAL_ASKP_RSQN", "TOTAL_BIDP_RSQN", "OVTM_TOTAL_ASKP_RSQN", "OVTM_TOTAL_BIDP_RSQN",
           "ANTC_CNPR", "ANTC_CNQN", "ANTC_VOL", "ANTC_CNTG_VRSS", "ANTC_CNTG_VRSS_SIGN",
           "ANTC_CNTG_PRDY_CTRT", "ACML_VOL", "TOTAL_ASKP_RSQN_ICDC", "TOTAL_BIDP_RSQN_ICDC",
           "OVTM_TOTAL_ASKP_ICDC", "OVTM_TOTAL_BIDP_ICDC", "STCK_DEAL_CLS_CODE"]           # H0STASP0
ASP_TOTAL = ASP_KRX + ["KMID_PRC", "KMID_TOTAL_RSQN", "KMID_CLS_CODE",
                       "NMID_PRC", "NMID_TOTAL_RSQN", "NMID_CLS_CODE"]                    # H0UNASP0

# HDFSCNT0 해외주식 실시간지연체결가 — 공식 examples_user/overseas_stock/overseas_stock_functions_ws.py delayed_ccnl.
# 미국은 0분 지연(실시간 무료). KYMD/KHMS = 한국 일자/시각, XYMD/XHMS = 현지 일자/시각, EVOL = 체결량, TVOL = 누적거래량.
# 2026-10-18 수정: 공식 HDFSCNT0 은 **RSYM(실시간종목코드) 이 1번 필드**다. 이게 빠져 있어서
# 폭이 25 로 잡혔고, 한 프레임에 레코드가 여러 개일 때(건수>1) 레코드마다 1칸씩 밀려
# SYMB 에 가격/체결강도가, KYMD/KHMS 에 현지 일자·시각이 들어갔다(2026-10-16 실측).
OVRS_CCNL = ["RSYM", "SYMB", "ZDIV", "TYMD", "XYMD", "XHMS", "KYMD", "KHMS", "OPEN", "HIGH", "LOW",
             "LAST", "SIGN", "DIFF", "RATE", "PBID", "PASK", "VBID", "VASK", "EVOL", "TVOL",
             "TAMT", "BIVL", "ASVL", "STRN", "MTYP"]

COLUMNS = {"H0STCNT0": CCNL_KRX, "H0UNCNT0": CCNL_TOTAL, "H0STASP0": ASP_KRX, "H0UNASP0": ASP_TOTAL,
           "HDFSCNT0": OVRS_CCNL}
KIND = {"H0STCNT0": "trade", "H0UNCNT0": "trade", "H0STASP0": "book", "H0UNASP0": "book",
        "HDFSCNT0": "us_trade"}
