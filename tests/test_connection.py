"""
db/connection.py 회귀 테스트.

2026-08-10에 실제로 겪은 장애: 쿼리 하나가 실패했을 때 롤백을 안 해서
커넥션이 "aborted transaction" 상태로 남아 이후 모든 쿼리가 연쇄로 실패했고,
그 SSL 커넥션 자체가 끊긴 경우 롤백조차 실패해 그 예외가 또 다른 예외를
가려버리는 문제가 있었다. execute/fetchone 등은 실패 시 반드시 롤백하고,
롤백조차 실패하면 커넥션을 버리고 다음 호출에서 재연결해야 한다.
"""
import pytest

import db.connection as dbconn


@pytest.fixture(autouse=True)
def reset_local_conn():
    """threading.local()이 테스트 간 상태를 들고 있지 않도록 매번 초기화."""
    dbconn._local.conn = None
    yield
    dbconn._local.conn = None


def _make_fake_conn(mocker):
    conn = mocker.MagicMock()
    conn.closed = 0
    cur = mocker.MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    return conn, cur


def test_execute_success_commits(mocker):
    conn, cur = _make_fake_conn(mocker)
    mocker.patch("psycopg2.connect", return_value=conn)

    dbconn.execute("INSERT INTO t VALUES (%s)", (1,))

    cur.execute.assert_called_once_with("INSERT INTO t VALUES (%s)", (1,))
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_execute_failure_rolls_back_and_reraises(mocker):
    conn, cur = _make_fake_conn(mocker)
    cur.execute.side_effect = RuntimeError("쿼리 실패")
    mocker.patch("psycopg2.connect", return_value=conn)

    with pytest.raises(RuntimeError):
        dbconn.execute("BAD SQL")

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
    # 롤백 자체는 성공했으므로 커넥션을 버리지 않고 재사용해야 함
    assert dbconn._local.conn is conn


def test_execute_failure_with_dead_connection_forces_reconnect(mocker):
    """쿼리 실패 + 롤백도 실패(커넥션이 완전히 죽은 상태) -> 커넥션을 버리고
    다음 get_conn() 호출 시 새 커넥션을 만들어야 한다."""
    conn, cur = _make_fake_conn(mocker)
    cur.execute.side_effect = RuntimeError("쿼리 실패")
    conn.rollback.side_effect = RuntimeError("커넥션 이미 끊김")
    connect_mock = mocker.patch("psycopg2.connect", return_value=conn)

    with pytest.raises(RuntimeError):
        dbconn.execute("BAD SQL")

    conn.close.assert_called_once()
    assert dbconn._local.conn is None

    # 다음 호출은 새 커넥션을 생성해야 함
    new_conn, new_cur = _make_fake_conn(mocker)
    connect_mock.return_value = new_conn
    dbconn.execute("SELECT 1")
    assert connect_mock.call_count == 2
    assert dbconn._local.conn is new_conn


def test_fetchone_failure_recovers_without_masking_original_exception(mocker):
    """롤백도 실패하는 상황에서도, 사용자가 보는 예외는 원래 쿼리 실패
    (RuntimeError)여야 한다 — 롤백 실패로 인한 InterfaceError 등으로
    가려지면 안 된다 (2026-08-10 실제 장애: 이 부분이 깨져서 원인 파악이 늦어졌음)."""
    conn, cur = _make_fake_conn(mocker)
    cur.execute.side_effect = RuntimeError("원래 쿼리 실패 사유")
    conn.rollback.side_effect = RuntimeError("커넥션 끊김")
    mocker.patch("psycopg2.connect", return_value=conn)

    with pytest.raises(RuntimeError, match="원래 쿼리 실패 사유"):
        dbconn.fetchone("SELECT 1")


def test_fetchall_success_returns_cursor_result(mocker):
    conn, cur = _make_fake_conn(mocker)
    cur.fetchall.return_value = [{"code": "005930"}]
    mocker.patch("psycopg2.connect", return_value=conn)

    result = dbconn.fetchall("SELECT code FROM instruments")

    assert result == [{"code": "005930"}]


def test_get_conn_reuses_existing_open_connection(mocker):
    conn, _ = _make_fake_conn(mocker)
    connect_mock = mocker.patch("psycopg2.connect", return_value=conn)

    c1 = dbconn.get_conn()
    c2 = dbconn.get_conn()

    assert c1 is c2
    connect_mock.assert_called_once()  # 두 번째 호출은 재연결하지 않아야 함


def test_reads_close_their_transaction(mocker):
    """SELECT도 트랜잭션을 연다. 안 닫으면 그 연결이 락을 쥔 채 남는다.

    2026-08-25에 대시보드 연결이 한 시간, 진단 스크립트가 한 시간 반 동안
    'idle in transaction'으로 있어서 ALTER TABLE이 10분 넘게 막혔다. 워커 시작 시
    마이그레이션을 돌리게 바꾼 뒤라, 그대로 뒀으면 대시보드가 조회 한 번 한 것만으로
    워커가 못 뜰 수 있었다.
    """
    conn = mocker.MagicMock()
    mocker.patch.object(dbconn, "get_conn", return_value=conn)
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None

    dbconn.fetchall("SELECT 1")
    assert conn.commit.called, "fetchall이 트랜잭션을 안 닫는다"

    conn.commit.reset_mock()
    dbconn.fetchone("SELECT 1")
    assert conn.commit.called, "fetchone이 트랜잭션을 안 닫는다"


def test_schema_migration_does_not_wait_forever_on_a_lock(mocker):
    """워커가 뜰 때마다 도는 경로다. 락에 무한정 걸리면 조용히 멈춘다."""
    conn = mocker.MagicMock()
    mocker.patch.object(dbconn, "get_conn", return_value=conn)
    cur = conn.cursor.return_value.__enter__.return_value

    dbconn.init_schema()

    stmts = [c[0][0] for c in cur.execute.call_args_list]
    assert any("lock_timeout" in s and dbconn.SCHEMA_LOCK_TIMEOUT in s for s in stmts), stmts
