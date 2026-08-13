"""
collector/credit_balance.py — 아직 전체 스윕이 한 번도 실행된 적 없는 수집기
(credit_balance 테이블 0행)라, 실제로 돌리기 전에 mock으로 경로를 검증한다.
investor_flow와 페이지네이션/커서 구조가 동일하므로 회귀 지점도 같다.
"""
from datetime import date

import pytest

from collector import credit_balance


@pytest.fixture(autouse=True)
def _isolate(mocker):
    mocker.patch("collector.credit_balance.time.sleep")
    mocker.patch.object(credit_balance.config, "KIS_APP_KEY", "test-key")
    mocker.patch.object(credit_balance.config, "KIS_APP_SECRET", "test-secret")


def _row(d, amt=5_000_000, ratio=1.23):
    return {
        "stlm_date": d,
        "whol_loan_rmnd_amt": str(amt),
        "whol_loan_rmnd_rate": str(ratio),
    }


def _cursor_calls(mock_db):
    return [c for c in mock_db.execute.call_args_list if "collect_cursor" in c[0][0]]


def _one_code(mock_db, mocker):
    mocker.patch("collector.credit_balance.target_codes", return_value=["005930"])
    mock_db.fetchone.return_value = None


def test_cursor_written_when_start_bound_reached(mock_db, mocker):
    _one_code(mock_db, mocker)
    mocker.patch(
        "collector.credit_balance._fetch_page",
        return_value=[_row("20220105"), _row("20220101")],
    )

    credit_balance.collect_kis(start_date="20220101", end_date="20220105")

    assert len(_cursor_calls(mock_db)) == 1


def test_cursor_NOT_written_when_page_limit_exhausted(mock_db, mocker, capsys):
    """investor_flow와 동일한 영구-결손 회귀."""
    _one_code(mock_db, mocker)
    mocker.patch.object(credit_balance, "MAX_PAGES_PER_CODE", 3)
    mocker.patch(
        "collector.credit_balance._fetch_page",
        side_effect=[[_row("20220110")], [_row("20220109")], [_row("20220108")]],
    )

    credit_balance.collect_kis(start_date="20220101", end_date="20220115")

    assert _cursor_calls(mock_db) == []
    assert "페이지 한도" in capsys.readouterr().out


def test_rows_are_parsed_into_amt_and_ratio(mock_db, mocker):
    _one_code(mock_db, mocker)
    mocker.patch(
        "collector.credit_balance._fetch_page",
        return_value=[_row("20220101", amt=7_500_000, ratio=2.5)],
    )

    credit_balance.collect_kis(start_date="20220101", end_date="20220105")

    assert mock_db.executemany.call_args[0][1] == [
        ("005930", date(2022, 1, 1), 7_500_000, 2.5)
    ]


def test_fetch_page_returns_output(mocker):
    mocker.patch("collector.credit_balance._get_token", return_value="tok")
    resp = mocker.MagicMock(status_code=200)
    resp.json.return_value = {"rt_cd": "0", "output": [_row("20220103")]}
    mocker.patch("collector.credit_balance.requests.get", return_value=resp)

    assert credit_balance._fetch_page("005930", "20220103") == [_row("20220103")]


def test_fetch_page_raises_on_error_rt_cd(mocker):
    mocker.patch("collector.credit_balance._get_token", return_value="tok")
    resp = mocker.MagicMock(status_code=200)
    resp.json.return_value = {"rt_cd": "1", "msg1": "조회할 자료가 없습니다"}
    mocker.patch("collector.credit_balance.requests.get", return_value=resp)

    with pytest.raises(RuntimeError, match="조회할 자료가 없습니다"):
        credit_balance._fetch_page("005930", "20220103")


def test_falls_back_to_stub_without_api_keys(mock_db, mocker):
    mocker.patch.object(credit_balance.config, "KIS_APP_KEY", "")
    stub = mocker.patch("collector.credit_balance.collect_stub")
    fetch = mocker.patch("collector.credit_balance._fetch_page")
    universe = mocker.patch("collector.credit_balance.target_codes")

    credit_balance.collect_kis(start_date="20220101", end_date="20220105")

    stub.assert_called_once_with("20220101", "20220105")
    fetch.assert_not_called()
    universe.assert_not_called()
