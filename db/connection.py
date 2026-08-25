import psycopg2
import psycopg2.extras
import threading
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

_local = threading.local()

def get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None or conn.closed:
        _local.conn = psycopg2.connect(config.DB_URL)
        _local.conn.autocommit = False
    return _local.conn

def _recover(conn):
    """실패한 쿼리 이후 트랜잭션을 롤백. 연결 자체가 죽어있으면(예: SSL drop)
    롤백도 실패하므로 이때는 연결을 강제로 버려서 다음 get_conn()이 재연결하게 한다."""
    try:
        conn.rollback()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None

def execute(sql, params=None):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    except Exception:
        _recover(conn)
        raise

def executemany(sql, rows):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, rows)
        conn.commit()
    except Exception:
        _recover(conn)
        raise

def fetchall(sql, params=None):
    """읽기도 트랜잭션을 연다. 끝나면 반드시 닫는다.

    psycopg2는 SELECT에도 트랜잭션을 시작하는데, 커밋하지 않으면 그 연결이
    'idle in transaction'으로 남아 락을 쥔다. 2026-08-25에 대시보드 연결이 한 시간,
    진단 스크립트 연결이 한 시간 반 그 상태로 있어서 ALTER TABLE이 10분 넘게 막혔다.
    워커 시작 시 스키마 마이그레이션을 돌리도록 바꾼 뒤라, 그대로 두면 대시보드가
    조회 한 번 한 것만으로 워커가 영영 못 뜰 수 있었다.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        conn.commit()
        return rows
    except Exception:
        _recover(conn)
        raise

def fetchone(sql, params=None):
    """읽기 트랜잭션을 닫는 이유는 fetchall()의 주석 참고."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
        return row
    except Exception:
        _recover(conn)
        raise

SCHEMA_LOCK_TIMEOUT = "30s"


def init_schema():
    """schema.sql을 적용한다. 락에 걸리면 기다리지 않고 실패한다.

    워커가 뜰 때마다 도는 경로라, 다른 연결이 테이블을 쥐고 있으면 무한정 기다리게
    된다. 30초 안에 못 잡으면 예외를 내고 죽는 편이 낫다 — 크래시 루프는 눈에
    보이지만 조용히 멈춘 워커는 안 보인다.
    """
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET lock_timeout = '{SCHEMA_LOCK_TIMEOUT}'")
            cur.execute(sql)
            cur.execute("SET lock_timeout = 0")
        conn.commit()
    except Exception:
        _recover(conn)
        raise
    print("스키마 초기화 완료")
