"""
참조 DB → 내 Railway DB 마이그레이션

복사 대상:
  1. instruments  (종목 메타)
  2. stock_daily  (OHLCV, 860K행)
  3. investor_flow (stock_flow 기반 파생: individual_net = -(foreign_net + inst_net))
"""
import os, sys, time
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

load_dotenv()

SRC_URL = "postgresql://postgres:OOcMdAMhSvFryGWkYqiuDxJMQBajxYgu@hayabusa.proxy.rlwy.net:46481/railway"
DST_URL = os.getenv("DB_URL")

if not DST_URL:
    sys.exit("ERROR: .env에 DB_URL이 없습니다.")

BATCH = 5000  # 한 번에 삽입할 행 수


def connect(url, label):
    print(f"  연결 중: {label} ...", end=" ", flush=True)
    conn = psycopg2.connect(url)
    print("OK")
    return conn


def migrate_instruments(src, dst):
    print("\n[1/3] instruments 마이그레이션")
    cur_s = src.cursor(cursor_factory=RealDictCursor)
    cur_d = dst.cursor()

    cur_s.execute("SELECT code, name FROM instruments ORDER BY code")
    rows = cur_s.fetchall()
    data = [(r["code"], r["name"]) for r in rows]

    execute_values(
        cur_d,
        """INSERT INTO instruments (code, name)
           VALUES %s
           ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name""",
        data,
    )
    dst.commit()
    print(f"  완료: {len(data):,}개 종목")


def migrate_stock_daily(src, dst):
    print("\n[2/3] stock_daily 마이그레이션 (시간 걸림)")
    cur_s = src.cursor(cursor_factory=RealDictCursor)
    cur_d = dst.cursor()

    cur_s.execute("SELECT COUNT(*) AS n FROM stock_daily")
    total = cur_s.fetchone()["n"]
    print(f"  총 {total:,}행 → 배치 {BATCH}행씩 삽입")

    cur_s.execute(
        "SELECT code, d, o, h, l, c, v FROM stock_daily ORDER BY d, code"
    )

    inserted = 0
    t0 = time.time()
    while True:
        rows = cur_s.fetchmany(BATCH)
        if not rows:
            break
        data = [(r["code"], r["d"], r["o"], r["h"], r["l"], r["c"], r["v"]) for r in rows]
        execute_values(
            cur_d,
            """INSERT INTO stock_daily (code, d, o, h, l, c, v)
               VALUES %s
               ON CONFLICT (code, d) DO NOTHING""",
            data,
        )
        dst.commit()
        inserted += len(data)
        elapsed = time.time() - t0
        pct = inserted / total * 100
        print(f"  {inserted:,}/{total:,} ({pct:.1f}%) {elapsed:.0f}s", end="\r")

    print(f"\n  완료: {inserted:,}행 삽입 ({time.time()-t0:.0f}s)")


def migrate_investor_flow(src, dst):
    print("\n[3/3] investor_flow 마이그레이션 (stock_flow 기반 파생)")
    print("  개인 순매수 = -(외국인 + 기관)  [한국 증시 항등식]")

    cur_s = src.cursor(cursor_factory=RealDictCursor)
    cur_d = dst.cursor()

    cur_s.execute("SELECT COUNT(*) AS n FROM stock_flow")
    total = cur_s.fetchone()["n"]
    print(f"  총 {total:,}행")

    cur_s.execute(
        "SELECT code, d, foreign_net, inst_net FROM stock_flow ORDER BY d, code"
    )

    inserted = 0
    t0 = time.time()
    while True:
        rows = cur_s.fetchmany(BATCH)
        if not rows:
            break
        data = []
        for r in rows:
            foreign = r["foreign_net"] or 0
            inst    = r["inst_net"] or 0
            indiv   = -(foreign + inst)
            data.append((r["code"], r["d"], indiv, foreign, inst))

        execute_values(
            cur_d,
            """INSERT INTO investor_flow (code, d, individual_net, foreign_net, institution_net)
               VALUES %s
               ON CONFLICT (code, d) DO NOTHING""",
            data,
        )
        dst.commit()
        inserted += len(data)
        pct = inserted / total * 100
        print(f"  {inserted:,}/{total:,} ({pct:.1f}%)", end="\r")

    print(f"\n  완료: {inserted:,}행 삽입 ({time.time()-t0:.0f}s)")


def verify(dst):
    print("\n=== 검증 ===")
    cur = dst.cursor(cursor_factory=RealDictCursor)
    for table in ("instruments", "stock_daily", "investor_flow"):
        cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
        n = cur.fetchone()["n"]
        print(f"  {table}: {n:,}행")


if __name__ == "__main__":
    print("=== 참조 DB → 내 Railway DB 마이그레이션 ===\n")
    src = connect(SRC_URL, "참조 DB (source)")
    dst = connect(DST_URL, "내 Railway DB (target)")

    try:
        migrate_instruments(src, dst)
        migrate_stock_daily(src, dst)
        migrate_investor_flow(src, dst)
        verify(dst)
        print("\n마이그레이션 완료!")
    except Exception as e:
        dst.rollback()
        print(f"\nERROR: {e}")
        raise
    finally:
        src.close()
        dst.close()
