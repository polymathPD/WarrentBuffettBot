"""
collector/investor_flow.py — KIS 페이지네이션 / 커서 기록 / 응답 파싱.

페이지네이션과 커서 기록은 collector/base.py의 KisDailyCollector가 갖고, 이 파일은
그 흐름을 investor_flow 설정으로 확인한다. 커서 기록 조건은 조용히 데이터를 영구
결손시키는 버그가 있던 자리라 회귀 테스트로 고정한다. 네트워크와 DB는 전부 mock이다.
"""
from datetime import date

import pytest

import config
from collector.investor_flow import InvestorFlowCollector, PBMN_TO_WON


@pytest.fixture(autouse=True)
def _isolate(mocker):
    """실제 sleep과 .env의 KIS 키에 의존하지 않도록 격리."""
    mocker.patch("collector.base.time.sleep")
    mocker.patch.object(config, "KIS_APP_KEY", "test-key")
    mocker.patch.object(config, "KIS_APP_SECRET", "test-secret")


def _row(d, ind=1000, frg=-400, org=-600):
    """KIS 응답 한 건. *_ntby_tr_pbmn은 백만원 단위다 (수집기가 원으로 환산한다)."""
    return {
        "stck_bsop_date": d,
        "prsn_ntby_tr_pbmn": str(ind),
        "frgn_ntby_tr_pbmn": str(frg),
        "orgn_ntby_tr_pbmn": str(org),
    }


def _cursor_calls(mock_db):
    return [c for c in mock_db.execute.call_args_list if "collect_cursor" in c[0][0]]


def _codes(mocker, codes):
    return mocker.patch("collector.universe.target_codes", return_value=codes)


def _one_code(mock_db, mocker):
    _codes(mocker, ["005930"])
    mock_db.fetchone.return_value = None  # 커서 없음 = 최초 수집


def _pages(mocker, **kwargs):
    return mocker.patch.object(InvestorFlowCollector, "fetch_page", **kwargs)


# --- 커서 기록 조건 (핵심 회귀) ---------------------------------------------

def test_cursor_written_when_start_bound_reached(mock_db, mocker):
    _one_code(mock_db, mocker)
    _pages(mocker, return_value=[_row("20220105"), _row("20220101")])  # oldest == start_bound

    InvestorFlowCollector("20220101", "20220105").run()

    assert len(_cursor_calls(mock_db)) == 1


def test_cursor_written_when_api_runs_out_of_rows(mock_db, mocker):
    """상장폐지/신규상장으로 더 줄 데이터가 없는 경우도 완주로 봐야 한다."""
    _one_code(mock_db, mocker)
    _pages(mocker, side_effect=[[_row("20220105")], []])

    InvestorFlowCollector("20220101", "20220105").run()

    assert len(_cursor_calls(mock_db)) == 1


def test_cursor_NOT_written_when_page_limit_exhausted(mock_db, mocker, capsys):
    """회귀: 페이지 한도 소진으로 끝났는데 커서를 찍으면, 다음 실행의 start_bound가
    그 날짜로 올라가 못 받은 과거 구간이 영구 결손된다. 커서를 남기면 안 된다."""
    _one_code(mock_db, mocker)
    mocker.patch.object(InvestorFlowCollector, "MAX_PAGES_PER_CODE", 3)
    _pages(mocker, side_effect=[[_row("20220110")], [_row("20220109")], [_row("20220108")]])

    InvestorFlowCollector("20220101", "20220115").run()

    assert _cursor_calls(mock_db) == []
    assert "페이지 한도" in capsys.readouterr().out


def test_end_anchor_defaults_to_latest_daily_bar(mock_db, mocker):
    """KIS는 당일 수급을 15:40 이후에만 준다. 오늘 날짜를 앵커로 잡으면 그 전에는
    전 종목이 'TIME LIMIT 00:00 ~ 15:40'으로 거부되므로, 일봉이 있는 마지막
    날짜를 앵커로 써야 한다."""
    _codes(mocker, ["005930"])
    mock_db.fetchone.side_effect = [
        {"d": date(2026, 8, 12)},  # latest_available_date: stock_daily 최신일
        {"d": None},               # last_data_date: 이 종목 데이터 없음
    ]
    fetch = _pages(mocker, return_value=[_row("20220101")])

    InvestorFlowCollector("20220101").run()

    assert fetch.call_args[0][1] == "20260812"


