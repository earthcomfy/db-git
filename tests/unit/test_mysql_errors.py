from unittest.mock import Mock

import pytest

from db_git.backends.mysql.connections import Connection
from db_git.errors import DatabaseError


@pytest.mark.parametrize(
    "code,hint",
    [(1205, "lock wait timed out"), (1419, "log_bin_trust_function_creators")],
)
def test_query_errors_offer_actionable_hints_without_server_secrets(code, hint):
    pymysql = pytest.importorskip("pymysql")
    conn = Connection.__new__(Connection)
    conn.raw = Mock()
    conn.raw.cursor.return_value.execute.side_effect = pymysql.OperationalError(
        code, "server-message-with-secret"
    )
    with pytest.raises(DatabaseError) as caught:
        conn.execute("SELECT 1")
    assert hint in str(caught.value)
    assert "server-message-with-secret" not in str(caught.value)
