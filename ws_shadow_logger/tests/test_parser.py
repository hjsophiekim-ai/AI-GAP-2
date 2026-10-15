import json
from datetime import datetime

import pytest

from ws_shadow_logger import parser, stub
from ws_shadow_logger.schema import COLUMNS

T = datetime(2026, 10, 15, 10, 0, 0)


def test_trade_frame_fields():
    f = stub.trade("H0STCNT0", "0193T0", T, 6750, 12, "1", 105.3, 100, 90, 190)
    m = parser.parse_frame(f)
    assert m["type"] == "data" and m["tr_id"] == "H0STCNT0"
    r = m["records"][0]
    assert len(r) == len(COLUMNS["H0STCNT0"]) == 46
    assert r["STCK_PRPR"] == "6750" and r["CCLD_DVSN"] == "1" and r["CTTR"] == "105.3"


def test_unified_trade_side_column():
    m = parser.parse_frame(stub.trade("H0UNCNT0", "000660", T, 1420000, 3, "5", 98.0, 1, 2, 3))
    assert m["records"][0]["CNTG_CLS_CODE"] == "5"


def test_book_frame_and_multi_record():
    one = stub.book("H0STASP0", "0193T0", T, 6750, 5, list(range(1, 11)), list(range(11, 21)))
    body = one.split("|", 3)[3]
    m = parser.parse_frame(f"0|H0STASP0|002|{body}^{body}")
    assert len(m["records"]) == 2
    assert m["records"][1]["BIDP_RSQN10"] == "10" and m["records"][0]["ASKP1"] == "6755"
    assert len(COLUMNS["H0UNASP0"]) == 65 and len(COLUMNS["H0STASP0"]) == 59


def test_system_pingpong_encrypted_errors():
    assert parser.parse_frame(json.dumps({"header": {"tr_id": "PINGPONG"}}))["type"] == "pingpong"
    s = parser.parse_frame(json.dumps({"header": {"tr_id": "H0STCNT0", "tr_key": "0193T0"},
                                       "body": {"rt_cd": "0", "msg1": "SUBSCRIBE SUCCESS"}}))
    assert s["type"] == "system" and s["rt_cd"] == "0"
    assert parser.parse_frame("1|H0STCNI0|001|xxxx")["type"] == "encrypted"
    with pytest.raises(parser.FrameError):
        parser.parse_frame("0|H0STCNT0|001|a^b")


def test_subscribe_message_is_register_only():
    m = json.loads(parser.subscribe_message("AK", "H0STASP0", "0197X0"))
    assert m["header"]["tr_type"] == "1"
    assert m["body"]["input"] == {"tr_id": "H0STASP0", "tr_key": "0197X0"}
