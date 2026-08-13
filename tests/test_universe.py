"""
collector/universe.py — 수집 대상 종목 선정.

시총 기준으로 잡주를 제외하되, 이미 수집한 종목은 시총과 무관하게 유지해야 한다.
FDR 시총은 '현재' 값이라, 과거 대형주였거나 이후 상장폐지된 종목을 소급 제외하면
시계열에 구멍이 생기고 백테스트에 생존 편향이 들어간다.
"""
import pandas as pd
import pytest

from collector import universe

BIG = universe.MIN_MARCAP + 1
SMALL = universe.MIN_MARCAP - 1


@pytest.fixture
def listing(mocker):
    def _set(rows):
        mocker.patch.object(
            universe.fdr,
            "StockListing",
            return_value=pd.DataFrame(rows, columns=["Code", "Marcap"]),
        )
    return _set


def _db(mock_db, in_daily, already):
    mock_db.fetchall.side_effect = [
        [{"code": c} for c in in_daily],
        [{"code": c} for c in already],
    ]


def test_keeps_large_caps_only(mock_db, listing):
    listing([("001", BIG), ("002", SMALL)])
    _db(mock_db, in_daily=["001", "002"], already=[])

    assert universe.target_codes("investor_flow") == ["001"]


def test_already_collected_small_cap_is_kept(mock_db, listing):
    """소급 제외하면 기존 시계열에 구멍이 생기고 생존 편향이 들어간다."""
    listing([("001", BIG), ("002", SMALL)])
    _db(mock_db, in_daily=["001", "002"], already=["002"])

    assert universe.target_codes("investor_flow") == ["001", "002"]


def test_delisted_code_absent_from_listing_is_kept_if_collected(mock_db, listing):
    """상장폐지되어 FDR 목록에 아예 없는 종목도 이미 수집했다면 유지한다."""
    listing([("001", BIG)])
    _db(mock_db, in_daily=["001", "999"], already=["999"])

    assert universe.target_codes("investor_flow") == ["001", "999"]


def test_code_without_daily_bars_is_excluded(mock_db, listing):
    """시총이 커도 stock_daily에 일봉이 없으면 수집 대상이 아니다."""
    listing([("001", BIG), ("003", BIG)])
    _db(mock_db, in_daily=["001"], already=[])

    assert universe.target_codes("investor_flow") == ["001"]


def test_missing_marcap_is_excluded(mock_db, listing):
    listing([("001", BIG), ("002", float("nan")), ("003", None)])
    _db(mock_db, in_daily=["001", "002", "003"], already=[])

    assert universe.target_codes("investor_flow") == ["001"]


def test_source_scopes_the_already_collected_set(mock_db, listing):
    """investor_flow 커서가 credit_balance 유니버스에 섞이면 안 된다."""
    listing([("001", BIG)])
    _db(mock_db, in_daily=["001", "002"], already=[])

    universe.target_codes("credit_balance")

    sql, params = mock_db.fetchall.call_args[0]
    assert "collect_cursor" in sql
    assert params == ("credit_balance",)
