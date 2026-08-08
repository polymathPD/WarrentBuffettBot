"""매매 통계 조회 유틸"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db.connection as db


def summary():
    stats = db.fetchone(
        """SELECT COUNT(*) FILTER (WHERE side='sell') AS total_trades,
                  ROUND(AVG(realized_pct) FILTER (WHERE side='sell') * 100, 2) AS avg_pct,
                  ROUND(STDDEV(realized_pct) FILTER (WHERE side='sell') * 100, 2) AS std_pct,
                  COUNT(*) FILTER (WHERE side='sell' AND realized_pct > 0) AS wins,
                  MIN(realized_pct) FILTER (WHERE side='sell') AS max_loss,
                  MAX(realized_pct) FILTER (WHERE side='sell') AS max_gain
           FROM trades"""
    )
    if not stats or not stats["total_trades"]:
        print("매매 기록 없음")
        return

    total = int(stats["total_trades"])
    wins = int(stats["wins"])
    avg = float(stats["avg_pct"] or 0)
    std = float(stats["std_pct"] or 0)
    win_rate = wins / total * 100 if total > 0 else 0

    print(f"=== 성과 요약 ===")
    print(f"총 매매: {total}건  승률: {win_rate:.1f}%")
    print(f"평균 수익률: {avg:+.2f}%  표준편차: {std:.2f}%")
    if std > 0:
        t_val = avg / (std / (total ** 0.5))
        print(f"t값: {t_val:.2f}  (|t|≥2면 통계적 유의)")
    print(f"최대 손실: {float(stats['max_loss'] or 0)*100:+.2f}%  최대 이익: {float(stats['max_gain'] or 0)*100:+.2f}%")

    # 보유 중
    held = db.fetchall("SELECT code, name, entry_date, entry_px FROM positions")
    if held:
        print(f"\n현재 보유: {len(held)}종목")
        for p in held:
            print(f"  {p['code']} {p['name']}  진입일={p['entry_date']}  진입가={float(p['entry_px']):,.0f}")


if __name__ == "__main__":
    summary()
