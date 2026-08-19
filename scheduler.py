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
    from strategy import contrarian, fundamental
    from executor.paper import sell

    for strat in (contrarian, fundamental):
        for e in strat.get_exit_candidates(today):
            sell(e["code"], e["name"], e["qty"], e["entry_px"], e["close"],
                 e["reason"], strat.STRATEGY)

    # 6. 진입 후보 → 에이전트 판단 → 모의 매수
    #    직전 거래일 신호를 오늘 시가로 체결한다 (_entry_signal_date 참고)
    from agents.gate import decide, decide_fundamental
    from executor.paper import buy, free_slots
    import db.connection as db

    def name_of(code):
        row = db.fetchone("SELECT name FROM instruments WHERE code=%s", (code,))
        return row["name"] if row else code

    STRATEGY = contrarian.STRATEGY
    signal_date = _entry_signal_date(today)
    if signal_date is None:
        print("진입 후보: 체결 가능한 직전 거래일 신호 없음 — 매수 단계 건너뜀")
        candidates = []
    else:
        candidates = contrarian.get_entry_candidates(signal_date)
        print(f"진입 후보({signal_date} 신호 → {today} 시가 체결): {len(candidates)}종목")

    # 슬롯이 차면 남은 후보는 게이트에 올리지 않는다. risk 에이전트가 '여유 슬롯
    # 0개'로 반려해 주긴 하지만, 그건 산술 계산 하나를 LLM 호출로 대신하는 것이라
    # 후보 수만큼 크레딧이 나간다 (2026-08-19: 38종목이 전부 이 사유로 반려됐다).
    # 판단 결과는 달라지지 않는다 — 어차피 executor도 같은 조건으로 거부한다.
    for i, c in enumerate(candidates):
        if free_slots(STRATEGY) <= 0:
            print(f"  슬롯 소진 — 남은 후보 {len(candidates) - i}종목 건너뜀")
            break
        code = c["code"]
        gate = decide(code, signal_date, STRATEGY)
        if gate["approved"]:
            buy(code, name_of(code), signal_date, c["close"], c["heat_score"],
                gate["agents"], STRATEGY)
        else:
            print(f"  [반려] {code}: {gate['reason']}")

    # 6-2. 펀더멘털 진입: 직전 거래일 공시를 오늘 시가로 체결
    prev_day = _prev_trading_day(today)
    if prev_day is None:
        print("펀더멘털 진입: 직전 거래일 없음 - 건너뜀")
    else:
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
    else:
        scheduler = BlockingScheduler(timezone="Asia/Seoul")
        # 평일 16:10 실행
        scheduler.add_job(daily_job, CronTrigger(
            day_of_week="mon-fri", hour=16, minute=10, timezone="Asia/Seoul"
        ))
        print("스케줄러 시작 — 평일 16:10 자동 실행 (Ctrl+C로 종료)")
        try:
            scheduler.start()
        except KeyboardInterrupt:
            print("스케줄러 종료")
