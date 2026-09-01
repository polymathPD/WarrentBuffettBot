"""
executor.live.record_today_trades() — 잔고와 장부의 차이를 그날 매매로 기록한다.

2026-09-01에 KG케미칼 225주가 실제로 체결됐는데 adjust()의 45초 폴링이 못 봐서
trades에 한 줄도 남지 않았다. 발주 직후에 확인하는 대신 장 마감 뒤 잔고를 읽는다.

증권사 날짜 필드(thdt_*)에 기대지 않는 이유: 장 마감 뒤 어느 시점에 bfdy_* 로
넘어가는데 그 경계 시각을 모른다(같은 날 21:30에 thdt_buyqty=0, bfdy_buy_qty=225).
"""
import pytest

from executor import live


@pytest.fixture(autouse=True)
def _mode(mocker):
    mocker.patch.object(live, "MODE", "live")


def _snap(holdings):
    return {"holdings": holdings, "cash": 0.0, "positions_value": 0.0,
            "total_equity": 10_000_000.0, "settled_cash": 0.0}


def _h(name, qty, avg, cur):
    return {"name": name, "qty": qty, "avg_px": avg, "cur_px": cur, "evlu_amt": qty * cur}


def _rows(mock_db, positions):
    mock_db.fetchall.return_value = [
        {"code": c, "name": n, "qty": q, "entry_px": a} for c, n, q, a in positions]


def _inserted(mock_db):
    calls = [c for c in mock_db.executemany.call_args_list
             if "INSERT INTO trades" in c[0][0]]
    return calls[0][0][1] if calls else []


def test_a_fill_the_poller_missed_is_recorded(mock_db):
    """회귀: 장부에 없고 잔고에만 있는 종목이 그날 산 것이다 (KG케미칼 225주)."""
    _rows(mock_db, [])
    n = live.record_today_trades(_snap({"001390": _h("KG케미칼", 225.0, 4454.755, 4490.0)}),
                                 "quality_v1")

    assert n == 1
    (mode, side, code, name, qty, px, amt, strategy, reason), = _inserted(mock_db)
    assert (side, code, qty) == ("buy", "001390", 225.0)
    assert px == pytest.approx(4454.755)     # 신규 편입이면 매입평균이 곧 체결가다
    assert amt == pytest.approx(225 * 4454.755)


def test_a_stock_that_vanished_from_the_balance_is_a_sell(mock_db):
    """전량 매도한 종목은 잔고 응답에서 아예 사라진다 — 장부 쪽에서 봐야 보인다."""
    _rows(mock_db, [("015860", "일진홀딩스", 76.0, 6000.0)])
    mock_db.fetchone.return_value = {"c": 6650.0}      # 마지막 종가

    n = live.record_today_trades(_snap({}), "quality_v1")

    assert n == 1
    (_m, side, code, _n, qty, px, _a, _s, reason), = _inserted(mock_db)
    assert (side, code, qty, px, reason) == ("sell", "015860", 76.0, 6650.0, "rebalance")


def test_adding_to_a_position_prices_only_the_new_shares(mock_db):
    """추가 매수는 이 종목의 매입평균 변화에서 단가를 역산한다.

    100주를 10,000원에 들고 있다가 20주를 12,000원에 더 사면 평균이 10,333.33이 된다.
    보유 전체의 원가(10,333)가 아니라 이번에 산 가격(12,000)이 장부에 남아야 한다.
    """
    _rows(mock_db, [("000000", "테스트", 100.0, 10_000.0)])

    live.record_today_trades(_snap({"000000": _h("테스트", 120.0, 10_333.3333, 11_000.0)}),
                             "quality_v1")

    (_m, side, _c, _n, qty, px, *_), = _inserted(mock_db)
    assert (side, qty) == ("buy", 20.0)
    assert round(px) == 12_000


def test_an_unchanged_account_writes_nothing(mock_db):
    """차이가 없으면 손대지 않는다 — 이미 기록된 것을 지워 버리면 안 된다.

    이 규칙 하나로 여러 번 돌려도 안전하다. 정산이 끝나면 positions가 잔고와 같아지므로
    두 번째 실행은 차이를 못 찾고, 그때 삭제부터 하면 방금 쓴 기록이 날아간다.
    """
    _rows(mock_db, [("000000", "테스트", 100.0, 10_000.0)])

    n = live.record_today_trades(_snap({"000000": _h("테스트", 100.0, 10_000.0, 11_000.0)}),
                                 "quality_v1")

    assert n == 0
    assert not mock_db.executemany.called
    assert not any("DELETE FROM trades" in c[0][0] for c in mock_db.execute.call_args_list)