def test_stalled_anchor_stops_instead_of_refetching(mock_db, mocker):
    """페이지의 최과거일이 앵커와 같으면 다음 요청이 같은 응답을 받는다.
    가드가 없으면 동일 요청을 MAX_PAGES_PER_CODE만큼 반복한다."""
    _one_code(mock_db, mocker)
    mocker.patch.object(InvestorFlowCollector, "MAX_PAGES_PER_CODE", 50)
    fetch = _pages(mocker, return_value=[_row("20220110")])  # 항상 같은 날짜만 반환

    InvestorFlowCollector("20220101", "20220110").run()

    assert fetch.call_count == 1
    assert len(_cursor_calls(mock_db)) == 1


def test_skipped_when_data_already_reaches_target_date(mock_db, mocker):
    """목표일까지 이미 받아 뒀으면 API를 부르지 않는다."""
    _codes(mocker, ["005930"])
    mock_db.fetchone.return_value = {"d": date(2026, 8, 13)}
    fetch = _pages(mocker)

    InvestorFlowCollector("20220101", "20260813").run()

    fetch.assert_not_called()


def test_collection_running_past_midnight_does_not_skip_next_day(mock_db, mocker):
    """회귀: 증분 기준을 '수집 시각'으로 잡으면 안 된다.

    전 종목 수집이 13시간 걸려 자정을 넘겨 끝나면 커서(UTC NOW)의 로컬 날짜가
    다음 날이 된다. 그걸로 '오늘 이미 했다'를 판정하면 다음 실행이 전 종목을
    건너뛰고, 실제로 하루치 수급이 통째로 비었다. 기준은 데이터 최신일이다."""
    _codes(mocker, ["005930"])
    # 데이터는 08-18까지만 있다 (커서 시각이 무엇이든 08-19는 받아야 한다)
    mock_db.fetchone.return_value = {"d": date(2026, 8, 18)}
    fetch = _pages(mocker, return_value=[_row("20260819")])

    InvestorFlowCollector("20220101", "20260819").run()

    assert fetch.called, "데이터가 목표일에 못 미치면 반드시 수집해야 한다"
    assert mock_db.executemany.called


# --- 행 파싱 ----------------------------------------------------------------

def test_rows_older_than_start_bound_are_not_inserted(mock_db, mocker):
    _one_code(mock_db, mocker)
    _pages(mocker, return_value=[_row("20220105"), _row("20211230")])  # 뒤엣것은 범위 밖

    InvestorFlowCollector("20220101", "20220105").run()

    rows = mock_db.executemany.call_args[0][1]
    M = PBMN_TO_WON
    assert rows == [("005930", date(2022, 1, 5), 1000 * M, -400 * M, -600 * M)]


def test_amounts_are_stored_in_won_not_millions(mock_db, mocker):
    """KIS는 백만원으로 준다. 원으로 환산하지 않으면 참조 DB에서 이관한 원 단위
    데이터와 10^6배 어긋나고, flow_ratio의 30일 창이 두 단위를 물어 heat_score가
    전 종목 0으로 죽는다."""
    _one_code(mock_db, mocker)
    _pages(mocker, return_value=[_row("20220105", ind=946, frg=-730, org=-216)])

    InvestorFlowCollector("20220101", "20220105").run()

    _, d, ind, frg, org = mock_db.executemany.call_args[0][1][0]
    assert (ind, frg, org) == (946_000_000, -730_000_000, -216_000_000)


def test_recollection_overwrites_existing_rows(mock_db, mocker):
    """DO NOTHING이면 단위가 틀린 행을 재수집으로 고칠 수 없다 — 실제로 그래서
    수집기를 다시 돌려도 데이터가 복구되지 않았다."""
    _one_code(mock_db, mocker)
    _pages(mocker, return_value=[_row("20220105")])

    InvestorFlowCollector("20220101", "20220105").run()

    sql = mock_db.executemany.call_args[0][0]
    assert "DO UPDATE" in sql and "DO NOTHING" not in sql


def test_malformed_dates_are_skipped(mock_db, mocker):
    _one_code(mock_db, mocker)
    _pages(mocker, return_value=[_row(""), _row("2022010"), _row("20220101")])

    InvestorFlowCollector("20220101", "20220105").run()

    rows = mock_db.executemany.call_args[0][1]
    assert [r[1] for r in rows] == [date(2022, 1, 1)]


