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
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception:
        _recover(conn)
        raise

def fetchone(sql, params=None):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    except Exception:
        _recover(conn)
        raise

def init_schema():
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print("스키마 초기화 완료")
