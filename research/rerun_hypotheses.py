"""
기각된 다섯 가설 + 벤치마크를 '시점별 시가총액' 유니버스로 다시 잰다.

기존 백테스트는 FDR의 현재 시총으로 유니버스를 정했다. 두 가지가 잘못됐다.
  1. 생존 편향 — 상장폐지된 272종목(신호 122,819행, 그중 81%가 heat<0)이
     통째로 빠졌다. 52주 신저가에서 헤매다 상폐된 종목은 이 전략의 최악의
     결과인데 그걸 한 건도 세지 않았다.
  2. 재현 불가 — 3,000억 경계 ±10%에 97종목이 걸쳐 있어 실행 시각마다 답이
     달라졌다 (같은 설정이 -0.60%와 +1.47%로 나왔다).

이제 유니버스는 '한 번이라도 하한을 넘었을 수 있는 종목'이고, 실제 하한 판정은
run_sim이 날짜별 시총(그 시점에 공시돼 있던 발행주식수 x 종가)으로 한다.

편향의 방향은 정해져 있다 — 망한 종목을 빼고 셌으니 수익률이 부풀려져 있었다.
따라서 음수 결론은 그대로 살아남고, 양수 결론만 의심 대상이다.

주의: 2025~2026은 이미 소진된 홀드아웃이다. 여기서 판정이 뒤집혀도 그것은
'채택 근거'가 아니라 '워크포워드로 다시 설계해 볼 이유'일 뿐이다.

재현: python research/rerun_hypotheses.py
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

MAX_HOLD = 20
TRAIN = ("2022-01-01", "2024-12-31")
VALID = ("2025-01-01", "2026-08-19")
SPAN_START, SPAN_END = "2022-01-01", "2026-08-19"
NO_SLOT_CAP = 99999   # 슬롯이 안 묶이게 해 '신호 전건 매수' 벤치마크를 만든다

# (키, 표시명, run_sim 인자, 기존(편향) 수치 train/valid)
VARIANTS = [
    ("base12", "기준: heat 랭킹 12슬롯",
     dict(rank_col="heat_score", ascending=True, slots=12, use_pos52w=True),
     (-0.44, +0.62)),
    ("h1_slot5", "H1 슬롯 12→5",
     dict(rank_col="heat_score", ascending=True, slots=5, use_pos52w=True),
     (-0.95, +1.47)),
    ("h5_slot2", "H5 heat 랭킹 상위 1~2위만",
     dict(rank_col="heat_score", ascending=True, slots=2, use_pos52w=True),
     (-0.66, +6.21)),
    ("h2_credit", "H2 랭킹을 신용급증 오름차순",
     dict(rank_col="credit_surge_ratio", ascending=True, slots=5, use_pos52w=True),
     (-0.63, -0.71)),
    ("h3_no52w", "H3 52주 필터 제거",
     dict(rank_col="credit_surge_ratio", ascending=True, slots=5, use_pos52w=False),
     (-1.10, +0.49)),
    ("h4_exit", "H4 신용/기관 상위 10% 청산 추가",
     dict(rank_col="credit_surge_ratio", ascending=True, slots=5, use_pos52w=False,
          exit_rank_pct=0.90,
          exit_cols=("credit_surge_ratio", "institution_flow_ratio")),
     (-1.36, -0.02)),
    ("pool_52w", "벤치마크: 52주 하위 30% 전건 매수",
     dict(rank_col="heat_score", ascending=True, slots=NO_SLOT_CAP, use_pos52w=True),
     (-0.33, -0.17)),
    ("pool_all", "벤치마크: 유니버스 전건 매수",
     dict(rank_col="heat_score", ascending=True, slots=NO_SLOT_CAP, use_pos52w=False),
     (-0.44, +0.64)),
]


def main():
    t0 = time.time()
    pb.START, pb.END = SPAN_START, SPAN_END
    pb.WARMUP = (pd.Timestamp(SPAN_START) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")

    codes = pb.universe()
    print(f"유니버스 {len(codes):,}종목 (시점별 시총 기준) — 로딩 중...", flush=True)
    px, sig = pb.load(codes)
    px = pb.add_pos52w(px)
    px = pb.add_marcap(px)
    have_mc = px["marcap"].notna().mean() * 100
    print(f"  일봉 {len(px):,}행 / 신호 {len(sig):,}행 / 시총 산출 {have_mc:.1f}% "
          f"({time.time()-t0:.0f}s)\n", flush=True)

    rows = []
    for key, label, kw, old in VARIANTS:
        line = {"label": label, "old": old}
        for wname, (ws, we) in (("train", TRAIN), ("valid", VALID)):
            tr = pb.run_sim(px, sig.copy(), max_hold=MAX_HOLD,
                            start=ws, end=we, **kw)
            st = stats(tr)
            line[wname] = st
            save_run(f"pit_{key}_{wname}", ws, we,
                     dict(kw, max_hold_days=MAX_HOLD, stop_pct=0.07,
                          exit_cols=list(kw.get("exit_cols", ())),
                          point_in_time_universe=True),
                     st, [])
            print(f"  {label:32} {wname:5} n={st['n']:>6} "
                  f"평균 {st.get('mean_pct', 0):+6.2f}%  t {st.get('t_val', 0):+5.2f}",
                  flush=True)
        rows.append(line)

    print("\n" + "=" * 92)
    print(f"{'':32} {'훈련 22-24':>26} {'검증 25-26':>26}")
    print(f"{'':32} {'편향본 → 시점별':>26} {'편향본 → 시점별':>26}")
    print("=" * 92)
    for r in rows:
        ot, ov = r["old"]; ts, vs = r["train"], r["valid"]
        print(f"{r['label']:32} "
              f"{ot:+6.2f}% → {ts.get('mean_pct',0):+6.2f}% (t{ts.get('t_val',0):+5.2f}) "
              f"{ov:+6.2f}% → {vs.get('mean_pct',0):+6.2f}% (t{vs.get('t_val',0):+5.2f})")
    print("=" * 92)
    print(f"\n총 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
