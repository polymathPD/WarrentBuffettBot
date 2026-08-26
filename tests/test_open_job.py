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
    mocker.patch.object(scheduler, "_stored_targets", return_value=(set(), None))
    mocker.patch.object(scheduler, "_store_targets")
    # 가로채지 않으면 증권사에 실제로 붙는다. open_job이 예외를 삼키므로 테스트는
    # 실패하지 않고 토큰 재시도(65+130+195초)에 걸려 멈춘다.
    mocker.patch.object(live, "check_today_ledger", return_value=True)

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


def test_one_failing_order_does_not_abort_the_rest(live_run, mocker, capsys):
    """종목 하나가 터져도 나머지 주문과 자산 스냅샷은 나가야 한다.

    2026-08-25 리밸런싱이 이걸 못 해서 계좌가 위험한 중간 상태로 남았다. 매도 8건을
    낸 뒤 다음 주문이 KIS 500을 받았고, 예외가 adjust()를 뚫고 open_job까지 올라와
    남은 매도와 매수 5건, equity_daily 기록이 전부 사라졌다. 미수는 절반만 털렸다.

    이 테스트가 없었던 이유는 fixture가 live.adjust를 리스트에 append하는 람다로
    갈아 끼워서 - 절대 예외를 내지 않는 것으로 - 루프의 내성을 한 번도 시험하지
    않았기 때문이다.
    """
    from executor import live

    eq = mocker.patch("recorder.equity.snapshot")
    calls = []

    def _boom(code, name, target_qty, *a, **kw):
        calls.append(code)
        if len(calls) == 2:
            raise RuntimeError("500 Server Error")

    mocker.patch.object(live, "adjust", side_effect=_boom)

    live_run(_snapshot({}, 10_000_000, 0, 10_000_000, 10_000_000))

    assert len(calls) == SLOTS, f"두 번째에서 멈췄다: {calls}"
    out = capsys.readouterr().out
    assert "주문 실패" in out
    assert eq.called, "자산 스냅샷이 기록되지 않았다"


def test_positions_are_reconciled_against_the_broker_after_ordering(live_run, mocker):
    """주문을 다 낸 뒤 잔고를 정답으로 삼아 DB를 맞춰야 한다.

    adjust()는 체결을 못 보면 아무것도 기록하지 않고 끝난다. 2026-08-24에 실제로
    산 오리온홀딩스 78주와 영원무역홀딩스 11주가 positions에도 trades에도 없었고,
    게이트를 통과했는데도 대시보드의 진입 판단이 빈칸이었다.
    """
    from executor import live

    rec = mocker.patch.object(live, "reconcile_positions", return_value=[])
    mocker.patch("recorder.equity.snapshot")

    live_run(_snapshot({}, 10_000_000, 0, 10_000_000, 10_000_000))

    assert rec.called, "주문 뒤 잔고 대조가 없다"
    _snap_arg, strategy, agents = rec.call_args[0]
    assert strategy == "quality_v1"
    assert len(agents) == SLOTS, "종목별 에이전트 판단이 함께 넘어가야 한다"


def test_worker_migrates_the_schema_before_doing_anything(mocker):
    """배포만으로 스키마가 안 맞으면 그날 배치가 통째로 실패한다.

    init_schema()는 setup_db.py에서만 불렸다 - 손으로 돌리는 스크립트라 배포
    경로에 없다. 2026-08-25에 positions.agents를 추가하고 배포했을 때 운영 DB에는
    컬럼이 없었고, 손으로 돌리지 않았다면 그날 12:00 배치의 포지션 기록이 전부
    깨졌을 것이다.
    """
    order = []
    mocker.patch("db.connection.init_schema", side_effect=lambda: order.append("schema"))
    mocker.patch.object(scheduler, "open_job", side_effect=lambda: order.append("open"))
    mocker.patch.object(scheduler, "daily_job", side_effect=lambda: order.append("daily"))

    scheduler.main(["--open"])
    assert order == ["schema", "open"], order

    order.clear()
    scheduler.main(["--now"])
    assert order == ["schema", "daily"], order


def test_worker_refuses_to_start_when_the_schema_cannot_be_applied(mocker):
    """스키마를 보장 못 하는 워커가 주문을 내면 안 된다."""
    mocker.patch("db.connection.init_schema", side_effect=RuntimeError("연결 실패"))
    started = mocker.patch.object(scheduler, "open_job")

    with pytest.raises(RuntimeError, match="연결 실패"):
        scheduler.main(["--open"])
    assert not started.called


