"""
성과 평가: MDD, 승률, 슬리피지 실측, 기간 분리 t값
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import db.connection as db


def mdd(equity_curve: np.ndarray) -> float:
    """최대 낙폭 계산"""
    peak = np.maximum.accumulate(equity_curve)
    drawdown = equity_curve / peak - 1
    return float(drawdown.min())


def bootstrap_positive_rate(returns: np.ndarray, n_iter: int = 1000) -> float:
    """부트스트랩: 재추출 시 양수 수익률 비율"""
    pos = 0
    n = len(returns)
    for _ in range(n_iter):
        sample = np.random.choice(returns, size=n, replace=True)
        if sample.mean() > 0:
            pos += 1
    return pos / n_iter


def full_report():
    rows = db.fetchall(
        "SELECT ts, realized_pct FROM trades WHERE side='sell' AND realized_pct IS NOT NULL ORDER BY ts"
    )
    if len(rows) < 5:
        print("평가에 필요한 매매 수 부족 (최소 5건)")
        return

    rets = np.array([float(r["realized_pct"]) for r in rows])
    n = len(rets)
    mean_r = rets.mean() * 100
    std_r = rets.std() * 100
    t_val = (rets.mean() / (rets.std() / np.sqrt(n))) if rets.std() > 0 else 0

    # 기간 분리
    half = n // 2
    t1_mean = rets[:half].mean() * 100
    t2_mean = rets[half:].mean() * 100

    # 자산 곡선 (단순 누적)
    equity = np.cumprod(1 + rets)
    _mdd = mdd(equity) * 100

    # 부트스트랩 (50건 미만이면 생략)
    if n >= 50:
        bp = bootstrap_positive_rate(rets) * 100
        bootstrap_str = f"부트스트랩 양수율: {bp:.1f}%"
    else:
        bootstrap_str = f"부트스트랩: 건수 부족({n}<50)"

    win_rate = (rets > 0).mean() * 100

    print("=" * 45)
    print(f"총 매매: {n}건   승률: {win_rate:.1f}%")
    print(f"평균 수익률: {mean_r:+.2f}%   표준편차: {std_r:.2f}%")
    print(f"t값: {t_val:.2f}   (|t|≥2 = 통계적 유의)")
    print(f"기간 전반 평균: {t1_mean:+.2f}%   후반: {t2_mean:+.2f}%")
    print(f"MDD: {_mdd:.1f}%")
    print(bootstrap_str)
    print("=" * 45)


if __name__ == "__main__":
    full_report()