def test_existing_rows_are_never_deleted(mock_db):
    """회귀: 빠진 것을 더하기만 한다. 지우고 다시 쓰면 안 된다.

    positions는 낮 동안 이미 부분적으로 잔고에 맞춰져 있어서, 차이에는 '아직 기록되지
    않은 것'만 남는다. 2026-09-01에 그 차이가 KG케미칼 하나였는데 그날 기록 9건을
    지우고 1건만 다시 써서 나머지 9건이 사라졌다.
    """
    # 이미 9종목이 기록돼 positions에 반영돼 있고, 하나만 누락된 상태
    _rows(mock_db, [(f"00{i:04d}", f"종목{i}", 10.0, 1000.0) for i in range(9)])
    held = {f"00{i:04d}": _h(f"종목{i}", 10.0, 1000.0, 1100.0) for i in range(9)}
    held["001390"] = _h("KG케미칼", 225.0, 4454.755, 4490.0)

    n = live.record_today_trades(_snap(held), "quality_v1")

    assert n == 1, "누락된 하나만 기록해야 한다"
    assert [r[2] for r in _inserted(mock_db)] == ["001390"]
    assert not any("DELETE FROM trades" in c[0][0] for c in mock_db.execute.call_args_list)


def test_a_sell_with_no_price_anywhere_is_skipped(mock_db, capsys):
    """단가를 만들 수 없으면 0원짜리 거래를 남기지 않는다."""
    _rows(mock_db, [("999999", "상장폐지", 10.0, 5000.0)])
    mock_db.fetchone.return_value = None          # 일봉도 없다

    n = live.record_today_trades(_snap({}), "quality_v1")

    assert n == 0
    assert "장부 보류" in capsys.readouterr().out


# --- scheduler.settle_job -----------------------------------------------------

def test_settle_job_records_then_reconciles(mocker, mock_db, capsys):
    """순서가 중요하다. reconcile이 positions를 잔고에 맞추는 순간 차이가 사라지므로,
    거래를 먼저 기록하고 그 다음에 맞춰야 한다."""
    import scheduler

    mocker.patch.object(scheduler.config, "KIS_MODE", "live")
    mocker.patch.object(scheduler, "_acquire_open_lock", return_value=True)
    mocker.patch.object(scheduler, "_release_open_lock")
    mocker.patch.object(live, "account_snapshot", return_value=_snap({}))

    order = []
    mocker.patch.object(live, "record_today_trades",
                        side_effect=lambda *a: order.append("record") or 1)
    mocker.patch.object(live, "reconcile_positions",
                        side_effect=lambda *a: order.append("reconcile") or [])

    scheduler.settle_job()

    assert order == ["record", "reconcile"]


def test_settle_job_places_no_orders(mocker, mock_db):
    """정산은 장부만 만진다. 장이 끝난 뒤라 주문을 내서도 안 된다."""
    import scheduler

    mocker.patch.object(scheduler.config, "KIS_MODE", "live")
    mocker.patch.object(scheduler, "_acquire_open_lock", return_value=True)
    mocker.patch.object(scheduler, "_release_open_lock")
    mocker.patch.object(live, "account_snapshot", return_value=_snap({}))
    mocker.patch.object(live, "record_today_trades", return_value=0)
    mocker.patch.object(live, "reconcile_positions", return_value=[])
    buy = mocker.patch.object(live, "buy")
    sell = mocker.patch.object(live, "sell")

    scheduler.settle_job()

    buy.assert_not_called()
    sell.assert_not_called()


def test_settle_job_yields_to_a_running_rebalance(mocker, mock_db, capsys):
    """앞 회차가 아직 주문 중이면 그 기록과 겹치므로 물러난다."""
    import scheduler

    mocker.patch.object(scheduler.config, "KIS_MODE", "live")
    mocker.patch.object(scheduler, "_acquire_open_lock", return_value=False)
    rec = mocker.patch.object(live, "record_today_trades")

    scheduler.settle_job()

    rec.assert_not_called()
    assert "건너뜀" in capsys.readouterr().out


def test_settle_job_is_registered_after_the_close(mocker):
    """15:40 — 장 마감(15:30) 직후라 잔고가 그날의 최종값이다."""
    import scheduler

    jobs = []
    sched = mocker.MagicMock()
    sched.add_job.side_effect = lambda fn, trig: jobs.append((fn.__name__, trig))
    mocker.patch.object(scheduler, "BlockingScheduler", return_value=sched)
    mocker.patch("db.connection.init_schema")

    scheduler.main([])

    at = {n: t for n, t in jobs}
    assert "settle_job" in at, [n for n, _ in jobs]
    f = {str(x.name): str(x) for x in at["settle_job"].fields}
    assert (f["hour"], f["minute"]) == ("15", "40")
