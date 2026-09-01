"""
collector/credit_balance.py — 아직 전체 스윕이 한 번도 실행된 적 없는 수집기
(credit_balance 테이블 0행)라, 실제로 돌리기 전에 mock으로 경로를 검증한다.
investor_flow와 KisDailyCollector를 공유하므로 회귀 지점도 같다.
"""
from datetime import date

import pytest

import config
from collector.credit_balance import CreditBalanceCollector


@pytest.fixture(autouse=True)
def _isolate(mocker):
    mocker.patch("collector.base.time.sleep")
    mocker.patch.object(config, "KIS_APP_KEY", "test-key")
    mocker.patch.object(config, "KIS_APP_SECRET", "test-secret")


def _row(d, amt=5_000_000, ratio=1.23):
    return {
        "stlm_date": d,
        "whol_loan_rmnd_amt": str(amt),
        "whol_loan_rmnd_rate": str(ratio),
    }


def _cursor_calls(mock_db):
    return [c for c in mock_db.execute.call_args_list if "collect_cursor" in c[0][0]]


def _codes(mocker, codes):
    return mocker.patch("collector.universe.target_codes", return_value=codes)


def _one_code(mock_db, mocker):
    _codes(mocker, ["005930"])
    mock_db.fetchone.return_value = None


def _pages(mocker, **kwargs):
    return mocker.patch.object(CreditBalanceCollector, "fetch_page", **kwargs)


def test_cursor_written_when_start_bound_reached(mock_db, mocker):
    _one_code(mock_db, mocker)
    _pages(mocker, return_value=[_row("20220105"), _row("20220101")])

    CreditBalanceCollector("20220101", "20220105").run()

    assert len(_cursor_calls(mock_db)) == 1


def test_cursor_NOT_written_when_page_limit_exhausted(mock_db, mocker, capsys):
    """investor_flow와 동일한 영구-결손 회귀."""
    _one_code(mock_db, mocker)
    mocker.patch.object(CreditBalanceCollector, "MAX_PAGES_PER_CODE", 3)
    _pages(mocker, side_effect=[[_row("20220110")], [_row("20220109")], [_row("20220108")]])

    CreditBalanceCollector("20220101", "20220115").run()

    assert _cursor_calls(mock_db) == []
    assert "페이지 한도" in capsys.readouterr().out


def test_rows_are_parsed_into_amt_and_ratio(mock_db, mocker):
    _one_code(mock_db, mocker)
    _pages(mocker, return_value=[_row("20220101", amt=7_500_000, ratio=2.5)])

    CreditBalanceCollector("20220101", "20220105").run()

    assert mock_db.executemany.call_args[0][1] == [
        ("005930", date(2022, 1, 1), 7_500_000, 2.5)
    ]


def _http(mocker, **kwargs):
    mocker.patch("executor.live._get_token", return_value="tok")
    return mocker.patch("collector.base.requests.get", **kwargs)


def test_fetch_page_returns_output(mocker):
    resp = mocker.MagicMock(status_code=200)
    resp.json.return_value = {"rt_cd": "0", "output": [_row("20220103")]}
    _http(mocker, return_value=resp)

    assert CreditBalanceCollector().fetch_page("005930", "20220103") == [_row("20220103")]


def test_fetch_page_raises_on_error_rt_cd(mocker):
    resp = mocker.MagicMock(status_code=200)
    resp.json.return_value = {"rt_cd": "1", "msg1": "조회할 자료가 없습니다"}
    _http(mocker, return_value=resp)

    with pytest.raises(RuntimeError, match="조회할 자료가 없습니다"):
        CreditBalanceCollector().fetch_page("005930", "20220103")


def test_falls_back_to_stub_without_api_keys(mock_db, mocker):
    mocker.patch.object(config, "KIS_APP_KEY", "")
    stub = mocker.patch.object(CreditBalanceCollector, "collect_stub")
    fetch = _pages(mocker)
    universe = _codes(mocker, ["005930"])

    CreditBalanceCollector("20220101", "20220105").run()

    stub.assert_called_once_with()
    fetch.assert_not_called()
    universe.assert_not_called()


def test_stub_fills_the_requested_window(mock_db, mocker):
    """stub은 생성자에 준 기간을 그대로 쓴다 (YYYYMMDD -> date 리터럴 변환)."""
    mock_db.fetchall.return_value = [{"code": "005930", "d": date(2022, 1, 3)}]

    CreditBalanceCollector("20220101", "20220105").collect_stub()

    assert mock_db.fetchall.call_args[0][1] == ("2022-01-01", "2022-01-05")
    assert mock_db.executemany.call_args[0][1] == [("005930", date(2022, 1, 3), 0, 0.0)]


def test_rate_limit_waits_longer_than_generic_5xx(mocker):
    """KIS 초당 한도는 HTTP 500으로 온다. 일반 5xx와 같은 간격으로 재시도하면
    재시도가 다시 한도를 먹는다."""
    mocker.patch.object(CreditBalanceCollector, "headers", return_value={})
    limited = mocker.MagicMock(status_code=500, text="초당 거래건수를 초과하였습니다")
    ok = mocker.MagicMock(status_code=200)
    ok.json.return_value = {"rt_cd": "0", "output": []}
    mocker.patch("collector.base.requests.get", side_effect=[limited, ok])
    sleep = mocker.patch("collector.base.time.sleep")

    CreditBalanceCollector().fetch_page("005930", "20260818")

    assert sleep.call_args[0][0] >= CreditBalanceCollector.SLEEP_SEC * 3
