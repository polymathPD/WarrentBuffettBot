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
    from strategy.contrarian import get_entry_candidates
    from agents.gate import decide
    from executor.paper import buy
    import db.connection as db

    candidates = get_entry_candidates(today)
    print(f"진입 후보: {len(candidates)}종목")

    for c in candidates:
        code = c["code"]
        gate = decide(code, today)
        if gate["approved"]:
            name_row = db.fetchone(
                "SELECT name FROM instruments WHERE code=%s", (code,)
            )
            name = name_row["name"] if name_row else code
            buy(code, name, today, c["close"], c["heat_score"], gate["agents"])
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
