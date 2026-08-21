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

    # 6. 퀄리티 전략 월별 리밸런싱
    #    직전 거래일이 그 달 첫 거래일일 때만 돈다.
    #
    #    비중까지 동일가중으로 되돌린다. 이게 이 전략의 물타기다 — 빠진 종목은
    #    비중이 줄어드니 다시 채우려면 더 사고, 오른 종목은 덜어낸다. 별도 현금
    #    없이도 매달 자동으로 일어난다. 명단 교체만 하면 검증한 것과 달라진다
    #    (백테스트는 매 구간 동일가중으로 되돌린 수익률을 쓴다).
    from strategy import quality
    from agents import value_trap
    from executor.paper import adjust, current_equity
    import db.connection as db
    import config

    def name_of(code):
        row = db.fetchone("SELECT name FROM instruments WHERE code=%s", (code,))
        return row["name"] if row else code

    rebal_d = _quality_rebalance_date(today)
    if rebal_d is None:
        print("퀄리티: 리밸런싱일 아님 - 건너뜀")
    else:
        slots = config.get_setting("SLOTS")
        opens = {r["code"]: float(r["o"]) for r in db.fetchall(
            "SELECT code, o FROM stock_daily WHERE d = %s::date AND o > 0", (today,))}

        # 랭킹 상위부터 가치 함정 판별을 거쳐 슬롯을 채운다. 반려되면 다음 순위로
        # 넘어간다 — 검증은 항상 슬롯을 다 채운 상태로 쟀으므로 빈 슬롯을 두면
        # 그것대로 검증한 것과 달라진다.
        #
        # 게이트는 반려만 할 수 있고 없던 종목을 넣지는 못한다. 순위(PER·PBR·ROE)는
        # 에이전트에 넘기지 않는다 — 필터가 이미 쓴 지표를 되물으면 동어반복이다.
        ranked = [t for t in quality.get_targets(rebal_d, slots, limit=slots * 3)
                  if t["code"] in opens]
        picked, rejected = [], []
        for t in ranked:
            if len(picked) >= slots:
                break
            v = value_trap.analyze(t["code"], rebal_d)
            # 호출 실패는 '관망'이 아니라 '판단 없음'이다. 크레딧이 떨어진 날
            # 포트폴리오가 통째로 비는 쪽이 더 나쁘므로 통과시키고 기록만 남긴다.
            if v.get("error") or v["decision"] == "매수":
                t["agents"] = {"value_trap": {k: v.get(k) for k in
                                              ("decision", "score", "rationale", "error")},
                               "rank": quality.RANK_KIND, "score": round(t["score"], 4),
                               "per": round(t["per"], 1), "pbr": round(t["pbr"], 2)}
                picked.append(t)
            else:
                rejected.append((t["code"], v["decision"], v.get("rationale", "")))

        for code, dec, why in rejected:
            print(f"  [반려] {code}: value_trap {dec} - {why[:60]}")
        targets = picked
        tgt = {t["code"]: t for t in targets}
        print(f"퀄리티 리밸런싱({rebal_d} 기준 -> {today} 시가 체결): "
              f"목표 {len(tgt)}종목")

        if not tgt:
            print("  목표 종목 없음 - 건너뜀")
        else:
            # 목표 금액은 현재 자산 기준이다. CAPITAL 고정으로 잡으면 자산이 늘어도
            # 투입액이 그대로여서 현금 비중이 저절로 커진다.
            equity = current_equity(quality.STRATEGY, opens)
            slot_value = equity / slots
            print(f"  자산 {equity:,.0f}원 / {slots}슬롯 = 슬롯당 {slot_value:,.0f}원")

            held = db.fetchall(
                "SELECT code, name, qty FROM positions WHERE strategy=%s AND mode='paper'",
                (quality.STRATEGY,))
            for h in held:
                if h["code"] in tgt or h["code"] not in opens:
                    continue
                adjust(h["code"], h["name"], 0, opens[h["code"]], quality.STRATEGY)

            for code, t in tgt.items():
                qty = int(slot_value // opens[code])
                if qty < 1:
                    print(f"  [보류] {code} - 1슬롯 금액으로 1주도 못 산다")
                    continue
                adjust(code, name_of(code), qty, opens[code], quality.STRATEGY,
                       t["agents"])

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
