"""
역발상 전략 워크포워드 검증

훈련/검증 고정 분할로 2025~2026을 이미 여러 번 열었다(가설 5개, 방향 3개,
팩터 9개). 홀드아웃은 소진됐고, 이제 그 구간 결과를 보고 규칙을 고르면 그 순간
정직한 점수가 아니다. 그래서 규칙 선택 자체를 자동화한 워크포워드로 간다.

  2022        -> 2023 시험
  2022~2023   -> 2024 시험
  2022~2024   -> 2025 시험
  2022~2025   -> 2026 시험

각 시험 구간의 랭킹 팩터는 그 앞 구간만 보고 정해진다. 선택 규칙은
'학습 구간 거래당 평균 최대'로 고정한다 — 사람이 결과를 보고 고르면 오염된다.

변형은 여섯 개 팩터의 오름차순뿐이다. 역발상은 '소외된 종목을 산다'가 전제이므로
낮은 순이 이 전략의 자연스러운 방향이고, 방향까지 열면 후보가 12개로 늘어
우연히 좋아 보이는 것이 생긴다.

대조군은 현행 운용 규칙(과열 점수 낮은 순)을 전 구간 고정으로 돌린 것이다.
선택이 실제로 값어치를 하는지 보려면 고정 규칙과 비교해야 한다.

재현: python research/contrarian_walkforward.py
"""
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import pandas as pd
import research.portfolio_backtest as pb
from backtester.fundamental_sim import stats
from backtester.store import save_run

SLOTS = 5
MAX_HOLD = 20
MIN_TRAIN_TRADES = 30      # 이보다 적으면 학습 결과를 믿지 않고 기본 팩터를 쓴다
DEFAULT_FACTOR = "heat_score"

FACTORS = ["heat_score", "individual_flow_ratio", "credit_surge_ratio",
           "volume_ratio", "foreign_flow_ratio", "institution_flow_ratio"]

WINDOWS = [
    ("2022-01-01", "2022-12-31", "2023-01-01", "2023-12-31"),
    ("2022-01-01", "2023-12-31", "2024-01-01", "2024-12-31"),
    ("2022-01-01", "2024-12-31", "2025-01-01", "2025-12-31"),
    ("2022-01-01", "2025-12-31", "2026-01-01", "2026-08-19"),
]
SPAN_START, SPAN_END = "2022-01-01", "2026-08-19"


def main():
    t0 = time.time()
    pb.START, pb.END = SPAN_START, SPAN_END
    pb.WARMUP = (pd.Timestamp(SPAN_START) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")

    codes = pb.universe()
    print(f"유니버스 {len(codes):,}종목  로딩 중...", flush=True)
    px, sig = pb.load(codes)
    px = pb.add_pos52w(px)
    px = pb.add_marcap(px)   # 시총 하한을 날짜별로 판정 (생존 편향 제거)
    print(f"  일봉 {len(px):,}행 / 신호 {len(sig):,}행 ({time.time()-t0:.0f}s)\n", flush=True)

    def sim(factor, start, end):
        return pb.run_sim(px, sig.copy(), factor, True, SLOTS, MAX_HOLD,
                          start=start, end=end)

    picked, oos, baseline = [], [], []

    for tr_s, tr_e, te_s, te_e in WINDOWS:
        scores = {f: stats(sim(f, tr_s, tr_e)) for f in FACTORS}
        usable = {f: s for f, s in scores.items() if s["n"] >= MIN_TRAIN_TRADES}
        best = max(usable, key=lambda f: usable[f]["mean_pct"]) if usable else DEFAULT_FACTOR

        test = sim(best, te_s, te_e)
        base = sim(DEFAULT_FACTOR, te_s, te_e)
        oos += test
        baseline += base
        picked.append({"test_year": te_s[:4], "factor": best,
                       "train": scores, "test": stats(test), "base": stats(base)})

        print(f"[{tr_s[:4]}~{tr_e[:4]} 학습 → {te_s[:4]} 시험]  선택: {best}")
        for f in FACTORS:
            s = scores[f]
            mark = " ←" if f == best else ""
            print(f"    학습 {f:24s} {s['n']:4d}건 {s.get('mean_pct', 0):+7.2f}%{mark}")
        t, b = stats(test), stats(base)
        print(f"    시험 결과   {t['n']:4d}건 {t.get('mean_pct',0):+7.2f}% (t {t.get('t_val',0):+.2f})"
              f"   |  고정 대조군 {b['n']:4d}건 {b.get('mean_pct',0):+7.2f}%\n", flush=True)

    print("=" * 70)
    for name, trades, strategy, selected in (
        ("워크포워드 선택", oos, "contrarian_walkforward", True),
        ("고정 대조군 (과열 점수 낮은 순)", baseline, "contrarian_walkforward_base", False),
    ):
        s = stats(trades)
        if not s["n"]:
            print(f"{name}: 거래 없음")
            continue
        print(f"{name}")
        print(f"  시험구간 합계 {s['n']:,}건  평균 {s['mean_pct']:+.2f}%  "
              f"승률 {s['win_rate']:.1f}%  t값 {s['t_val']:+.2f}")
        print(f"  평균 보유 {s['avg_held_days']:.1f}거래일  청산사유 {s['reasons']}")
        save_run(
            strategy, WINDOWS[0][2], WINDOWS[-1][3],
            {"slots": SLOTS, "max_hold_days": MAX_HOLD, "pos52w_filter": True,
             "stop_pct": 0.07, "walkforward": True, "windows": len(WINDOWS),
             "point_in_time_universe": True,
             "factor_pool": FACTORS if selected else [DEFAULT_FACTOR],
             "selection": "학습 구간 거래당 평균 최대" if selected else "고정",
             "picked": [p["factor"] for p in picked] if selected else []},
            s,
            [{"code": t["code"],
              "entry_d": pd.Timestamp(t["entry_d"]).date(),
              "exit_d": pd.Timestamp(t["exit_d"]).date(),
              "entry_px": t["entry_px"], "exit_px": t["exit_px"],
              "ret_pct": t["ret"], "exit_reason": t["reason"]} for t in trades])

    print(f"\n완료 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
