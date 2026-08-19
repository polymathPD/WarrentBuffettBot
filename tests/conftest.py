"""
공통 테스트 픽스처.

프로젝트의 모든 모듈이 `import db.connection as db` 후 `db.fetchone(...)` 식으로
호출하므로, `db.connection` 모듈의 함수 자체를 patch하면 어떤 모듈에서 호출하든
동일하게 mock이 적용된다 (실제 DB 연결 없이 단위 테스트 가능).
"""
import sys
import types
from dataclasses import dataclass, field

import pytest


@dataclass
class MockDB:
    """db.connection의 4개 함수를 mock으로 교체하고, 반환값을 손쉽게 설정하기 위한 헬퍼."""
    fetchone: "object" = None
    fetchall: "object" = None
    execute: "object" = None
    executemany: "object" = None


@pytest.fixture
def mock_db(mocker):
    m = MockDB(
        fetchone=mocker.patch("db.connection.fetchone", return_value=None),
        fetchall=mocker.patch("db.connection.fetchall", return_value=[]),
        execute=mocker.patch("db.connection.execute", return_value=None),
        executemany=mocker.patch("db.connection.executemany", return_value=None),
    )
    return m


@pytest.fixture
def mock_settings(mocker):
    """config.get_setting()이 내부적으로 psycopg2.connect()를 직접 호출해 실제 DB에
    접속을 시도하므로(캐시 만료 시), 테스트에서는 하드코딩 기본값(config._DEFAULTS)만
    반환하도록 격리한다."""
    import config

    def _fake_get_setting(key):
        return config._DEFAULTS[key]

    return mocker.patch("config.get_setting", side_effect=_fake_get_setting)


@pytest.fixture
def mock_claude(mocker):
    """agents.base._get_client()가 반환하는 Anthropic 클라이언트를 mock으로 교체.
    사용법: mock_claude.return_value = "결정: 매수 / 확신: 8 / 이유: 테스트"
    처럼 텍스트를 지정하면 messages.create()가 그 텍스트를 담은 응답을 반환한다."""

    def _make_response(text):
        content_block = types.SimpleNamespace(text=text)
        return types.SimpleNamespace(content=[content_block])

    fake_client = mocker.MagicMock()
    holder = {"text": "결정: 관망 / 확신: 5 / 이유: 기본 mock 응답"}

    def _create(*args, **kwargs):
        return _make_response(holder["text"])

    fake_client.messages.create.side_effect = _create
    mocker.patch("agents.base._get_client", return_value=fake_client)

    class _Proxy:
        @property
        def return_value(self):
            return holder["text"]

        @return_value.setter
        def return_value(self, text):
            holder["text"] = text

        @property
        def mock(self):
            return fake_client.messages.create

    return _Proxy()


@pytest.fixture(autouse=True)
def _no_real_api_calls(mocker):
    """테스트가 실제 Anthropic API를 때리지 못하게 막는다.

    2026-08-20에 gate 테스트가 새 에이전트를 mock하지 않은 채로 돌아 실제 호출이
    한 건 나갔다. 테스트는 조용히 통과했고(반환값이 우연히 '매수'였다) 실행시간이
    늘어난 것 말고는 신호가 없었다. mock_claude를 쓰는 테스트는 같은 대상을 다시
    patch하므로 이 가드를 덮어쓴다 — 즉 의도적으로 mock한 경우만 통과한다.
    """
    def _blocked(*args, **kwargs):
        raise AssertionError(
            "테스트에서 실제 Claude API를 호출하려 했다. "
            "해당 에이전트의 analyze()를 mock하거나 mock_claude 픽스처를 써라."
        )

    return mocker.patch("agents.base._get_client", side_effect=_blocked)
