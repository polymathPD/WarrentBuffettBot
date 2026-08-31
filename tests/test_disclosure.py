"""collector/disclosure.py - 월 분할, 응답 파싱, 페이지 종료 조건. 네트워크/DB는 mock."""
from datetime import date

import config
from collector import base
from collector.disclosure import DisclosureCollector, _month_ranges


def test_month_ranges_splits_on_calendar_months():
    got = list(_month_ranges(date(2026, 1, 15), date(2026, 3, 3)))

    assert got == [
        (date(2026, 1, 15), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 3)),
    ]


def test_month_ranges_single_day():
    assert list(_month_ranges(date(2026, 8, 18), date(2026, 8, 18))) == [
        (date(2026, 8, 18), date(2026, 8, 18))
    ]


def test_rows_keeps_known_codes_and_builds_url():
    data = {"list": [
        {"stock_code": "005930", "rcept_no": "20260818000001",
         "rcept_dt": "20260818", "report_nm": "주요사항보고서"},
        {"stock_code": "999999", "rcept_no": "20260818000002",
         "rcept_dt": "20260818", "report_nm": "우리가 안 보는 종목"},
        {"stock_code": "", "rcept_no": "20260818000003",
         "rcept_dt": "20260818", "report_nm": "비상장"},
    ]}

    rows = DisclosureCollector().rows(data, {"005930"})

    assert rows == [(
        "20260818000001", "005930", "2026-08-18", "주요사항보고서",
        "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260818000001",
    )]


def _one_day(mock_db, mocker):
    mocker.patch.object(config, "DART_API_KEY", "dummy")
    mock_db.fetchall.return_value = [{"code": "005930"}]
    mock_db.fetchone.return_value = None
    return DisclosureCollector("2026-08-18", "2026-08-18")


def test_collect_stops_at_last_page(mock_db, mocker):
    """total_page에 도달하면 다음 페이지를 부르지 않는다 (시장 2개 × 1페이지)."""
    col = _one_day(mock_db, mocker)
    fetch = mocker.patch.object(DisclosureCollector, "fetch_page", return_value={
        "status": "000", "total_page": 1,
        "list": [{"stock_code": "005930", "rcept_no": "1",
                  "rcept_dt": "20260818", "report_nm": "보고서"}],
    })

    saved = col.run()

    assert saved == 2                 # 유가증권 + 코스닥 각 1건
    assert fetch.call_count == 2      # 시장당 1페이지씩만


def test_collect_skips_cursor_update_on_failure(mock_db, mocker):
    """실패가 있으면 커서를 올리지 않아 다음 실행이 같은 구간을 다시 받는다."""
    col = _one_day(mock_db, mocker)
    mocker.patch.object(DisclosureCollector, "fetch_page",
                        side_effect=RuntimeError("DART 오류 800"))

    saved = col.run()

    assert saved == 0
    assert not any("collect_cursor" in str(c[0][0]) for c in mock_db.execute.call_args_list)


def test_collect_treats_no_data_status_as_empty(mock_db, mocker):
    """status 013(데이터 없음)은 오류가 아니라 빈 구간이다."""
    col = _one_day(mock_db, mocker)
    mocker.patch.object(DisclosureCollector, "fetch_page",
                        return_value={"status": base.DART_NO_DATA})

    saved = col.run()

    assert saved == 0
    assert any("collect_cursor" in str(c[0][0]) for c in mock_db.execute.call_args_list)


def test_collect_without_api_key_is_a_noop(mock_db, mocker):
    mocker.patch.object(config, "DART_API_KEY", "")

    assert DisclosureCollector("2026-08-18", "2026-08-18").run() == 0
    mock_db.executemany.assert_not_called()
