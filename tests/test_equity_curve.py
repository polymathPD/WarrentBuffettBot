"""누적 수익률의 분모 - 계좌에 처음 넣은 원금이어야 한다.

첫 스냅샷의 총자산을 분모로 쓰면 스냅샷을 언제부터 찍었느냐가 수익률을 바꾼다.
2026-08-24(미수 -16,376,832원, 2.6배 레버리지)가 live의 첫 스냅샷이라, 원금 대비
+0.28%인 계좌가 화면에 누적 -1.94%로 찍혔다.
"""
from datetime import date

from dashboard.app import _equity_chart_data
from recorder.equity import initial_capital


def _rows(*values):
    return [{"d": date(2026, 8, 24), "cash": 0, "positions_value": v,
             "total_equity": v} for v in values]


def test_cumulative_return_is_measured_from_the_deposited_capital():
    chart = _equity_chart_data(_rows(10_226_788, 10_027_898), 10_000_000)
    assert chart["cum_pct"] == 0.28


def test_a_contaminated_first_snapshot_does_not_move_the_return():
    """첫날이 미수로 부풀려져 있어도 분모가 원금이면 결과가 같다."""
    clean = _equity_chart_data(_rows(9_900_000, 10_027_898), 10_000_000)
    dirty = _equity_chart_data(_rows(10_226_788, 10_027_898), 10_000_000)
    assert clean["cum_pct"] == dirty["cum_pct"]


def test_a_strategy_handover_does_not_reset_the_denominator():
    """전략이 바뀌어도 원금은 그대로다. 이어붙인 곡선 전체가 한 기준으로 재진다."""
    chart = _equity_chart_data(
        _rows(10_005_653, 10_088_931, 9_954_320, 10_347_306), 10_000_000)
    assert chart["cum_pct"] == 3.47


def test_daily_bars_still_compare_to_the_previous_day():
    """분모를 바꾼 것은 누적뿐이다. 일간 막대는 전일 대비 그대로여야 한다."""
    chart = _equity_chart_data(_rows(10_000_000, 10_100_000), 9_000_000)
    assert "일간 +1.00%" in chart["points"][1]["tip"]
    assert "누적 +12.22%" in chart["points"][1]["tip"]


def test_initial_capital_reads_the_per_mode_setting(mock_db):
    mock_db.fetchone.return_value = {"value": "12345678"}
    assert initial_capital("real") == 12345678.0
    key = mock_db.fetchone.call_args[0][1][0]
    assert key == "INIT_CAPITAL_real"


def test_initial_capital_falls_back_to_the_global_capital(mock_db, mock_settings):
    """모드별 값을 안 넣었으면 전역 CAPITAL을 쓴다."""
    mock_db.fetchone.return_value = None
    assert initial_capital("paper") == 10_000_000.0


def test_the_four_numbers_on_screen_close():
    """원금 + 평가손익 + 실현손익 = 총자산. 화면이 이 항등식으로 닫혀야 한다."""
    from dashboard.app import _realized_pl

    capital, total_equity, unrealized = 10_000_000, 10_027_898, 188_392
    realized = _realized_pl(total_equity, capital, unrealized)

    assert realized == -160_494
    assert capital + unrealized + realized == total_equity


def test_realized_is_not_read_from_the_trade_ledger():
    """live 장부에는 2026-08-24 매수와 그 정리 매도가 빠져 있다. 장부를 합치면
    -161,537이 나오지만 잔고 역산은 -160,494로 맞는다."""
    from dashboard.app import _realized_pl

    assert _realized_pl(10_027_898, 10_000_000, 188_392) == -160_494
