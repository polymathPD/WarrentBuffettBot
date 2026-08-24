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
    mocker.patch.object(live, "_settled_cash_after", return_value=10_000_000.0)

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
