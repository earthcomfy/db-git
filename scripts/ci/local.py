"""Run the test workflow's matrix locally in disposable Ubuntu containers."""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=1, help="Concurrent matrix jobs")
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument(
        "--session", action="append", help="Run only these nox sessions"
    )
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    root = Path(__file__).resolve().parents[2]
    endpoint = (
        os.environ.get("DOCKER_HOST")
        or subprocess.check_output(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            text=True,
        ).strip()
    )
    if not endpoint.startswith("unix://"):
        parser.error("Local CI requires a local Docker Unix socket")
    socket = endpoint.removeprefix("unix://")
    socket_group = str(Path(socket).stat().st_gid)
    # Docker Desktop's macOS client socket is a proxy, not a bindable VM path.
    if sys.platform == "darwin":
        socket = "/var/run/docker.sock"
        socket_group = "0"
    image = "db-git-local-ci"
    subprocess.run(
        [
            "docker",
            "build",
            "--platform",
            args.platform,
            "-t",
            image,
            "-f",
            str(root / "scripts/ci/Dockerfile"),
            str(root / "scripts/ci"),
        ],
        check=True,
    )
    # Include uncommitted edits; avoid mounting the checkout or its virtualenvs.
    names = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
        )
        .decode()
        .split("\0")
    )
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name in dict.fromkeys(names):
            path = root / name
            if name and path.is_file():
                tar.add(path, arcname=name)
    source = archive.getvalue()
    # Mirror the Python/database matrix in noxfile.py and test.yml.
    sessions = args.session or [
        "lint",
        "types",
        "unit-3.12",
        "unit-3.13",
        *[
            f"integration-{python}(pg_image='postgres:{pg}')"
            for python in ("3.12", "3.13")
            for pg in (13, 14, 15, 16, 17)
        ],
        *[
            f"mysql-{python}(mysql_image='mysql:{mysql}')"
            for python in ("3.12", "3.13")
            for mysql in ("8.0", "8.4")
        ],
    ]
    logs = Path(tempfile.mkdtemp(prefix="dbgit-local-ci-"))
    print(f"Logs: {logs}", flush=True)

    def run(item: tuple[int, str]) -> dict:
        index, session = item
        name = f"dbgit-ci-{uuid.uuid4().hex[:12]}"
        log = logs / f"{index:02d}.log"
        command = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--group-add",
            socket_group,
            "--platform",
            args.platform,
            "--add-host",
            "host.docker.internal:host-gateway",
            "-v",
            f"{socket}:/var/run/docker.sock",
            "-e",
            "TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal",
            "-e",
            "TESTCONTAINERS_RYUK_DISABLED=true",
            image,
            "bash",
            "-c",
            'tar -xf - && git init -q && nox -s "$1"',
            "local-ci",
            session,
        ]
        print(f"START {session}", flush=True)
        with log.open("wb") as output:
            result = subprocess.run(command, input=source, stdout=output, stderr=output)
        status = "PASS" if result.returncode == 0 else "FAIL"
        print(f"{status} {session} ({log})", flush=True)
        return {"session": session, "exit_code": result.returncode, "log": str(log)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(run, enumerate(sessions)))
    (logs / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    failures = sum(result["exit_code"] != 0 for result in results)
    print(f"{len(results) - failures}/{len(results)} sessions passed. Results: {logs}")
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
