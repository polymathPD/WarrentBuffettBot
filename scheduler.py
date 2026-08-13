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

    # 4. 신호 계산
    from processor.signals import compute_for_date
    compute_for_date(today)

    # 5. 청산 후보 처리
    from strategy.contrarian import get_exit_candidates
    exits = get_exit_candidates(today)
    if exits:
        from executor.paper import sell
        for e in exits:
            sell(e["code"], e["name"], e["qty"], e["entry_px"], e["close"], e["reason"])

    # 6. 진입 후보 → 에이전트 판단 → 모의 매수
    #    직전 거래일 신호를 오늘 시가로 체결한다 (_entry_signal_date 참고)
    from strategy.contrarian import get_entry_candidates
    from agents.gate import decide
    from executor.paper import buy
    import db.connection as db

    signal_date = _entry_signal_date(today)
    if signal_date is None:
        print("진입 후보: 체결 가능한 직전 거래일 신호 없음 — 매수 단계 건너뜀")
        candidates = []
    else:
        candidates = get_entry_candidates(signal_date)
        print(f"진입 후보({signal_date} 신호 → {today} 시가 체결): {len(candidates)}종목")

    for c in candidates:
        code = c["code"]
        gate = decide(code, signal_date)
        if gate["approved"]:
            name_row = db.fetchone(
                "SELECT name FROM instruments WHERE code=%s", (code,)
            )
            name = name_row["name"] if name_row else code
            buy(code, name, signal_date, c["close"], c["heat_score"], gate["agents"])
        else:
            print(f"  [반려] {code}: {gate['reason']}")

    # 7. 성과 출력
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
