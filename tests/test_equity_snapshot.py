"""장중 스냅샷이 전일 종가로 오늘 행을 만들면 안 된다.

open_job은 09:05·13:10에 snapshot()을 부르는데 그 시각에는 오늘 일봉이 없다.
종목별 폴백(거래정지 대비)이 시장 전체 미수집에도 걸리면 전일 값이 오늘 것으로
복제되고, 16:10 배치가 덮어쓰기 전에 죽으면 그대로 굳는다 — 2026-08-25 paper
스냅샷이 08-24와 평가금액까지 똑같았다.
"""
import recorder.equity as eq


def _no_bars(sql, params=None):
    return None


def _has_bars(sql, params=None):
    return {"ok": 1} if "FROM stock_daily" in sql else None


def _rows(values):
    """settings 조회(_overrides)와 보유 평가 조회를 구분해 돌려준다."""
    def _f(sql, params=None):
        return [] if "FROM settings" in sql else values
    return _f


def test_skips_priced_modes_when_the_day_has_no_bars(mock_db, mock_settings, mocker):
    mock_db.fetchone.side_effect = _no_bars
    mock_db.fetchall.side_effect = _rows([])
    mocker.patch.object(eq, "cash_by_key", return_value={("paper", "quality_v1"): 500.0})

    assert eq.snapshot("2026-08-25") == 0
    mock_db.executemany.assert_not_called()


def test_records_priced_modes_once_the_bars_are_in(mock_db, mock_settings, mocker):
    mock_db.fetchone.side_effect = _has_bars
    mock_db.fetchall.side_effect = _rows(
        [{"mode": "paper", "strategy": "quality_v1", "v": 9_929_300}])
    mocker.patch.object(eq, "cash_by_key",
                        return_value={("paper", "quality_v1"): 332_796.0})

    assert eq.snapshot("2026-08-25") == 1
    rows = mock_db.executemany.call_args[0][1]
    assert rows[0][:3] == ("2026-08-25", "paper", "quality_v1")
    assert rows[0][5] == 332_796.0 + 9_929_300


def test_live_is_recorded_even_without_bars(mock_db, mock_settings, mocker):
    """live는 KIS 순자산이라 일봉과 무관하다."""
    mock_db.fetchone.side_effect = _no_bars
    mock_db.fetchall.side_effect = _rows([])
    mocker.patch.object(eq, "cash_by_key", return_value={("live", "quality_v1"): 0.0})
    mocker.patch("executor.live.account_snapshot", return_value={
        "total_equity": 10_027_898, "positions_value": 9_800_270, "unrealized": 188_392})

    assert eq.snapshot("2026-08-25") == 1
    rows = mock_db.executemany.call_args[0][1]
    assert rows[0][1] == "live"
    assert rows[0][5] == 10_027_898
