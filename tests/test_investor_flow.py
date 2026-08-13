"""
collector/investor_flow.py — KIS 페이지네이션 / 커서 기록 / 응답 파싱.

전면 재작성(2026-08-12)했는데 테스트가 없던 파일이다. 특히 커서 기록 조건은
조용히 데이터를 영구 결손시키는 버그가 있던 자리라 회귀 테스트로 고정한다.
네트워크와 DB는 전부 mock이다.
"""
from datetime import date, datetime, time, timezone

import pytest

from collector import investor_flow


@pytest.fixture(autouse=True)
def _isolate(mocker):
    """실제 sleep과 .env의 KIS 키에 의존하지 않도록 격리."""
    mocker.patch("collector.investor_flow.time.sleep")
    mocker.patch.object(investor_flow.config, "KIS_APP_KEY", "test-key")
    mocker.patch.object(investor_flow.config, "KIS_APP_SECRET", "test-secret")


def _row(d, ind=1000, frg=-400, org=-600):
    return {
        "stck_bsop_date": d,
        "prsn_ntby_tr_pbmn": str(ind),
        "frgn_ntby_tr_pbmn": str(frg),
        "orgn_ntby_tr_pbmn": str(org),
    }


def _cursor_calls(mock_db):
    return [c for c in mock_db.execute.call_args_list if "collect_cursor" in c[0][0]]


def _one_code(mock_db, mocker):
    mocker.patch("collector.investor_flow.target_codes", return_value=["005930"])
    mock_db.fetchone.return_value = None  # 커서 없음 = 최초 수집


# --- 커서 기록 조건 (핵심 회귀) ---------------------------------------------

def test_cursor_written_when_start_bound_reached(mock_db, mocker):
    _one_code(mock_db, mocker)
    mocker.patch(
        "collector.investor_flow._fetch_page",
        return_value=[_row("20220105"), _row("20220101")],  # oldest == start_bound
    )

    investor_flow.collect(start_date="20220101", end_date="20220105")

    assert len(_cursor_calls(mock_db)) == 1


def test_cursor_written_when_api_runs_out_of_rows(mock_db, mocker):
    """상장폐지/신규상장으로 더 줄 데이터가 없는 경우도 완주로 봐야 한다."""
    _one_code(mock_db, mocker)
    mocker.patch(
        "collector.investor_flow._fetch_page",
        side_effect=[[_row("20220105")], []],
    )

    investor_flow.collect(start_date="20220101", end_date="20220105")

    assert len(_cursor_calls(mock_db)) == 1


def test_cursor_NOT_written_when_page_limit_exhausted(mock_db, mocker, capsys):
    """회귀: 페이지 한도 소진으로 끝났는데 커서를 찍으면, 다음 실행의 start_bound가
    그 날짜로 올라가 못 받은 과거 구간이 영구 결손된다. 커서를 남기면 안 된다."""
    _one_code(mock_db, mocker)
    mocker.patch.object(investor_flow, "MAX_PAGES_PER_CODE", 3)
    mocker.patch(
        "collector.investor_flow._fetch_page",
        side_effect=[[_row("20220110")], [_row("20220109")], [_row("20220108")]],
    )

    investor_flow.collect(start_date="20220101", end_date="20220115")

    assert _cursor_calls(mock_db) == []
    assert "페이지 한도" in capsys.readouterr().out


def test_end_anchor_defaults_to_latest_daily_bar(mock_db, mocker):
    """KIS는 당일 수급을 15:40 이후에만 준다. 오늘 날짜를 앵커로 잡으면 그 전에는
    전 종목이 'TIME LIMIT 00:00 ~ 15:40'으로 거부되므로, 일봉이 있는 마지막
    날짜를 앵커로 써야 한다."""
    mocker.patch("collector.investor_flow.target_codes", return_value=["005930"])
    mock_db.fetchone.side_effect = [
        {"d": date(2026, 8, 12)},  # _latest_available_date: stock_daily 최신일
        None,                      # _last_collected: 커서 없음
    ]
    fetch = mocker.patch(
        "collector.investor_flow._fetch_page", return_value=[_row("20220101")]
    )

    investor_flow.collect(start_date="20220101")

    assert fetch.call_args[0][1] == "20260812"


