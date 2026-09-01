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

    state = {"qty": 0.0, "orders": [], "db": mock_db}
    mocker.patch.object(live, "_today_traded", return_value=(0.0, 0.0, {}))

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


def _snap(qty=0.0, avg=10_000.0):
    h = {"000000": {"name": "테스트", "qty": qty, "cur_px": 10_000.0,
                    "avg_px": avg}} if qty else {}
    return {"holdings": h, "cash": 0.0, "positions_value": 0.0,
            "total_equity": 10_000_000.0, "settled_cash": 10_000_000.0}


def test_no_reorder_while_shares_are_locked_in_a_live_sell(broker, mocker):
    """매도 주문에 묶인 주식이 있으면 추가로 내지 않는다 - 겹쳐 팔면 안 된다."""
    broker["qty"] = 90.0                      # 100 → 10 목표인데 아직 90
    mocker.patch.object(live, "_pending_sell_qty", return_value=80.0)

    live.adjust("000000", "테스트", 10, "quality_v1", _snap(100.0))

    assert broker["orders"] == [("sell", 90)], broker["orders"]


def test_no_reorder_when_the_balance_will_not_answer(broker, mocker):
    """대기 수량을 못 읽으면 모르는 것이다 - 매도를 겹쳐 내지 않는다."""
    broker["qty"] = 90.0
    mocker.patch.object(live, "_pending_sell_qty", return_value=None)

    live.adjust("000000", "테스트", 10, "quality_v1", _snap(100.0))

    assert broker["orders"] == [("sell", 90)], "확인도 없이 재주문이 나갔다"


def test_sells_the_shortfall_when_nothing_is_locked(broker, mocker):
    """묶인 수량이 0이면 앞선 주문은 끝난 것이다. 부족분을 다시 낸다.

    2026-08-25에 오리온홀딩스 78주 매도가 40주만 체결되고 잔량이 종료됐는데,
    묶인 수량이 0인데도 재주문을 막아 38주가 남았다. 그 미청산이 현금을 묶어
    신규 편입 3종목이 통째로 빠졌다.
    """
    broker["qty"] = 60.0                      # 100 → 0 목표인데 40주만 팔렸다
    mocker.patch.object(live, "_pending_sell_qty", return_value=0.0)

    live.adjust("000000", "테스트", 0, "quality_v1", _snap(100.0))

    assert broker["orders"][0] == ("sell", 100)
    assert broker["orders"][1] == ("sell", 60), broker["orders"]


def test_pending_sell_qty_reads_the_gap_in_the_balance(mocker):
    """주문 내역 API가 없어도 잔고가 알려준다: 보유 - 주문가능 = 매도 대기."""
    mocker.patch.object(live, "_find_holding",
                        return_value={"pdno": "000000", "hldg_qty": "78",
                                      "ord_psbl_qty": "38"})
    assert live._pending_sell_qty("000000") == 40.0

    mocker.patch.object(live, "_find_holding", return_value=None)
    assert live._pending_sell_qty("000000") == 0.0

    mocker.patch.object(live, "_find_holding", side_effect=RuntimeError("500"))
    assert live._pending_sell_qty("000000") is None


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
    mocker.patch.object(live, "_pending_sell_qty", return_value=0.0)
    #                                10,000원 × 1.005 = 10,050 → 520,000 // 10,050 = 51주
    live.adjust("000000", "테스트", 100, "quality_v1", _snap())

    assert broker["orders"] == [("buy", 51)], broker["orders"]
    assert "수량 축소" in capsys.readouterr().out


def test_one_share_unaffordable_still_skips(broker, mocker, capsys):
    mocker.patch.object(live, "_settled_cash", return_value=5_000.0)

    live.adjust("000000", "테스트", 100, "quality_v1", _snap())

    assert broker["orders"] == []
    assert "1주도 못 산다" in capsys.readouterr().out