def test_missing_net_fields_default_to_zero(mock_db, mocker):
    _one_code(mock_db, mocker)
    _pages(mocker, return_value=[{"stck_bsop_date": "20220101"}])

    InvestorFlowCollector("20220101", "20220105").run()

    assert mock_db.executemany.call_args[0][1] == [("005930", date(2022, 1, 1), 0, 0, 0)]


# --- 종목별 실패 격리 --------------------------------------------------------

def test_one_failing_code_does_not_abort_the_sweep(mock_db, mocker, capsys):
    _codes(mocker, ["000001", "000002"])
    mock_db.fetchone.return_value = None
    _pages(mocker, side_effect=[RuntimeError("초당 거래건수 초과"), [_row("20220101")]])

    InvestorFlowCollector("20220101", "20220105").run()

    out = capsys.readouterr().out
    assert "초당 거래건수 초과" in out
    # 실패한 종목엔 커서가 없어야 하고, 성공한 종목만 커서가 남는다
    assert [c[0][1][1] for c in _cursor_calls(mock_db)] == ["000002"]


# --- fetch_page -------------------------------------------------------------

def _http(mocker, **kwargs):
    mocker.patch("executor.live._get_token", return_value="tok")
    return mocker.patch("collector.base.requests.get", **kwargs)


def test_fetch_page_returns_output2(mocker):
    resp = mocker.MagicMock(status_code=200)
    resp.json.return_value = {"rt_cd": "0", "output2": [_row("20220103")]}
    _http(mocker, return_value=resp)

    assert InvestorFlowCollector().fetch_page("005930", "20220103") == [_row("20220103")]


def test_fetch_page_retries_transient_500(mocker):
    """종목당 수십 페이지를 순차 조회하므로 페이지 하나의 일시 오류로 종목 전체가
    중단되면 종목 실패율이 급증한다. 5xx는 페이지 단위로 재시도해야 한다."""
    bad = mocker.MagicMock(status_code=500)
    good = mocker.MagicMock(status_code=200)
    good.json.return_value = {"rt_cd": "0", "output2": [_row("20220103")]}
    get = _http(mocker, side_effect=[bad, bad, good])

    assert InvestorFlowCollector().fetch_page("005930", "20220103") == [_row("20220103")]
    assert get.call_count == 3


def test_fetch_page_gives_up_after_max_retries(mocker):
    bad = mocker.MagicMock(status_code=500)
    bad.raise_for_status.side_effect = RuntimeError("500 Server Error")
    get = _http(mocker, return_value=bad)

    with pytest.raises(RuntimeError, match="500"):
        InvestorFlowCollector().fetch_page("005930", "20220103")
    assert get.call_count == InvestorFlowCollector.MAX_RETRIES


def test_fetch_page_does_not_retry_business_errors(mocker):
    """rt_cd 오류는 재조회해도 같은 결과라 재시도하면 호출만 낭비한다."""
    resp = mocker.MagicMock(status_code=200)
    resp.json.return_value = {"rt_cd": "1", "msg1": "TIME LIMIT 00:00 ~ 15:40"}
    get = _http(mocker, return_value=resp)

    with pytest.raises(RuntimeError, match="TIME LIMIT"):
        InvestorFlowCollector().fetch_page("005930", "20220103")
    assert get.call_count == 1


def test_fetch_page_raises_on_error_rt_cd(mocker):
    resp = mocker.MagicMock(status_code=200)
    resp.json.return_value = {"rt_cd": "1", "msg1": "초당 거래건수를 초과하였습니다"}
    _http(mocker, return_value=resp)

    with pytest.raises(RuntimeError, match="초당 거래건수"):
        InvestorFlowCollector().fetch_page("005930", "20220103")


def test_collect_aborts_without_api_keys(mock_db, mocker):
    mocker.patch.object(config, "KIS_APP_KEY", "")
    fetch = _pages(mocker)
    universe = _codes(mocker, ["005930"])

    InvestorFlowCollector("20220101").run()

    fetch.assert_not_called()
    universe.assert_not_called()  # 유니버스 조회(FDR)도 하지 않아야 함
