from contextlib import closing

import pytest

from contextforge import intelligence


@pytest.fixture(autouse=True)
def close_index_connections(monkeypatch):
    original_connect = intelligence.sqlite3.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(intelligence.sqlite3, "connect", tracked_connect)
    try:
        yield
    finally:
        for connection in reversed(connections):
            with closing(connection):
                pass
