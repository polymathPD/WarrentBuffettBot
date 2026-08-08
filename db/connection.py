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

def execute(sql, params=None):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()

def executemany(sql, rows):
    conn = get_conn()
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows)
    conn.commit()

def fetchall(sql, params=None):
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()

def fetchone(sql, params=None):
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchone()

def init_schema():
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print("스키마 초기화 완료")