def test_fill_price_comes_from_the_brokers_daily_total(broker, mocker):
    """매도 기록의 단가가 매도가여야 한다.

    2026-08-25까지는 잔고의 pchs_avg_pric(매입평균가)을 기록했다. 그날 매도 8건 중
    7건이 KIS 매입평균가와 소수점까지 일치했다 - 실현손익이 구조적으로 0이 된다.
    모의계좌에는 체결 단가 API가 없으므로(일별주문체결조회는 빈 응답, 실현손익은
    '없는 서비스 코드'), 오늘 누적 매도대금의 증분에서 역산한다.
    """
    broker["qty"] = 100.0
    mocker.patch.object(live, "_pending_sell_qty", return_value=0.0)
    # 주문 전 (매수 0, 매도 0) → 주문 후 (매수 0, 매도 1,030,000). 현재가 10,000
    # 대비 +3%다 - 국내 주식은 하루 ±30%가 한도라 그 밖의 값은 체결가일 수 없고
    # _PX_SANITY에 걸린다.
    mocker.patch.object(live, "_today_traded", side_effect=[(0.0, 0.0, {}), (0.0, 1_030_000.0, {"000000": 100.0})])

    def _after(code, expected):
        broker["qty"] = 0.0
        return {"pdno": code, "hldg_qty": "0", "pchs_avg_pric": "11111"}, 0.0
    mocker.patch.object(live, "_wait_for_fill", side_effect=_after)

    live.adjust("000000", "테스트", 0, "quality_v1", _snap(100.0))

    ins = [c for c in broker["db"].execute.call_args_list if "INSERT INTO trades" in c[0][0]]
    assert ins, "매매 기록이 없다"
    price = ins[0][0][1][4]          # (mode, code, name, qty, price, amount, strategy)
    assert price == 10_300.0, f"매입평균가 11,111이 아니라 매도가 10,300이어야 한다: {price}"


def test_reconcile_recovers_a_position_that_was_never_recorded(mocker, mock_db):
    """체결을 못 본 종목도 잔고에는 있다. 그 사실이 DB에 남아야 한다.

    2026-08-24에 오리온홀딩스 78주와 영원무역홀딩스 11주를 실제로 샀는데, 체결이
    폴링 뒤에 잡혀 adjust()가 filled==0으로 끝났다. positions에도 trades에도 아무것도
    안 남아서 대시보드의 진입 판단이 빈칸이었다 - 게이트는 통과했는데도.
    """
    mock_db.fetchone.return_value = None          # DB에 그 포지션이 없다
    mock_db.fetchall.return_value = []
    snap = {"holdings": {"001800": {"name": "오리온홀딩스", "qty": 78.0,
                                    "avg_px": 25_425.0, "cur_px": 25_650.0}}}

    changed = live.reconcile_positions(snap, "quality_v1",
                                       {"001800": {"value_trap": {"decision": "매수"}}})

    assert changed == ["001800"]
    sql, params = mock_db.execute.call_args[0]
    assert "INSERT INTO positions" in sql
    assert params[5] == 78.0                       # 잔고 수량 그대로
    assert "value_trap" in params[7]               # 판단도 함께 남는다


def test_reconcile_drops_a_position_the_broker_no_longer_shows(mocker, mock_db):
    mock_db.fetchone.return_value = {"qty": 100.0}
    mock_db.fetchall.return_value = [{"code": "010780"}]

    changed = live.reconcile_positions({"holdings": {}}, "quality_v1")

    assert changed == ["010780"]
    sql, _ = mock_db.execute.call_args[0]
    assert "DELETE FROM positions" in sql


