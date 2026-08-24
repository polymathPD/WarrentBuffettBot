"""
scheduler.open_job() 회귀 테스트.

2026-08-24에 발견한 버그 두 가지가 배경이다.

1. open_job이 config.KIS_MODE / config.get_setting을 참조하는데 scheduler.py가
   config를 임포트하지 않아 09:05 첫 실행이 NameError로 죽었다. 테스트 253개가
   전부 통과하던 상태였는데, 이 함수를 실행하는 테스트가 하나도 없었기 때문이다.
   파이썬은 전역 이름을 호출 시점에 찾으므로 `import scheduler`만으로는 드러나지
   않는다. 여기서는 함수를 실제로 끝까지 돌린다.

2. 그날 수동 재실행이 겹치면서 1,000만원 계좌가 2,670만원어치를 들고 미수
   -1,640만원이 났다. KIS 모의가 매수 뒤에도 dnca_tot_amt를 그대로 두는 탓에
   예수금+평가로 잡은 자산이 실행할 때마다 불어났다. 예산은 증권사가 계산한
   순자산(nass_amt) 하나만 봐야 하고, 미수 상태면 리밸런싱일이 아니어도
   정리해야 한다 (T+2에 반대매매로 끝나므로 달력을 기다릴 수 없다).
"""
import pytest

import scheduler


SLOTS = 10


def _snapshot(holdings, cash, positions_value, total_equity, settled_cash):
    return {"holdings": holdings, "cash": cash, "positions_value": positions_value,
            "total_equity": total_equity, "settled_cash": settled_cash,
            "raw_summary": {}}


@pytest.fixture
def live_run(mocker, mock_db, mock_settings):
    """KIS_MODE=live로 open_job을 돌리되 증권사 호출만 가로챈다.

    반환한 객체의 .orders에 (code, target_qty)가 쌓인다.
    """
    from executor import live
    from strategy import quality

    mocker.patch.object(scheduler.config, "KIS_MODE", "live")

    orders = []
    mocker.patch.object(
        live, "adjust",
        side_effect=lambda code, name, target_qty, *a, **kw: orders.append((code, target_qty)))

    # 후보 10종목: 모두 10,000원, 에이전트는 전원 매수.
    mocker.patch.object(quality, "get_targets", return_value=[
        {"code": f"00{i:04d}", "score": 1.0, "per": 5.0, "pbr": 0.5, "close": 10_000.0}
        for i in range(SLOTS)])
    for name in ("value_trap", "market_state", "risk", "disclosure", "financials"):
        mod = __import__(f"agents.{name}", fromlist=[name])
        mocker.patch.object(mod, "analyze", return_value={
            "decision": "매수", "score": 8, "rationale": "테스트"})

    mocker.patch("recorder.equity.snapshot", return_value=None)

    class Run:
        orders = None

        def __call__(self, snap, rebal_d="2026-08-21"):
            orders.clear()
            mocker.patch.object(live, "account_snapshot", return_value=snap)
            mocker.patch.object(scheduler, "_quality_rebalance_date", return_value=rebal_d)
            mocker.patch.object(scheduler, "_prev_trading_day", return_value="2026-08-21")
            scheduler.open_job()
            Run.orders = list(orders)
            return Run.orders

    return Run()


def test_open_job_runs_end_to_end(live_run, capsys):
    """회귀 핵심: 예전에는 첫 print 줄에서 NameError로 죽었다."""
    live_run(_snapshot({}, 10_000_000, 0, 10_000_000, 10_000_000))

    out = capsys.readouterr().out
    assert "개장 리밸런싱" in out
    assert len(live_run.orders) == SLOTS


def test_budget_comes_from_net_assets_not_deposit_plus_holdings(live_run):
    """KIS 모의는 매수해도 예수금이 안 줄어든다. 예수금+평가로 예산을 잡으면
    재실행마다 슬롯이 불어나 미수가 뚫린다 - 순자산(nass_amt)만 봐야 한다."""
    holdings = {f"00{i:04d}": {"name": f"종목{i}", "qty": 100.0, "cur_px": 10_000.0}
                for i in range(SLOTS)}
    # 예수금은 1,000만원 그대로인데 이미 1,000만원어치를 들고 있는 상태.
    # 예수금+평가로 계산하면 2,000만원 → 슬롯당 200주로 두 배를 더 산다.
    snap = _snapshot(holdings, cash=10_000_000, positions_value=10_000_000,
                     total_equity=10_000_000, settled_cash=0)

    live_run(snap)

    assert dict(live_run.orders) == {c: 100 for c in holdings}, \
        "순자산 1,000만원 / 10슬롯 / 10,000원 = 100주여야 한다"


def test_debt_is_unwound_outside_the_rebalance_calendar(live_run, capsys):
    """미수는 T+2에 반대매매로 끝난다. 다음 리밸런싱일까지 기다리면 늦는다."""
    holdings = {f"00{i:04d}": {"name": f"종목{i}", "qty": 260.0, "cur_px": 10_000.0}
                for i in range(SLOTS)}
    snap = _snapshot(holdings, cash=10_000_000, positions_value=26_000_000,
                     total_equity=10_000_000, settled_cash=-16_000_000)

    live_run(snap, rebal_d=None)   # 리밸런싱일이 아니다

    out = capsys.readouterr().out
    assert "미수" in out
    assert dict(live_run.orders) == {c: 100 for c in holdings}, \
        "260주 → 100주로 줄이는 매도 주문이 나가야 한다"


def test_non_rebalance_day_is_skipped_when_the_account_is_healthy(live_run, capsys):
    holdings = {"000000": {"name": "종목0", "qty": 100.0, "cur_px": 10_000.0}}
    snap = _snapshot(holdings, 0, 10_000_000, 10_000_000, settled_cash=0)

    live_run(snap, rebal_d=None)

    assert "건너뜀" in capsys.readouterr().out
    assert live_run.orders == []


def test_sells_are_all_placed_before_buys(live_run, mocker):
    """매도 대금이 잡히기 전에 매수를 내면 미수 가드에 걸려 슬롯이 빈 채로 끝난다.

    2026-08-25 리밸런싱을 미리 돌려 보니, 편출 5종목을 판 뒤에도 결제예정이 +88만인
    시점에 랭킹 3위의 100만원짜리 매수가 나가 가드에 막혔다. 매도를 전부 먼저 내면
    +519만이 확보된 뒤 매수가 시작된다.
    """
    from strategy import quality

    # 후보 절반은 이미 목표보다 많이 들고 있고(매도), 절반은 신규(매수)다.
    mocker.patch.object(quality, "get_targets", return_value=[
        {"code": f"00{i:04d}", "score": 1.0, "per": 5.0, "pbr": 0.5, "close": 10_000.0}
        for i in range(SLOTS)])
    holdings = {f"00{i:04d}": {"name": f"종목{i}", "qty": 500.0, "cur_px": 10_000.0}
                for i in range(0, SLOTS, 2)}          # 짝수 번호만 보유, 전부 초과
    holdings["999999"] = {"name": "편출", "qty": 100.0, "cur_px": 10_000.0}
    snap = _snapshot(holdings, 0, 6_000_000, 10_000_000, settled_cash=-5_000_000)

    live_run(snap, rebal_d=None)

    deltas = [target - holdings.get(code, {}).get("qty", 0.0)
              for code, target in live_run.orders]
    last_sell = max(i for i, d in enumerate(deltas) if d < 0)
    first_buy = min(i for i, d in enumerate(deltas) if d > 0)
    assert last_sell < first_buy, f"매도/매수가 섞였다: {deltas}"
