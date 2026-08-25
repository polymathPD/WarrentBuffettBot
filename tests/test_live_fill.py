"""
executor.live.adjust()의 재주문 규칙 회귀 테스트.

2026-08-24, 1,000만원 모의계좌가 2,670만원어치를 들고 미수 -1,640만원이 났다.
원인 하나는 체결 반영 지연이었다. 그날 체결된 2,097주 중 우리가 15초 폴링으로 본
것은 793주(38%)뿐이었고, 잔고에 안 잡힌 1,304주를 '미체결'로 보고 다시 주문했다.
원주문은 그때까지도 살아 있었으므로 같은 물량을 두 번 산 셈이다.

규칙: 부족분 재주문은 앞선 주문이 끝났다고 증권사가 확인해 준 경우에만.
확인할 수 없으면(조회 실패, 또는 모의 서버가 내역을 안 주는 경우) 주문하지 않는다.
"""
import pytest

from executor import live


@pytest.fixture
def broker(mocker, mock_db):
    """주문 API와 잔고를 갈아 끼운다. .orders에 (side, qty)가 쌓인다."""
    mocker.patch.object(live, "guard", return_value=None)
    mocker.patch.object(live, "_settled_cash", return_value=10_000_000.0)
    # 미보유 종목의 매수 단가는 stock_daily 종가에서 온다 (snapshot에 현재가가 없으므로).
    mock_db.fetchone.return_value = {"c": 10_000.0}

    state = {"qty": 0.0, "orders": []}

    # 폴링 자체는 여기서 검증하지 않는다 - 잔고가 얼마로 보이는지만 준다.
    mocker.patch.object(
        live, "_wait_for_fill",
        side_effect=lambda code, expected: (
            {"pdno": code, "hldg_qty": str(state["qty"]), "pchs_avg_pric": "10000"},
            state["qty"]))

    def _order(side):
        def f(code, qty):
            state["orders"].append((side, qty))
            return {"rt_cd": "0"}
        return f

    mocker.patch.object(live, "buy", side_effect=_order("buy"))
    mocker.patch.object(live, "sell", side_effect=_order("sell"))
    mocker.patch.object(
        live, "_find_holding",
        side_effect=lambda code: {"pdno": code, "hldg_qty": str(state["qty"]),
                                  "pchs_avg_pric": "10000"})
    return state


def _snap(qty=0.0):
    h = {"000000": {"name": "테스트", "qty": qty, "cur_px": 10_000.0}} if qty else {}
    return {"holdings": h, "cash": 0.0, "positions_value": 0.0,
            "total_equity": 10_000_000.0, "settled_cash": 10_000_000.0}


def test_no_reorder_while_the_first_order_is_still_alive(broker, mocker):
    """원주문 11주가 미체결로 남아 있으면 추가 주문을 내지 않는다."""
    broker["qty"] = 3.0                       # 14주 중 3주만 잔고에 잡혔다
    mocker.patch.object(live, "_outstanding_qty", return_value=11)

    live.adjust("000000", "테스트", 14, "quality_v1", _snap())

    assert broker["orders"] == [("buy", 14)], "재주문이 나갔다"


def test_no_reorder_when_the_broker_will_not_say(broker, mocker):
    """미체결 조회가 안 되면 모르는 것이다 - 주문하지 않는다.

    KIS 모의는 rt_cd=0에 빈 output1을 주기도 한다. 이걸 '미체결 0'으로 읽으면
    지연된 체결을 미체결로 오해해 그대로 겹쳐 산다.
    """
    broker["qty"] = 3.0
    mocker.patch.object(live, "_outstanding_qty", return_value=None)

    live.adjust("000000", "테스트", 14, "quality_v1", _snap())

    assert broker["orders"] == [("buy", 14)], "확인도 없이 재주문이 나갔다"


def test_tops_up_only_after_the_order_is_confirmed_done(broker, mocker):
    """앞선 주문이 끝났고(미체결 0) 아직 목표에 못 미치면 그때만 부족분을 낸다."""
    broker["qty"] = 3.0
    mocker.patch.object(live, "_outstanding_qty", return_value=0)

    live.adjust("000000", "테스트", 14, "quality_v1", _snap())

    assert broker["orders"][0] == ("buy", 14)
    assert broker["orders"][1] == ("buy", 11), broker["orders"]


def test_sell_side_is_guarded_the_same_way(broker, mocker):
    """오늘 리밸런싱은 대부분 매도다. 매도도 겹쳐 내면 안 된다."""
    broker["qty"] = 90.0                      # 100 → 10 목표인데 아직 90
    mocker.patch.object(live, "_outstanding_qty", return_value=-80)

    live.adjust("000000", "테스트", 10, "quality_v1", _snap(100.0))

    assert broker["orders"] == [("sell", 90)], broker["orders"]