def test_stalled_anchor_stops_instead_of_refetching(mock_db, mocker):
    """페이지의 최과거일이 앵커와 같으면 다음 요청이 같은 응답을 받는다.
    가드가 없으면 동일 요청을 MAX_PAGES_PER_CODE만큼 반복한다."""
    _one_code(mock_db, mocker)
    mocker.patch.object(investor_flow, "MAX_PAGES_PER_CODE", 50)
    fetch = mocker.patch(
        "collector.investor_flow._fetch_page",
        return_value=[_row("20220110")],  # 항상 같은 날짜만 반환
    )

    investor_flow.collect(start_date="20220101", end_date="20220110")

    assert fetch.call_count == 1
    assert len(_cursor_calls(mock_db)) == 1


def test_already_collected_today_is_skipped_without_api_call(mock_db, mocker):
    mocker.patch("collector.investor_flow.target_codes", return_value=["005930"])
    mock_db.fetchone.return_value = {"last_seen": datetime.now(timezone.utc)}
    fetch = mocker.patch("collector.investor_flow._fetch_page")

    investor_flow.collect(start_date="20220101", end_date="20260813")

    fetch.assert_not_called()


def test_utc_cursor_is_compared_in_local_time(mock_db, mocker):
    """커서는 Postgres NOW()(UTC)로 저장된다. KST 00~09시에는 UTC 날짜가 하루
    뒤처지므로, 로컬 변환 없이 date.today()와 비교하면 오늘 끝낸 종목을 다시 받는다."""
    mocker.patch("collector.investor_flow.target_codes", return_value=["005930"])
    # 로컬 기준 오늘 08:00에 수집 완료 -> UTC로는 어제 23:00일 수 있다
    local_today_morning = datetime.combine(
        date.today(), time(8, 0), tzinfo=datetime.now().astimezone().tzinfo
    )
    mock_db.fetchone.return_value = {"last_seen": local_today_morning.astimezone(timezone.utc)}
    fetch = mocker.patch("collector.investor_flow._fetch_page")

    investor_flow.collect(start_date="20220101", end_date="20260813")

    fetch.assert_not_called()


# --- 행 파싱 ----------------------------------------------------------------

def test_rows_older_than_start_bound_are_not_inserted(mock_db, mocker):
    _one_code(mock_db, mocker)
    mocker.patch(
        "collector.investor_flow._fetch_page",
        return_value=[_row("20220105"), _row("20211230")],  # 뒤엣것은 범위 밖
    )

    investor_flow.collect(start_date="20220101", end_date="20220105")

    rows = mock_db.executemany.call_args[0][1]
    assert rows == [("005930", date(2022, 1, 5), 1000, -400, -600)]


def test_malformed_dates_are_skipped(mock_db, mocker):
    _one_code(mock_db, mocker)
    mocker.patch(
        "collector.investor_flow._fetch_page",
        return_value=[_row(""), _row("2022010"), _row("20220101")],
    )

    investor_flow.collect(start_date="20220101", end_date="20220105")

    rows = mock_db.executemany.call_args[0][1]
    assert [r[1] for r in rows] == [date(2022, 1, 1)]


def test_missing_net_fields_default_to_zero(mock_db, mocker):
    _one_code(mock_db, mocker)
    mocker.patch(
        "collector.investor_flow._fetch_page",
        return_value=[{"stck_bsop_date": "20220101"}],
    )

    investor_flow.collect(start_date="20220101", end_date="20220105")

    assert mock_db.executemany.call_args[0][1] == [("005930", date(2022, 1, 1), 0, 0, 0)]


# --- 종목별 실패 격리 --------------------------------------------------------