def test_buys_are_never_re_ordered(broker, mocker, capsys):
    """매수 부족분은 다시 내지 않는다 - 대기 수량을 셀 방법이 없다.

    2026-08-25 일진홀딩스는 폴링으로 32주만 보였는데 실제로는 142주가 체결됐다.
    그 시점에 부족분 110주를 다시 냈다면 목표의 네 배를 샀을 것이다. 매도는
    잔고의 ord_psbl_qty로 대기 수량을 알 수 있지만 매수는 그런 신호가 없다.
    """
    broker["qty"] = 32.0                      # 143주 목표인데 32주만 보인다
    mocker.patch.object(live, "_pending_sell_qty", return_value=0.0)

    live.adjust("000000", "테스트", 143, "quality_v1", _snap())

    assert broker["orders"] == [("buy", 143)], broker["orders"]
    assert "매수 중단" in capsys.readouterr().out


def test_buy_price_ignores_another_stocks_fill(broker, mocker):
    """매수 단가가 계좌 전체 누적이 아니라 이 종목의 매입금액에서 나와야 한다.

    2026-08-26에 일진홀딩스 4주가 _wait_for_fill 타임아웃 뒤에 체결되면서, 그
    대금 27,143원이 다음 종목인 대한해운의 thdt_buy_amt 증분에 섞였다. 대한해운
    21주가 그날 상한가 2,795보다 높은 3,370원에 기록되고 일진홀딩스는 아예
    누락됐다. 종목별 매입금액 차분은 이런 끼어듦에 오염되지 않는다.
    """
    mocker.patch.object(live, "_today_traded",
                        side_effect=[(0.0, 0.0, {}), (3_000_000.0, 0.0, {})])

    def _after(code, expected):
        broker["qty"] = 100.0
        return {"pdno": code, "hldg_qty": "100", "pchs_avg_pric": "10200"}, 100.0
    mocker.patch.object(live, "_wait_for_fill", side_effect=_after)

    live.adjust("000000", "테스트", 100, "quality_v1", _snap())

    ins = [c for c in broker["db"].execute.call_args_list
           if "INSERT INTO trades" in c[0][0]]
    assert ins, "매매 기록이 없다"
    price = ins[0][0][1][4]
    assert price == 10_200.0, (
        f"계좌 누적이 섞인 30,000이 아니라 매입평균 10,200이어야 한다: {price}")


def test_topping_up_prices_only_the_new_shares(broker, mocker):
    """이미 들고 있는 종목을 더 살 때, 기존 보유분의 매입금액을 빼야 한다."""
    broker["qty"] = 100.0
    mocker.patch.object(live, "_today_traded", return_value=(0.0, 0.0, {}))

    def _after(code, expected):
        broker["qty"] = 120.0
        return {"pdno": code, "hldg_qty": "120", "pchs_avg_pric": "10100"}, 120.0
    mocker.patch.object(live, "_wait_for_fill", side_effect=_after)

    # 120주 x 10,100 - 100주 x 10,000 = 212,000, 새로 산 20주로 나누면 10,600
    live.adjust("000000", "테스트", 120, "quality_v1", _snap(100.0, avg=10_000.0))

    ins = [c for c in broker["db"].execute.call_args_list
           if "INSERT INTO trades" in c[0][0]]
    assert ins, "매매 기록이 없다"
    qty, price = ins[0][0][1][3], ins[0][0][1][4]
    assert qty == 20.0, f"새로 산 20주만 기록해야 한다: {qty}"
    assert price == 10_600.0, (
        f"기존 보유분을 안 뺀 60,600이 아니라 10,600이어야 한다: {price}")