def test_holdings_that_differ_from_the_target_are_corrected(live_run, mocker, capsys):
    """보유가 직전 목표와 다르면 달력을 기다리지 않는다.

    2026-08-25 13:10 보정 실행이 '리밸런싱일 아님 - 건너뜀'만 찍고 끝났다. 팔았어야
    할 3종목이 채웠어야 할 3종목의 자리를 차지해 보유가 정확히 10종목이었고, 게이트가
    종목 수만 세고 있어서 꽉 찬 것으로 봤다. 개수가 아니라 어떤 종목인지를 봐야 한다.
    """
    mocker.patch.object(scheduler, "_stored_targets",
                        return_value=({f"00{i:04d}" for i in range(SLOTS)}, "2026-08-21"))
    store = mocker.patch.object(scheduler, "_store_targets")
    # 목표 10종목 중 7개만 맞고 3개는 엉뚱한 종목을 들고 있다 - 개수는 10으로 같다.
    holdings = {f"00{i:04d}": {"name": f"종목{i}", "qty": 100.0, "cur_px": 10_000.0}
                for i in range(SLOTS - 3)}
    holdings.update({f"99{i:04d}": {"name": f"잔여{i}", "qty": 100.0, "cur_px": 10_000.0}
                     for i in range(3)})
    snap = _snapshot(holdings, 0, 10_000_000, 10_000_000, settled_cash=1_000_000)

    live_run(snap, rebal_d=None)

    out = capsys.readouterr().out
    assert "목표와 어긋나 보정" in out, out
    assert store.called, "이번 실행의 목표를 저장해야 다음 보정이 판단할 수 있다"


def test_matching_holdings_skip_outside_the_calendar(live_run, mocker, capsys):
    """보유가 목표와 같으면 건너뛴다 - 보정이 매일 리밸런싱이 되면 안 된다."""
    targets = {f"00{i:04d}" for i in range(SLOTS)}
    mocker.patch.object(scheduler, "_stored_targets", return_value=(targets, "2026-08-21"))
    holdings = {c: {"name": c, "qty": 100.0, "cur_px": 10_000.0} for c in targets}
    snap = _snapshot(holdings, 0, 10_000_000, 10_000_000, settled_cash=0)

    live_run(snap, rebal_d=None)

    assert "건너뜀" in capsys.readouterr().out
    assert live_run.orders == []


def test_correction_reuses_the_ranking_date_of_the_last_rebalance(live_run, mocker):
    """보정은 다시 랭킹하지 않는다 - 매일 다시 매기면 일간 리밸런싱이 된다."""
    from strategy import quality

    mocker.patch.object(scheduler, "_stored_targets", return_value=({"999999"}, "2026-08-03"))
    mocker.patch.object(scheduler, "_store_targets")
    holdings = {"000000": {"name": "종목", "qty": 100.0, "cur_px": 10_000.0}}
    snap = _snapshot(holdings, 0, 1_000_000, 10_000_000, settled_cash=1_000_000)

    live_run(snap, rebal_d=None)

    assert quality.get_targets.call_args[0][0] == "2026-08-03", (
        "직전 거래일이 아니라 직전 리밸런싱 기준일을 써야 한다")


def test_missing_target_record_is_treated_as_work_to_do(live_run, mocker, capsys):
    """목표 기록이 없으면 확인한다 - 없다는 것이 '할 일 없음'은 아니다.

    이 기능을 배포한 다음 날 아침이 그대로 조용히 지나갈 뻔했다. settings에 목표가
    아직 없어서 unfinished가 False가 됐고, 리밸런싱일도 아니고 미수도 아니라
    게이트가 닫혔다.
    """
    mocker.patch.object(scheduler, "_stored_targets", return_value=(set(), None))
    mocker.patch.object(scheduler, "_store_targets")
    holdings = {f"00{i:04d}": {"name": f"종목{i}", "qty": 100.0, "cur_px": 10_000.0}
                for i in range(SLOTS)}
    snap = _snapshot(holdings, 0, 10_000_000, 10_000_000, settled_cash=1_000_000)

    live_run(snap, rebal_d=None)

    assert "직전 목표 기록이 없어 확인" in capsys.readouterr().out
