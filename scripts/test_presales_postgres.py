"""Start a disposable loopback PostgreSQL cluster, run phase-one tests, and stop it."""

import argparse
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine

DEFAULT_PYTEST_TIMEOUT_SECONDS = 180
SELECTED_PYTEST_TIMEOUT_SECONDS = 300
FULL_SUITE_PYTEST_TIMEOUT_SECONDS = 900


def select_pytest_timeout(*, full_suite: bool, pytest_args: list[str]) -> int:
    if full_suite:
        return FULL_SUITE_PYTEST_TIMEOUT_SECONDS
    if any(argument == "-k" or argument.startswith("-k") for argument in pytest_args):
        return SELECTED_PYTEST_TIMEOUT_SECONDS
    return DEFAULT_PYTEST_TIMEOUT_SECONDS


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg-bin", type=Path, required=True)
    parser.add_argument("--check-vector", action="store_true")
    parser.add_argument("--full-suite", action="store_true")
    parser.add_argument(
        "--probe", action="store_true", help="Run the explicit real-Embedding development probe"
    )
    parser.add_argument("--probe-output", type=Path, help="Write the probe to a new report path")
    parser.add_argument(
        "--workbench",
        action="store_true",
        help="Serve the synthetic UI for bounded manual browser verification",
    )
    parser.add_argument("--pytest-args", nargs=argparse.REMAINDER, default=[])
    args = parser.parse_args()
    if sum([args.full_suite, args.workbench, args.probe]) > 1:
        parser.error("Select only one execution mode")
    project = Path(__file__).resolve().parents[1]
    if args.probe_output is not None:
        if not args.probe:
            parser.error("--probe-output requires --probe")
        probe_output = args.probe_output.resolve()
        if probe_output.exists() or not probe_output.parent.is_dir():
            parser.error("--probe-output requires a new file in an existing directory")
    else:
        probe_output = project / "docs/maintenance/phase2a-real-probe-2026-11-11.json"
    extension = ".exe" if os.name == "nt" else ""
    binaries = {name: args.pg_bin / f"{name}{extension}" for name in ["initdb", "pg_ctl"]}
    if not all(path.is_file() for path in binaries.values()):
        parser.error("PostgreSQL executables not found")
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    passwords = {name: secrets.token_hex(24) for name in ["phaseadmin", "phaseapp", "phasemaint"]}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    inherited = [
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "COMSPEC",
        "HOME",
    ]
    environment = {name: os.environ[name] for name in inherited if name in os.environ}
    with tempfile.TemporaryDirectory(prefix="biddesk-phase1-pg-") as temporary:
        root = Path(temporary).resolve()
        data = root / "cluster"
        password_file = root / "initial-password"
        password_file.write_text(passwords["phaseadmin"], encoding="utf-8")
        started = False
        try:
            subprocess.run(
                [
                    str(binaries["initdb"]),
                    "-D",
                    str(data),
                    "-U",
                    "phaseadmin",
                    "--encoding=UTF8",
                    "--locale=C",
                    "--auth=scram-sha-256",
                    f"--pwfile={password_file}",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                creationflags=flags,
                timeout=60,
            )
            password_file.unlink()
            subprocess.run(
                [
                    str(binaries["pg_ctl"]),
                    "-D",
                    str(data),
                    "-l",
                    str(root / "postgres.log"),
                    "-o",
                    f"-h 127.0.0.1 -p {port} -c max_connections=30 -c shared_buffers=32MB",
                    "-w",
                    "start",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                creationflags=flags,
                timeout=60,
            )
            started = True
            connection = dict(
                host="127.0.0.1", port=port, user="phaseadmin", password=passwords["phaseadmin"]
            )
            with psycopg.connect(**connection, dbname="postgres", autocommit=True) as conn:
                conn.execute("CREATE DATABASE biddesk_phase1_test")
            with psycopg.connect(
                **connection, dbname="biddesk_phase1_test", autocommit=True
            ) as conn:
                conn.execute("CREATE TABLE public.phase_one_test_marker(value text NOT NULL)")
                conn.execute(
                    "INSERT INTO public.phase_one_test_marker VALUES('disposable-phase-one')"
                )
                for user in ["phaseapp", "phasemaint"]:
                    conn.execute(
                        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                            sql.Identifier(user), sql.Literal(passwords[user])
                        )
                    )
                version = conn.execute("SHOW server_version").fetchone()[0]
                if args.check_vector:
                    conn.execute("CREATE EXTENSION IF NOT EXISTS vector VERSION '0.8.6'")
                    vector_version = conn.execute(
                        "SELECT extversion FROM pg_extension WHERE extname='vector'"
                    ).fetchone()[0]
                    assert vector_version == "0.8.6"
                    assert (
                        conn.execute(
                            "SELECT '[1,0,0]'::vector(3) <-> '[0,1,0]'::vector(3)"
                        ).fetchone()[0]
                        > 1
                    )
                    print(f"PGVector {vector_version}; native vector distance verified", flush=True)
            urls = {
                name: f"postgresql+psycopg://{name}:{passwords[name]}@127.0.0.1:{port}/biddesk_phase1_test"
                for name in passwords
            }
            cfg = Config(str(project / "alembic.ini"))
            engine = create_engine(urls["phaseadmin"], hide_parameters=True)
            with engine.begin() as conn:
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, "0001_identity")
            with engine.begin() as conn:
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, "head")
            engine.dispose()
            with psycopg.connect(
                **connection, dbname="biddesk_phase1_test", autocommit=True
            ) as conn:
                conn.execute("GRANT presales_runtime TO phaseapp")
                conn.execute("GRANT presales_maintainer TO phasemaint")
            environment.update(
                {
                    "PRESALES_TEST_URL": urls["phaseadmin"],
                    "PRESALES_TEST_RUNTIME_URL": urls["phaseapp"],
                    "PRESALES_TEST_MAINTENANCE_URL": urls["phasemaint"],
                    "PRESALES_ALLOW_DISPOSABLE_TESTS": "yes",
                }
            )
            print(
                f"Disposable PostgreSQL {version}; loopback only; credentials omitted", flush=True
            )
            if args.probe:
                environment["PRESALES_UI_FILE_ROOT"] = str(root / "files")
                environment["PYTHONPATH"] = str(project / "src")
                for name, value in os.environ.items():
                    if (
                        name.startswith("PRESALES_OUTBOUND_EMBEDDING__")
                        or name == "PRESALES_RETRIEVAL_DIMENSIONS"
                    ):
                        environment[name] = value
                result = subprocess.run(
                    [
                        sys.executable,
                        str(project / "scripts/probe_presales_retrieval.py"),
                        "--execute",
                        "--output",
                        str(probe_output),
                    ],
                    cwd=project,
                    env=environment,
                    creationflags=flags,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=660,
                )
                output = result.stdout + result.stderr
                for password in passwords.values():
                    output = output.replace(password, "[redacted]")
                print(output, flush=True)
                return result.returncode
            if args.workbench:
                environment["PRESALES_UI_FILE_ROOT"] = str(root / "files")
                environment["PYTHONPATH"] = os.pathsep.join([str(project / "src"), str(project)])
                environment["PYTHONIOENCODING"] = "utf-8"
                with subprocess.Popen(
                    [sys.executable, "-u", str(project / "scripts/check_presales_workbench.py")],
                    cwd=project,
                    env=environment,
                    creationflags=flags,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                ) as child:
                    timer = threading.Timer(1200, child.terminate)
                    timer.start()
                    try:
                        assert child.stdout is not None
                        for line in child.stdout:
                            for password in passwords.values():
                                line = line.replace(password, "[redacted]")
                            print(line, end="", flush=True)
                        return child.wait()
                    finally:
                        timer.cancel()
                        if child.poll() is None:
                            child.terminate()
                            child.wait(timeout=10)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests" if args.full_suite else "tests/presales",
                    *args.pytest_args,
                ],
                cwd=project,
                env=environment,
                creationflags=flags,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=select_pytest_timeout(
                    full_suite=args.full_suite, pytest_args=args.pytest_args
                ),
            )
            output = result.stdout + result.stderr
            for password in passwords.values():
                output = output.replace(password, "[redacted]")
            print(output, flush=True)
            return result.returncode
        finally:
            if started or (data / "postmaster.pid").exists():
                subprocess.run(
                    [str(binaries["pg_ctl"]), "-D", str(data), "-m", "fast", "-w", "stop"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    creationflags=flags,
                    timeout=60,
                )
                print("Disposable PostgreSQL stopped.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