def test_impossible_price_is_rejected(broker, mocker, capsys):
    """산출가가 하루 등락 한도 밖이면 기록하지 않고 현재가로 대체한다.

    국내 주식은 ±30%가 한도라 현재가에서 그보다 먼 값은 체결가일 수 없다.
    매도는 매입평균이 안 바뀌어 계좌 전체 증분에 기댈 수밖에 없으므로, 이
    검사가 잘못된 귀속을 잡는 마지막 그물이다.
    """
    broker["qty"] = 100.0
    mocker.patch.object(live, "_pending_sell_qty", return_value=0.0)
    mocker.patch.object(live, "_today_traded",
                        side_effect=[(0.0, 0.0, {}), (0.0, 3_370_000.0, {"000000": 100.0})])

    def _after(code, expected):
        broker["qty"] = 0.0
        return {"pdno": code, "hldg_qty": "0", "pchs_avg_pric": "11111"}, 0.0
    mocker.patch.object(live, "_wait_for_fill", side_effect=_after)

    live.adjust("000000", "테스트", 0, "quality_v1", _snap(100.0))

    assert "단가 이상" in capsys.readouterr().out
    ins = [c for c in broker["db"].execute.call_args_list
           if "INSERT INTO trades" in c[0][0]]
    price = ins[0][0][1][4]
    assert price == 10_000.0, f"33,700이 아니라 현재가 10,000으로 대체해야 한다: {price}"


def test_ledger_check_flags_a_mismatch(mocker, mock_db, capsys):
    """장부 합계가 증권사 집계와 다르면 그날 안에 알려야 한다.

    2026-08-25(매도가가 매입평균가)도 2026-08-26(앞 종목 체결이 섞임)도 이틀 뒤에
    손으로 대조해서 찾았다. 두 번 다 이 비교 하나로 그날 걸렸을 것이다.
    """
    mocker.patch.object(live, "_today_traded", return_value=(70_760.0, 48_000.0, {}))
    mock_db.fetchone.return_value = {"b": 43_620.0, "s": 48_000.0}

    assert live.check_today_ledger() is False
    out = capsys.readouterr().out
    assert "장부 불일치" in out
    assert "-27,140" in out, out


def test_ledger_check_passes_when_totals_agree(mocker, mock_db, capsys):
    mocker.patch.object(live, "_today_traded", return_value=(70_760.0, 48_000.0, {}))
    mock_db.fetchone.return_value = {"b": 70_760.0, "s": 48_000.0}

    assert live.check_today_ledger() is True
    assert "장부 확인" in capsys.readouterr().out


def test_token_waits_long_only_for_the_rate_limit(mocker):
    """65초 백오프는 발급 분당 1회 제한(403) 때문이다. 네트워크 실패에는 쓰지 않는다.

    2026-08-26에 KIS가 ReadTimeout을 내자 잔고 조회 한 번이 4회 x (65+130+195초)로
    26분을 썼다. open_job은 종목마다 잔고를 보므로 아침 잡이 오후 회차까지 이어진다.
    """
    import requests

    resp = mocker.Mock(status_code=403)
    assert live._rate_limited(requests.HTTPError(response=resp)) is True
    assert live._rate_limited(requests.exceptions.ReadTimeout("timed out")) is False
    assert live._rate_limited(requests.HTTPError(response=mocker.Mock(status_code=500))) is False


def test_token_retry_backoff_is_short_for_a_timeout(mocker):
    import requests

    slept = []
    mocker.patch("time.sleep", side_effect=slept.append)
    live._token_cache["expires_at"] = 0
    ok = mocker.Mock()
    ok.json.return_value = {"access_token": "t", "expires_in": 86400}
    mocker.patch("requests.post",
                 side_effect=[requests.exceptions.ReadTimeout("timed out"), ok])

    live._get_token()

    assert slept == [live._TOKEN_NET_BACKOFF_S], slept