def test_no_rows_means_unknown_not_zero(mocker):
    """rt_cd=0인데 내역이 비어 있으면 None(모름)이어야 한다."""
    mocker.patch.object(live, "_headers", return_value={})
    resp = mocker.MagicMock()
    resp.json.return_value = {"rt_cd": "0", "output1": []}
    mocker.patch("requests.get", return_value=resp)

    assert live._outstanding_qty("000000") is None


def test_wait_for_fill_keeps_polling_until_the_target_is_reached(mocker):
    """수량이 '조금이라도' 바뀌면 빠져나오던 예전 판이 재주문의 방아쇠였다.
    3주가 붙어도 목표 14주에 닿을 때까지는 계속 기다려야 한다."""
    seen = iter([3.0, 3.0, 8.0, 14.0, 14.0])
    mocker.patch.object(
        live, "_find_holding",
        side_effect=lambda code: {"pdno": code, "hldg_qty": str(next(seen)),
                                  "pchs_avg_pric": "10000"})
    mocker.patch("time.sleep", return_value=None)

    _holding, qty = live._wait_for_fill("000000", 14.0, timeout_s=5.0, poll_s=0.0)

    assert qty == 14.0


def test_balance_retries_a_transient_server_error(mocker, capsys):
    """증권사 500 한 번에 하루 리밸런싱을 통째로 버리면 안 된다.

    2026-08-25 09:05, 계좌가 미수 -1,638만원인 채로 open_job이 떴는데
    inquire-balance가 500을 냈고 '잔고 조회 실패 - 건너뜀'만 찍고 끝났다.
    같은 요청이 몇 분 뒤에는 정상이었다.
    """
    mocker.patch("time.sleep", return_value=None)
    ok = {"rt_cd": "0", "output1": [], "output2": [{}]}
    mocker.patch.object(
        live, "_get_balance_once",
        side_effect=[RuntimeError("500 Server Error"), RuntimeError("500 Server Error"), ok])

    assert live.get_balance() is ok
    assert "잔고 조회 재시도" in capsys.readouterr().out


def test_balance_gives_up_after_the_last_retry(mocker):
    """계속 실패하면 마지막 예외를 그대로 올린다 - 조용히 넘어가지 않는다."""
    mocker.patch("time.sleep", return_value=None)
    mocker.patch.object(live, "_get_balance_once", side_effect=RuntimeError("500 Server Error"))

    with pytest.raises(RuntimeError, match="500"):
        live.get_balance()
    assert live._get_balance_once.call_count == live._RETRIES


def test_order_retries_a_transient_server_error(mocker, capsys):
    """주문 전송 500 하나가 리밸런싱 전체를 죽이면 안 된다 (2026-08-25)."""
    mocker.patch("time.sleep", return_value=None)
    ok = {"rt_cd": "0", "msg1": "주문 전송 완료"}
    mocker.patch.object(live, "_order_once",
                        side_effect=[RuntimeError("500 Server Error"), ok])

    assert live.sell("000000", 10) is ok
    assert "주문 전송 재시도" in capsys.readouterr().out


def test_order_gives_up_after_the_last_retry(mocker):
    mocker.patch("time.sleep", return_value=None)
    mocker.patch.object(live, "_order_once", side_effect=RuntimeError("500 Server Error"))

    with pytest.raises(RuntimeError, match="500"):
        live.buy("000000", 10)
    assert live._order_once.call_count == live._RETRIES


def test_short_cash_buys_fewer_shares_instead_of_skipping(broker, mocker, capsys):
    """돈이 모자라면 슬롯을 통째로 비우지 말고 살 수 있는 만큼 산다.

    예전 판은 주문 전액이 결제 예정 예수금을 넘으면 그대로 break해서 슬롯이 0주로
    남았다. 목표 비중에서 보면 '조금 덜 채운 것'보다 '아예 안 산 것'이 더 멀다.
    """
    mocker.patch.object(live, "_settled_cash", return_value=520_000.0)
    mocker.patch.object(live, "_outstanding_qty", return_value=None)
    #                                10,000원 × 1.005 = 10,050 → 520,000 // 10,050 = 51주
    live.adjust("000000", "테스트", 100, "quality_v1", _snap())

    assert broker["orders"] == [("buy", 51)], broker["orders"]
    assert "수량 축소" in capsys.readouterr().out


def test_one_share_unaffordable_still_skips(broker, mocker, capsys):
    mocker.patch.object(live, "_settled_cash", return_value=5_000.0)

    live.adjust("000000", "테스트", 100, "quality_v1", _snap())

    assert broker["orders"] == []
    assert "1주도 못 산다" in capsys.readouterr().out
