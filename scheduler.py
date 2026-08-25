"""
APScheduler 메인 스케줄러
매일 장 마감 후 자동 실행:
  16:10 → 일봉 수집 → 수급 수집 → 신호 계산 → 에이전트 판단 → 모의 주문
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import date
import logging

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def name_of(code: str) -> str:
    import db.connection as db
    row = db.fetchone("SELECT name FROM instruments WHERE code=%s", (code,))
    return row["name"] if row else code


def _entry_signal_date(today: str):
    """
    오늘 시가로 체결할 진입 신호일을 고른다.

    체결 규칙은 "신호일 다음 거래일 시가"(backtester/engine.py와 동일)다.
    따라서 오늘 계산한 신호는 내일 시가로 체결되는데, 오늘 16:10 시점에는 내일 봉이
    아직 없으므로 executor.paper._next_open()이 None을 반환해 매수가 전부 거부된다.
    오늘 체결할 대상은 '직전 거래일 신호'다.

    직전 신호일의 다음 거래일이 오늘이 아니면(신호 계산이 며칠 밀린 경우)
    이미 지나간 날 시가로 체결하게 되므로 건너뛴다.
    """
    import db.connection as db

    row = db.fetchone(
        "SELECT MAX(d) AS d FROM contrarian_signals WHERE d < %s::date", (today,)
    )
    if not row or not row["d"]:
        return None
    signal_date = row["d"].strftime("%Y-%m-%d")

    fill = db.fetchone(
        "SELECT MIN(d) AS d FROM stock_daily WHERE d > %s::date", (signal_date,)
    )
    if not fill or not fill["d"] or fill["d"].strftime("%Y-%m-%d") != today:
        print(f"진입 건너뜀: {signal_date} 신호의 체결일이 오늘({today})이 아님")
        return None
    return signal_date


def _prev_trading_day(today: str):
    """오늘 시가로 체결할 신호일. 일봉 기준 직전 거래일이다.

    공시는 장중·장마감 후 아무 때나 나오므로, 그날 공시를 그날 시가로 체결했다고
    보면 미래를 참조하게 된다. 직전 거래일 공시를 오늘 시가로 체결한다.
    """
    import db.connection as db

    row = db.fetchone("SELECT MAX(d) AS d FROM stock_daily WHERE d < %s::date", (today,))
    return row["d"].strftime("%Y-%m-%d") if row and row["d"] else None


def _quality_rebalance_date(today: str):
    """오늘 시가로 체결할 퀄리티 전략 리밸런싱 기준일.

    체결 규칙은 '기준일 다음 거래일 시가'다(research/quality_backtest.py와 동일).
    16:10 배치 시점에는 내일 봉이 없으므로, 오늘 체결할 대상은 '직전 거래일'이고
    그 직전 거래일이 그 달의 첫 거래일일 때만 리밸런싱한다.
    """
    import db.connection as db

    prev = _prev_trading_day(today)
    if prev is None:
        return None
    row = db.fetchone(
        """SELECT MIN(d) AS d FROM stock_daily
            WHERE d >= date_trunc('month', %s::date) AND d <= %s::date""",
        (prev, prev),
    )
    if not row or not row["d"] or row["d"].strftime("%Y-%m-%d") != prev:
        return None
    return prev


def open_job():
    """개장 직후 리밸런싱 — 결정은 직전 거래일 데이터로, 체결은 오늘 시가 근처로.

    16:10 배치에서 떼어낸 이유는 체결 시각 때문이다. 검증한 규칙이 '신호일 다음
    거래일 시가'이고 모의투자 주문은 장중에만 체결된다. 마감 뒤에 주문을 낼 수는
    없다.

    KIS_MODE=live면 증권사에 실제 주문을 내고 잔고로 체결을 확인한다. paper면
    DB 시뮬레이션이다. 지금까지 이 분기가 없어서 KIS_MODE는 읽히기만 하고
    아무것도 결정하지 않았다.
    """
    today = date.today().strftime("%Y-%m-%d")
    print(f"=== 개장 리밸런싱 {today} ({config.KIS_MODE}) ===")

    import db.connection as db
    from strategy import quality
    from agents import value_trap, market_state, risk, disclosure, financials

    # 계좌가 비어 있으면 달력을 기다리지 않는다. 월 첫 거래일 규칙은 회전율을
    # 낮추려는 것이지 빈 계좌를 놀리려는 게 아니다 — 전략을 새로 붙이거나 계좌를
    # 옮긴 직후에는 다음 리밸런싱까지 몇 주가 빌 수 있다.
    slots = config.get_setting("SLOTS")
    snap = None
    if config.KIS_MODE == "live":
        from executor import live
        try:
            snap = live.account_snapshot()
        except Exception as e:
            print(f"잔고 조회 실패 - 건너뜀: {type(e).__name__} {str(e)[:100]}")
            return
        held = len(snap["holdings"])
        empty = not held
        # 미수(외상매수)는 T+2에 반대매매로 끝난다. 달력을 기다리면 늦는다.
        # 2026-08-24에 재실행이 겹쳐 10M 계좌가 26.7M을 들고 -16.4M 미수가 났는데,
        # 다음 리밸런싱일이 9월 1일이라 그때까지 방치될 뻔했다.
        in_debt = snap["settled_cash"] < 0
        if in_debt:
            print(f"  미수 {snap['settled_cash']:,.0f}원 - 달력과 무관하게 정리한다")
    else:
        in_debt = False
        held = db.fetchone(
            "SELECT COUNT(*) c FROM positions WHERE strategy=%s AND mode='paper'",
            (quality.STRATEGY,))["c"]
        empty = not held

    # 슬롯이 비어 있으면 앞선 실행이 끝까지 가지 못한 것이다. 12:00 보정 실행이
    # 있는 이유가 이것인데, 2026-08-25에는 게이트에 이 조건이 없어 그냥 건너뛰었다 —
    # 아침 배치가 주문 500으로 죽어 5슬롯이 빈 채였고 미수는 이미 털린 뒤였다.
    under_filled = 0 < held < slots

    rebal_d = _quality_rebalance_date(today)
    if rebal_d is None:
        if not empty and not in_debt and not under_filled:
            print("리밸런싱일 아님 - 건너뜀")
            return
        rebal_d = _prev_trading_day(today)
        if rebal_d is None:
            print("직전 거래일 없음 - 건너뜀")
            return
        why = ("미수 정리" if in_debt
               else "보유가 없어 최초 편입" if empty
               else f"슬롯 {slots - held}개가 비어 보정")
        print(f"리밸런싱일은 아니지만 {why}를 진행한다 ({rebal_d} 기준)")

    def _op(v):
        return {k: v.get(k) for k in ("decision", "score", "rationale", "error")}

    ranked = quality.get_targets(rebal_d, slots, limit=slots * 3)
    picked, rejected = [], []
    for t in ranked:
        if len(picked) >= slots:
            break
        v = value_trap.analyze(t["code"], rebal_d)
        if v.get("error") or v["decision"] == "매수":
            t["agents"] = {"value_trap": _op(v)}
            t["metrics"] = {"랭킹": quality.RANK_KIND, "점수": round(t["score"], 4),
                            "PER": round(t["per"], 1), "PBR": round(t["pbr"], 2)}
            picked.append(t)
        else:
            rejected.append((t["code"], v["decision"], v.get("rationale", "")))
    for code, dec, why in rejected:
        print(f"  [반려] {code}: value_trap {dec} - {why[:60]}")

    for t in picked:
        for nm, fn in (("market_state", lambda c: market_state.analyze(c, rebal_d, quality.STRATEGY)),
                       ("risk", lambda c: risk.analyze(c, rebal_d, quality.STRATEGY)),
                       ("disclosure", lambda c: disclosure.analyze(c, rebal_d)),
                       ("financials", lambda c: financials.analyze(c, rebal_d))):
            try:
                t["agents"][nm] = _op(fn(t["code"]))
            except Exception as e:
                t["agents"][nm] = {"decision": "오류", "error": str(e)[:120]}

    tgt = {t["code"]: t for t in picked}
    print(f"목표 {len(tgt)}종목 ({rebal_d} 기준)")

    if config.KIS_MODE == "live":
        from executor import live
        # KIS 모의는 매수해도 dnca_tot_amt(예수금)가 안 줄어든다. 이걸 그대로 쓰면
        # 재실행마다 잔고가 부풀어 slot_value가 커지고, 새 매수가 미수(외상)로 뚫린다.
        # 2026-08-24에 실제로 벌어진 일이다 — 10M 계좌로 26.7M어치를 사서 미수
        # -16.4M이 났다. 브로커가 계산한 nass_amt(순자산)이 유일한 정답이다.
        equity = snap["total_equity"]
        print(f"  잔고: 예수금 {snap['cash']:,.0f} / 평가 {snap['positions_value']:,.0f} / 순자산 {equity:,.0f}")
        slot_value = equity / slots
        # 매도를 먼저 전부 낸 뒤 매수로 넘어간다. 랭킹 순서대로 섞어 내면 매도 대금이
        # 아직 안 잡힌 상태에서 매수가 미수 가드에 걸려 그대로 건너뛰어진다 —
        # 2026-08-25 리밸런싱을 미리 돌려 보니 결제예정이 +88만인 시점에 100만원짜리
        # 매수가 걸려 슬롯 하나가 빈 채로 끝났다. 큰 매도부터 내면 여유가 먼저 생긴다.
        plan = []
        for code, h in snap["holdings"].items():
            if code not in tgt:
                plan.append((code, h["name"], 0, None))
        for code, t in tgt.items():
            px = snap["holdings"].get(code, {}).get("cur_px") or t["close"]
            qty = int(slot_value // px)
            if qty < 1:
                print(f"  [보류] {code} - 1슬롯 금액으로 1주도 못 산다")
                continue
            plan.append((code, name_of(code), qty,
                         dict(t["agents"], _metrics=t["metrics"])))

        def _delta_amount(p):
            code, _, qty, _agents = p
            h = snap["holdings"].get(code, {})
            px = h.get("cur_px") or (tgt[code]["close"] if code in tgt else 0.0)
            return (qty - h.get("qty", 0.0)) * px

        # 종목 하나가 실패해도 나머지는 낸다. 2026-08-25에 매도 8건을 낸 뒤
        # 다음 주문이 500을 받았고, 그 예외가 여기까지 올라와 남은 매도와 매수
        # 5건이 통째로 사라졌다. 계좌는 미수를 절반만 턴 채로 남았다.
        # 수집기(collector/investor_flow.py)가 종목별로 격리하는 것과 같은 이유다.
        failed = []
        for code, nm, qty, agents in sorted(plan, key=_delta_amount):
            try:
                live.adjust(code, nm, qty, quality.STRATEGY, snap, agents)
            except Exception as e:
                failed.append(code)
                print(f"  [주문 실패] {code} {nm} - {type(e).__name__} {str(e)[:100]}")
        if failed:
            print(f"  {len(failed)}종목 실패: {', '.join(failed)} - 다음 실행에서 다시 맞춘다")

        # 주문을 다 낸 뒤 증권사 잔고로 positions를 맞춘다. adjust()는 체결을 못 보면
        # 아무것도 기록하지 않고 끝나므로, 산 종목이 DB에서 통째로 빠질 수 있다.
        try:
            fixed = live.reconcile_positions(
                live.account_snapshot(), quality.STRATEGY,
                {c: dict(t["agents"], _metrics=t["metrics"]) for c, t in tgt.items()})
            if fixed:
                print(f"  잔고와 어긋나 바로잡음: {', '.join(fixed)}")
        except Exception as e:
            print(f"  [잔고 대조 실패] {type(e).__name__} {str(e)[:100]}")
    else:
        from executor.paper import adjust, current_equity
        opens = {r["code"]: float(r["o"]) for r in db.fetchall(
            "SELECT code, o FROM stock_daily WHERE d = %s::date AND o > 0", (today,))}
        tgt = {c: t for c, t in tgt.items() if c in opens}
        if not tgt:
            print("  오늘 시가 없음 - 건너뜀")
            return
        equity = current_equity(quality.STRATEGY, opens)
        slot_value = equity / slots
        for h in db.fetchall("SELECT code, name, qty FROM positions "
                             "WHERE strategy=%s AND mode='paper'", (quality.STRATEGY,)):
            if h["code"] not in tgt and h["code"] in opens:
                adjust(h["code"], h["name"], 0, opens[h["code"]], quality.STRATEGY)
        for code, t in tgt.items():
            qty = int(slot_value // opens[code])
            if qty >= 1:
                adjust(code, name_of(code), qty, opens[code], quality.STRATEGY,
                       dict(t["agents"], _metrics=t["metrics"]))

    # 자산 스냅샷 실패가 주문 결과까지 실패로 보이게 하면 안 된다.
    try:
        from recorder.equity import snapshot as eq_snapshot
        eq_snapshot(today)
    except Exception as e:
        print(f"  [자산 스냅샷 실패] {type(e).__name__} {str(e)[:100]}")


def daily_job():
    today = date.today().strftime("%Y-%m-%d")
    print(f"\n{'='*50}")
    print(f"[스케줄러] 일일 실행 시작: {today}")

    # 1. 일봉 수집
    from collector.stock_daily import collect as collect_daily
    collect_daily()

    # 2. 수급 수집
    from collector.investor_flow import collect as collect_flow
    collect_flow()

    # 3. 신용잔고 (KIS API)
    from collector.credit_balance import collect_kis
    collect_kis()

    # 3-1. 공시 (DART) — 마지막 수집일부터 오늘까지 이어받는다
    from collector.disclosure import collect as collect_disclosure
    collect_disclosure()

    # 3-2. 재무 (DART) — 분기마다 갱신되지만 전 종목이 40여 회 호출이라 매일 확인한다
    from collector.financials import collect as collect_financials
    collect_financials()

    # 4. 신호 계산
    from processor.signals import compute_for_date
    compute_for_date(today)

    # 5. 청산 후보 처리 (전략별로 규칙이 다르다)
    #
    # 역발상(contrarian)은 뺐다. 생존 편향을 제거한 워크포워드에서 거래당 -0.78%,
    # 고정 대조군(-2.37%)은 이겼지만 부호가 음수였고, 랭킹 기준으로 쓰던 heat_score는
    # 대조군 t -3.40으로 해롭다는 것이 확정됐다. research/README.md 참고.
    # 퀄리티 전략은 손절도 보유기한도 없다 — 청산은 월별 리밸런싱이 결정한다.
    from strategy import fundamental
    from executor.paper import sell

    for e in fundamental.get_exit_candidates(today):
        sell(e["code"], e["name"], e["qty"], e["entry_px"], e["close"],
             e["reason"], fundamental.STRATEGY)

    # 6. 퀄리티 리밸런싱은 여기서 하지 않는다.
    #    체결 규칙이 '신호일 다음 거래일 시가'인데 이 배치는 16:10, 장 마감 뒤다.
    #    open_job()이 다음 거래일 09:05에 직전 거래일 데이터로 결정하고 주문한다.

    # 6-2. 펀더멘털 진입: 직전 거래일 공시를 오늘 시가로 체결
    prev_day = _prev_trading_day(today)
    if prev_day is None:
        print("펀더멘털 진입: 직전 거래일 없음 - 건너뜀")
    else:
        from executor.paper import free_slots, buy
        from agents.gate import decide_fundamental
        f_candidates = fundamental.get_entry_candidates(prev_day)
        print(f"펀더멘털 후보({prev_day} 공시 -> {today} 시가 체결): {len(f_candidates)}종목")
        for i, c in enumerate(f_candidates):
            if free_slots(fundamental.STRATEGY) <= 0:
                print(f"  슬롯 소진 - 남은 후보 {len(f_candidates) - i}종목 건너뜀")
                break
            code = c["code"]
            gate = decide_fundamental(code, prev_day, fundamental.STRATEGY)
            if gate["approved"]:
                buy(code, name_of(code), prev_day, c["close"], 0.0,
                    gate["agents"], fundamental.STRATEGY)
            else:
                print(f"  [반려] {code}: {gate['reason']}")

    # 7. 자산 스냅샷 + 성과 출력
    from recorder.equity import snapshot
    snapshot(today)

    from recorder.trade_log import summary
    summary()


def main(argv: list[str] | None = None) -> None:
    """워커 진입점. 무슨 일을 하든 스키마부터 맞춘다.

    db.init_schema()는 setup_db.py에서만 불렸다 — 손으로 돌리는 스크립트라
    배포에는 끼어들지 않는다. 2026-08-25에 positions.agents 컬럼을 추가하고
    배포했더니 운영 DB에는 그 컬럼이 없어서, 손으로 setup_db.py를 돌리지
    않았다면 그날 12:00 배치의 모든 포지션 기록이 실패할 뻔했다.
    schema.sql은 전부 IF NOT EXISTS라 매번 돌려도 안전하다.

    실패하면 그대로 죽인다. 스키마를 보장 못 하는 워커가 주문을 내면 안 된다.
    """
    import sys
    import db.connection as db

    argv = sys.argv[1:] if argv is None else argv
    db.init_schema()

    if argv and argv[0] == "--now":
        daily_job()                      # 즉시 한 번 실행 (테스트용)
        return
    if argv and argv[0] == "--open":
        open_job()
        return

    scheduler = BlockingScheduler(timezone="Asia/Seoul")
    # 평일 16:10 — 수집·신호 계산 (장 마감 뒤)
    scheduler.add_job(daily_job, CronTrigger(
        day_of_week="mon-fri", hour=16, minute=10, timezone="Asia/Seoul"
    ))
    # 평일 09:05 — 리밸런싱 주문. 검증한 체결 규칙이 "기준일 다음 거래일
    # 시가"이므로 개장에 최대한 붙인다.
    scheduler.add_job(open_job, CronTrigger(
        day_of_week="mon-fri", hour=9, minute=5, timezone="Asia/Seoul"
    ))
    # 평일 12:00 — 보정 실행. 하는 일은 같고, 이미 목표에 맞으면 주문이 나가지
    # 않는다(adjust가 차이만 낸다).
    #
    # 하루 한 번으로는 부족하다는 것이 이틀 연속 확인됐다. 2026-08-24는 미수로,
    # 2026-08-25는 주문 500 예외로 배치가 중간에 죽어 포트폴리오가 반쯤 완성된
    # 채 남았다. 에이전트 판단은 입력 해시로 캐시되므로 같은 날 두 번째 실행에
    # 추가 API 비용은 들지 않는다.
    scheduler.add_job(open_job, CronTrigger(
        day_of_week="mon-fri", hour=12, minute=0, timezone="Asia/Seoul"
    ))
    print("스케줄러 시작 — 평일 09:05·12:00 리밸런싱 / 16:10 수집 (Ctrl+C로 종료)")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("스케줄러 종료")


if __name__ == "__main__":
    main()
