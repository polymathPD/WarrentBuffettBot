"""processor/valuation.py - 시점 매핑, 단위 오류 배제, PBR. DB는 mock."""
from datetime import date

import pytest

from processor import valuation as val


def test_available_period_follows_filing_schedule():
    """Y년 사업보고서는 Y+1년 3월에 나온다 — 그 전에는 전전년도 값만 알 수 있다."""
    assert val.available_period("2026-08-18") == "2025Q4"
    assert val.available_period("2026-04-01") == "2025Q4"
    assert val.available_period("2026-03-31") == "2024Q4"
    assert val.available_period("2022-01-05") == "2020Q4"
    assert val.available_period(date(2023, 6, 1)) == "2022Q4"


def test_reliable_shares_drops_unit_errors(mock_db):
    """한 기간만 1,000배로 튀면 그 종목은 버린다 (LS에코에너지·네오팜 사례)."""
    mock_db.fetchall.return_value = [
        {"code": "092730", "period": "2022Q4", "issued": 8207361},
        {"code": "092730", "period": "2023Q4", "issued": 8207361},
        {"code": "092730", "period": "2024Q4", "issued": 16027989},
        {"code": "092730", "period": "2025Q4", "issued": 16027989000},   # x1000
    ]

    assert val.reliable_shares(["092730"], "2025Q4") == {}


def test_reliable_shares_keeps_consistent_history(mock_db):
    mock_db.fetchall.return_value = [
        {"code": "005930", "period": "2024Q4", "issued": 5919637922},
        {"code": "005930", "period": "2025Q4", "issued": 5846278608},
    ]

    got = val.reliable_shares(["005930"], "2025Q4")

    assert got == {"005930": 5846278608.0}


def test_reliable_shares_allows_normal_dilution(mock_db):
    """증자로 2배가 되는 것은 정상이므로 버리지 않는다."""
    mock_db.fetchall.return_value = [
        {"code": "000001", "period": "2024Q4", "issued": 1_000_000},
        {"code": "000001", "period": "2025Q4", "issued": 2_000_000},
    ]

    assert "000001" in val.reliable_shares(["000001"], "2025Q4")


def test_market_caps_multiplies_close_by_shares(mock_db):
    mock_db.fetchall.side_effect = [
        [{"code": "005930", "period": "2025Q4", "issued": 1000}],
        [{"code": "005930", "c": 70000}],
        [{"code": "005930", "equity": 20_000_000}],   # PBR 3.5 - 정상
    ]

    assert val.market_caps(["005930"], "2026-08-18") == {"005930": 70_000_000.0}


def test_market_caps_drops_codes_whose_shares_contradict_equity(mock_db):
    """중앙값 검사를 통과해도 자본총계 대비 시총이 터무니없으면 뺀다 (조선내화 사례)."""
    mock_db.fetchall.side_effect = [
        [{"code": "462520", "period": "2025Q4", "issued": 11_855_168_000},
         {"code": "462520", "period": "2024Q4", "issued": 11_855_168_000}],
        [{"code": "462520", "c": 13650}],
        [{"code": "462520", "equity": 1_000_000_000_000}],   # PBR 약 162
    ]

    assert val.market_caps(["462520"], "2026-08-18") == {}


def test_pbr_skips_negative_equity(mock_db):
    mock_db.fetchall.side_effect = [
        [{"code": "000001", "period": "2025Q4", "issued": 1000},
         {"code": "000002", "period": "2025Q4", "issued": 1000}],
        [{"code": "000001", "c": 100}, {"code": "000002", "c": 100}],
        [{"code": "000001", "equity": 50000},      # PBR 2.0
         {"code": "000002", "equity": -10000}],    # 자본잠식 -> 제외
        [{"code": "000001", "equity": 50000},
         {"code": "000002", "equity": -10000}],    # pbr()이 다시 조회
    ]

    got = val.pbr(["000001", "000002"], "2026-08-18")

    assert got == {"000001": pytest.approx(2.0)}


def test_empty_input_returns_empty(mock_db):
    assert val.reliable_shares([], "2025Q4") == {}
    mock_db.fetchall.assert_not_called()
