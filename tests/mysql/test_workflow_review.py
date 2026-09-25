from __future__ import annotations

import json
import sys

import pytest
from tests._pg_helpers import run_git
from tests.mysql.test_extended import shared
from tests.mysql.test_mysql import command, selected

from db_git.backends.mysql.connections import identifier
from db_git.backends.mysql.operations import MARKER
from db_git.recovery import operations
from db_git.repository import git_directory, operations_directory


def test_url_refuses_pending_per_branch_recovery(project):
    command(project, "create", "feature")
    root = operations_directory(git_directory(project[0]))
    record = operations(root)[0]
    record.phase = "ready"
    record.write(root)
    result = command(project, "url", "feature", check=False)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "recover" in result.stderr


@pytest.mark.parametrize("damage", ["missing", "wrong-marker"])
def test_url_and_run_refuse_damaged_generation(project, damage):
    root, _, _, env, conn = project
    run_git("checkout", "-b", "feature", cwd=root, env=env)
    database = identifier(selected(project))
    if damage == "missing":
        conn.execute(f"DROP DATABASE {database}")
    else:
        conn.execute(
            f"ALTER TABLE {database}.{identifier(MARKER)} COMMENT=%s",
            ("dbgit:" + "f" * 32,),
        )
    for args in (
        ("url", "feature"),
        ("run", "--", sys.executable, "-c", "print('application-started')"),
    ):
        result = command(project, *args, check=False)
        assert result.returncode == 1
        assert result.stdout == ""
        assert "doctor" in result.stderr
    report = command(project, "doctor", "--json", check=False)
    assert not json.loads(report.stdout)["ok"]


def test_shared_status_refuses_missing_active_generation(project):
    shared(project)
    command(project, "save", "main")
    command(project, "restore", "main")
    database = command(project, "url").stdout.strip().rsplit("/", 1)[1]
    project[4].execute(f"DROP DATABASE {identifier(database)}")
    result = command(project, "status", check=False)
    assert result.returncode == 1
    assert "active-generation" in result.stderr


def test_mysql_checkout_reminds_user_to_restart_application(project):
    root, _, _, env, _ = project
    first = run_git("checkout", "-b", "feature", cwd=root, env=env)
    assert "db-git run" in first.stderr
    second = run_git("checkout", "main", cwd=root, env=env)
    assert "db-git run" in second.stderr


def test_status_does_not_invent_generation_for_uncreated_branch(project):
    root, _, _, env, _ = project
    command(project, "disable")
    run_git("checkout", "-b", "feature", cwd=root, env=env)
    result = command(project, "status")
    assert "not created" in result.stderr
    assert "_dbgit_" not in result.stderr


@pytest.mark.parametrize("mode", ["shared", "per-branch"])
def test_application_connects_to_selected_branch_after_restart(project, mode):
    if mode == "shared":
        shared(project)
    root, _, _, env, _ = project
    script = """
import json, os, ssl, sys
from urllib.parse import unquote, urlsplit
import pymysql

url = urlsplit(os.environ['DATABASE_URL'])
tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
tls.check_hostname = False
tls.verify_mode = ssl.CERT_NONE
connection = pymysql.connect(
    host=url.hostname, port=url.port,
    user=unquote(url.username), password=unquote(url.password),
    database=unquote(url.path[1:]), ssl=tls, charset='utf8mb4', autocommit=True,
)
try:
    with connection.cursor() as cursor:
        if len(sys.argv) > 1:
            cursor.execute('INSERT INTO users(name) VALUES(%s)', (sys.argv[1],))
        cursor.execute('SELECT name FROM users ORDER BY id')
        print(json.dumps([row[0] for row in cursor.fetchall()]))
finally:
    connection.close()
"""
    run_git("checkout", "-b", "feature", cwd=root, env=env)
    written = command(
        project, "run", "--", sys.executable, "-c", script, "feature-only"
    )
    assert json.loads(written.stdout) == ["seed é 🐈", "feature-only"]
    run_git("checkout", "main", cwd=root, env=env)
    main = command(project, "run", "--", sys.executable, "-c", script)
    assert json.loads(main.stdout) == ["seed é 🐈"]
    run_git("checkout", "feature", cwd=root, env=env)
    feature = command(project, "run", "--", sys.executable, "-c", script)
    assert json.loads(feature.stdout) == ["seed é 🐈", "feature-only"]
