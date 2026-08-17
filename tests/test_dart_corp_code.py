"""collector/dart_corp_code.py - corpCode.xml 파싱과 응답 검증. 네트워크/DB는 mock."""
import io
import zipfile

import pytest

from collector import dart_corp_code as dcc


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<result>
  <list>
    <corp_code>00126380</corp_code>
    <corp_name>삼성전자</corp_name>
    <stock_code>005930</stock_code>
    <modify_date>20260101</modify_date>
  </list>
  <list>
    <corp_code>00164742</corp_code>
    <corp_name>비상장회사</corp_name>
    <stock_code> </stock_code>
    <modify_date>20260101</modify_date>
  </list>
  <list>
    <corp_code>00164779</corp_code>
    <corp_name>기아</corp_name>
    <stock_code>000270</stock_code>
    <modify_date>20260101</modify_date>
  </list>
</result>""".encode("utf-8")


def _zip_bytes(payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("CORPCODE.xml", payload)
    return buf.getvalue()


def test_parse_keeps_only_listed_companies():
    """종목코드가 비어 있는 비상장사는 제외된다 (DART 목록의 대부분이 비상장)."""
    mapping = dcc.parse(SAMPLE_XML)

    assert mapping == {"005930": "00126380", "000270": "00164779"}


def test_fetch_xml_rejects_non_zip_response(mocker):
    """키가 틀리면 DART가 zip 대신 오류 XML을 200으로 돌려준다 — zip으로 열지 않는다."""
    mocker.patch.object(dcc.config, "DART_API_KEY", "dummy")
    resp = mocker.MagicMock()
    resp.content = b'<result><status>010</status><message>\xeb\x93\xb1\xeb\xa1\x9d</message></result>'
    mocker.patch("requests.get", return_value=resp)

    with pytest.raises(RuntimeError, match="zip 응답이 아님"):
        dcc.fetch_xml()


def test_fetch_xml_requires_api_key(mocker):
    mocker.patch.object(dcc.config, "DART_API_KEY", "")

    with pytest.raises(RuntimeError, match="DART_API_KEY"):
        dcc.fetch_xml()


def test_collect_updates_only_known_codes(mock_db, mocker):
    """instruments에 없는 종목은 건너뛴다 (DART에는 우리가 안 보는 종목도 있다)."""
    mocker.patch.object(dcc.config, "DART_API_KEY", "dummy")
    resp = mocker.MagicMock()
    resp.content = _zip_bytes(SAMPLE_XML)
    mocker.patch("requests.get", return_value=resp)

    mock_db.fetchall.return_value = [{"code": "005930"}]   # 기아는 미보유
    mock_db.fetchone.return_value = {"n": 1}

    updated = dcc.collect()

    assert updated == 1
    rows = mock_db.executemany.call_args[0][1]
    assert rows == [("00126380", "005930")]