def test_one_failing_code_does_not_abort_the_sweep(mock_db, mocker, capsys):
    mocker.patch(
        "collector.investor_flow.target_codes", return_value=["000001", "000002"]
    )
    mock_db.fetchone.return_value = None
    mocker.patch(
        "collector.investor_flow._fetch_page",
        side_effect=[RuntimeError("초당 거래건수 초과"), [_row("20220101")]],
    )

    investor_flow.collect(start_date="20220101", end_date="20220105")

    out = capsys.readouterr().out
    assert "초당 거래건수 초과" in out
    # 실패한 종목엔 커서가 없어야 하고, 성공한 종목만 커서가 남는다
    assert [c[0][1][1] for c in _cursor_calls(mock_db)] == ["000002"]


# --- _fetch_page ------------------------------------------------------------

def test_fetch_page_returns_output2(mocker):
    mocker.patch("collector.investor_flow._get_token", return_value="tok")
    resp = mocker.MagicMock(status_code=200)
    resp.json.return_value = {"rt_cd": "0", "output2": [_row("20220103")]}
    mocker.patch("collector.investor_flow.requests.get", return_value=resp)

    assert investor_flow._fetch_page("005930", "20220103") == [_row("20220103")]


def test_fetch_page_retries_transient_500(mocker):
    """종목당 수십 페이지를 순차 조회하므로 페이지 하나의 일시 오류로 종목 전체가
    중단되면 종목 실패율이 급증한다. 5xx는 페이지 단위로 재시도해야 한다."""
    mocker.patch("collector.investor_flow._get_token", return_value="tok")
    mocker.patch("collector.investor_flow.time.sleep")
    bad = mocker.MagicMock(status_code=500)
    good = mocker.MagicMock(status_code=200)
    good.json.return_value = {"rt_cd": "0", "output2": [_row("20220103")]}
    get = mocker.patch(
        "collector.investor_flow.requests.get", side_effect=[bad, bad, good]
    )

    assert investor_flow._fetch_page("005930", "20220103") == [_row("20220103")]
    assert get.call_count == 3


def test_fetch_page_gives_up_after_max_retries(mocker):
    mocker.patch("collector.investor_flow._get_token", return_value="tok")
    mocker.patch("collector.investor_flow.time.sleep")
    bad = mocker.MagicMock(status_code=500)
    bad.raise_for_status.side_effect = RuntimeError("500 Server Error")
    get = mocker.patch("collector.investor_flow.requests.get", return_value=bad)

    with pytest.raises(RuntimeError, match="500"):
        investor_flow._fetch_page("005930", "20220103")
    assert get.call_count == investor_flow.MAX_RETRIES


def test_fetch_page_does_not_retry_business_errors(mocker):
    """rt_cd 오류는 재조회해도 같은 결과라 재시도하면 호출만 낭비한다."""
    mocker.patch("collector.investor_flow._get_token", return_value="tok")
    mocker.patch("collector.investor_flow.time.sleep")
    resp = mocker.MagicMock(status_code=200)
    resp.json.return_value = {"rt_cd": "1", "msg1": "TIME LIMIT 00:00 ~ 15:40"}
    get = mocker.patch("collector.investor_flow.requests.get", return_value=resp)

    with pytest.raises(RuntimeError, match="TIME LIMIT"):
        investor_flow._fetch_page("005930", "20220103")
    assert get.call_count == 1


def test_fetch_page_raises_on_error_rt_cd(mocker):
    mocker.patch("collector.investor_flow._get_token", return_value="tok")
    resp = mocker.MagicMock(status_code=200)
    resp.json.return_value = {"rt_cd": "1", "msg1": "초당 거래건수를 초과하였습니다"}
    mocker.patch("collector.investor_flow.requests.get", return_value=resp)

    with pytest.raises(RuntimeError, match="초당 거래건수"):
        investor_flow._fetch_page("005930", "20220103")


def test_collect_aborts_without_api_keys(mock_db, mocker):
    mocker.patch.object(investor_flow.config, "KIS_APP_KEY", "")
    fetch = mocker.patch("collector.investor_flow._fetch_page")
    universe = mocker.patch("collector.investor_flow.target_codes")

    investor_flow.collect(start_date="20220101")

    fetch.assert_not_called()
    universe.assert_not_called()  # 유니버스 조회(FDR)도 하지 않아야 함
