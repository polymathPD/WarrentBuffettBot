"""collector/financials.py - 금액 파싱, 연결/개별 선택, 배치 처리. 네트워크/DB는 mock."""
from datetime import date
import pytest

from collector import financials as fin


def _item(corp, fs_div, account, amount):
    return {"corp_code": corp, "fs_div": fs_div,
            "account_nm": account, "thstrm_amount": amount}


def test_amount_parsing():
    assert fin._amount("44,425,929,000,000") == 44425929000000
    assert fin._amount("-1,234") == -1234
    assert fin._amount("(1,234)") == -1234
    assert fin._amount("") is None
    assert fin._amount("-") is None
    assert fin._amount(None) is None


def test_rows_prefers_consolidated_statements():
    """연결(CFS)과 개별(OFS)이 함께 오면 연결을 쓴다."""
    data = {"list": [
        _item("00126380", "CFS", "매출액", "300,000"),
        _item("00126380", "CFS", "영업이익", "30,000"),
        _item("00126380", "OFS", "매출액", "200,000"),
        _item("00126380", "OFS", "영업이익", "20,000"),
    ]}

    rows = fin._rows(data, "2025Q4", {"00126380": "005930"})

    assert len(rows) == 1
    code, period, fs_div = rows[0][:3]
    assert (code, period, fs_div) == ("005930", "2025Q4", "CFS")
    assert rows[0][3:5] == (300000, 30000)   # revenue, op_income


def test_rows_falls_back_to_separate_statements():
    """연결재무제표를 내지 않는 회사는 개별을 쓴다."""
    data = {"list": [_item("00126380", "OFS", "매출액", "200,000")]}

    rows = fin._rows(data, "2025Q4", {"00126380": "005930"})

    assert rows[0][2] == "OFS"
    assert rows[0][3] == 200000


def test_rows_accepts_account_name_variants():
    """제출사마다 계정명 표기가 다르다 ('당기순이익(손실)', 공백 포함 등)."""
    data = {"list": [
        _item("00126380", "CFS", "수익(매출액)", "1,000"),
        _item("00126380", "CFS", "당기순이익(손실)", "-500"),
        _item("00126380", "CFS", "법인세차감전 순이익", "700"),   # 저장 대상 아님
    ]}

    rows = fin._rows(data, "2025Q4", {"00126380": "005930"})
    values = dict(zip(fin.FIELDS, rows[0][3:]))

    assert values["revenue"] == 1000
    assert values["net_income"] == -500
    assert values["op_income"] is None


def test_rows_skips_unmapped_companies():
    data = {"list": [_item("99999999", "CFS", "매출액", "1,000")]}

    assert fin._rows(data, "2025Q4", {"00126380": "005930"}) == []


@pytest.mark.parametrize("today, expected", [
    (date(2026, 8, 18), ("2026", "11012")),   # 반기보고서 제출 후
    (date(2026, 11, 20), ("2026", "11014")),  # 3분기
    (date(2026, 5, 20), ("2026", "11013")),   # 1분기
    (date(2026, 4, 10), ("2025", "11011")),   # 전년도 사업보고서 제출 후
    (date(2026, 2, 10), ("2024", "11011")),   # 전년도분은 아직 제출 전
])
def test_latest_period_follows_filing_deadlines(today, expected):
    assert fin.latest_period(today) == expected


def test_collect_batches_by_api_limit(mock_db, mocker):
    """100종목 상한에 맞춰 배치를 나눈다 (120종목 -> 2회 호출)."""
    mocker.patch.object(fin.config, "DART_API_KEY", "dummy")
    mock_db.fetchall.return_value = [
        {"code": f"{i:06d}", "dart_corp_code": f"{i:08d}"} for i in range(120)
    ]
    fetch = mocker.patch.object(fin, "fetch_batch", return_value={"status": "013"})

    fin.collect("2025", "11011")

    assert fetch.call_count == 2
    assert len(fetch.call_args_list[0][0][0]) == 100
    assert len(fetch.call_args_list[1][0][0]) == 20


def test_collect_rejects_unknown_report_code(mock_db, mocker):
    mocker.patch.object(fin.config, "DART_API_KEY", "dummy")

    with pytest.raises(ValueError, match="reprt_code"):
        fin.collect("2025", "99999")


def test_collect_skips_cursor_when_a_batch_fails(mock_db, mocker):
    mocker.patch.object(fin.config, "DART_API_KEY", "dummy")
    mock_db.fetchall.return_value = [{"code": "005930", "dart_corp_code": "00126380"}]
    mocker.patch.object(fin, "fetch_batch", side_effect=RuntimeError("DART 오류 800"))

    fin.collect("2025", "11011")

    assert not any("collect_cursor" in str(c[0][0]) for c in mock_db.execute.call_args_list)
