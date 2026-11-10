"""Start a disposable loopback PostgreSQL cluster, run phase-one tests, and stop it."""

import argparse
import os
import secrets
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg-bin", type=Path, required=True)
    parser.add_argument("--check-vector", action="store_true")
    parser.add_argument("--full-suite", action="store_true")
    parser.add_argument("--pytest-args", nargs=argparse.REMAINDER, default=[])
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
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
                timeout=180,
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