def test_a_concurrent_sell_disqualifies_the_account_wide_total(broker, mocker, capsys):
    """회귀: 같은 구간에 다른 종목도 팔렸으면 계좌 매도금액을 이 종목 몫으로 못 쓴다.

    2026-09-01 리밸런싱이 성우하이텍 → 일진홀딩스 → 대한유화 순으로 팔았는데,
    일진홀딩스 76주 대금이 KIS 집계에 늦게 반영되어 대한유화 2주 구간에 섞였다.
    산출가가 583,100원 — 현재가 98,900원의 5.9배였다.

    ±30% 가드만으로는 부족하다. 섞인 금액이 작으면 그럴듯한 값이 되어 조용히
    통과한다. 종목별 thdt_sll_qty는 귀속이 보장되므로 그것으로 판정한다.
    """
    broker["qty"] = 100.0
    mocker.patch.object(live, "_pending_sell_qty", return_value=0.0)
    # 이 종목은 100주를 팔았고, 그 사이 999999도 76주 팔렸다.
    mocker.patch.object(live, "_today_traded", side_effect=[
        (0.0, 0.0, {"000000": 0.0, "999999": 0.0}),
        (0.0, 1_100_000.0, {"000000": 100.0, "999999": 76.0}),
    ])

    def _after(code, expected):
        broker["qty"] = 0.0
        return {"pdno": code, "hldg_qty": "0", "pchs_avg_pric": "10000"}, 0.0
    mocker.patch.object(live, "_wait_for_fill", side_effect=_after)

    live.adjust("000000", "테스트", 0, "quality_v1", _snap(100.0))

    out = capsys.readouterr().out
    assert "단가 보류" in out and "999999" in out, out
    ins = [c for c in broker["db"].execute.call_args_list
           if "INSERT INTO trades" in c[0][0]]
    assert ins[0][0][1][4] == 10_000.0, "현재가로 대체해야 한다"


def test_the_account_wide_total_is_used_when_nothing_else_sold(broker, mocker):
    """다른 종목이 팔리지 않았으면 계좌 증분이 이 종목 몫이 맞다."""
    broker["qty"] = 100.0
    mocker.patch.object(live, "_pending_sell_qty", return_value=0.0)
    mocker.patch.object(live, "_today_traded", side_effect=[
        (0.0, 0.0, {"000000": 0.0, "999999": 12.0}),
        (0.0, 1_030_000.0, {"000000": 100.0, "999999": 12.0}),   # 999999는 그대로
    ])

    def _after(code, expected):
        broker["qty"] = 0.0
        return {"pdno": code, "hldg_qty": "0", "pchs_avg_pric": "10000"}, 0.0
    mocker.patch.object(live, "_wait_for_fill", side_effect=_after)

    live.adjust("000000", "테스트", 0, "quality_v1", _snap(100.0))

    ins = [c for c in broker["db"].execute.call_args_list
           if "INSERT INTO trades" in c[0][0]]
    assert ins[0][0][1][4] == 10_300.0, "1,030,000 / 100주 = 10,300"


def test_cost_basis_comes_from_the_broker_not_the_fill_price(broker, mocker):
    """회귀: entry_px는 이번 체결가가 아니라 증권사의 매입평균단가다.

    추가 매수하면 그 회차 단가가 보유 전체의 원가를 덮고, 일부만 판 날에는 판
    가격이 원가로 들어간다. 2026-09-01 대한유화는 실제 원가 81,794원인데 매도
    추정가 98,900원으로 적혀 있었다 — 21% 어긋난 값이 대시보드 손익의 분모였다.
    """
    broker["qty"] = 100.0          # 이미 100주 보유 (평균 10,000원)

    def _after(code, expected):
        broker["qty"] = 120.0
        # 20주를 12,000원에 더 사서 (1,000,000 + 240,000) / 120 = 10,333.3333.
        return {"pdno": code, "hldg_qty": "120", "pchs_avg_pric": "10333.3333"}, 120.0
    mocker.patch.object(live, "_wait_for_fill", side_effect=_after)

    live.adjust("000000", "테스트", 120, "quality_v1", _snap(100.0))

    pos = [c for c in broker["db"].execute.call_args_list
           if "INSERT INTO positions" in c[0][0]]
    entry_px = pos[0][0][1][4]
    assert entry_px == 10_333.3333, f"증권사 매입평균단가여야 한다: {entry_px}"

    trd = [c for c in broker["db"].execute.call_args_list
           if "INSERT INTO trades" in c[0][0]]
    assert round(trd[0][0][1][4]) == 12_000, "거래 장부에는 이번 체결가가 남아야 한다"
