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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


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

    rebal_d = _quality_rebalance_date(today)
    if rebal_d is None:
        print("리밸런싱일 아님 - 건너뜀")
        return

    def _op(v):
        return {k: v.get(k) for k in ("decision", "score", "rationale", "error")}

    def name_of(code):
        row = db.fetchone("SELECT name FROM instruments WHERE code=%s", (code,))
        return row["name"] if row else code

    slots = config.get_setting("SLOTS")
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
        snap = live.account_snapshot()
        equity = snap["cash"] + sum(h["qty"] * h["cur_px"] for h in snap["holdings"].values())
        print(f"  잔고: 예수금 {snap['cash']:,.0f} / 자산 {equity:,.0f}")
        slot_value = equity / slots
        for code, h in snap["holdings"].items():
            if code not in tgt:
                live.adjust(code, h["name"], 0, quality.STRATEGY, snap)
        for code, t in tgt.items():
            px = snap["holdings"].get(code, {}).get("cur_px") or t["close"]
            qty = int(slot_value // px)
            if qty < 1:
                print(f"  [보류] {code} - 1슬롯 금액으로 1주도 못 산다")
                continue
            live.adjust(code, name_of(code), qty, quality.STRATEGY, snap,
                        dict(t["agents"], _metrics=t["metrics"]))
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

    from recorder.equity import snapshot as eq_snapshot
    eq_snapshot(today)


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
        from executor.paper import free_slots
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


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--now":
        # 즉시 한 번 실행 (테스트용)
        daily_job()
    elif len(sys.argv) > 1 and sys.argv[1] == "--open":
        open_job()
    else:
        scheduler = BlockingScheduler(timezone="Asia/Seoul")
        # 평일 16:10 — 수집·신호 계산 (장 마감 뒤)
        scheduler.add_job(daily_job, CronTrigger(
            day_of_week="mon-fri", hour=16, minute=10, timezone="Asia/Seoul"
        ))
        # 평일 09:05 — 리밸런싱 주문 (개장 직후, 장중에만 체결되므로)
        scheduler.add_job(open_job, CronTrigger(
            day_of_week="mon-fri", hour=9, minute=5, timezone="Asia/Seoul"
        ))
        print("스케줄러 시작 — 평일 09:05 리밸런싱 / 16:10 수집 (Ctrl+C로 종료)")
        try:
            scheduler.start()
        except KeyboardInterrupt:
            print("스케줄러 종료")
