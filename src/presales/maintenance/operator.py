import csv
import io
import os
import subprocess

from presales.domain.contracts import AccessDenied


def current_operator() -> str:
    if os.name == "nt":
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        record = next(csv.reader(io.StringIO(result.stdout)))
        sid = record[1]
        if not sid.startswith("S-1-"):
            raise AccessDenied()
        return f"windows:{sid}"
    return f"unix:{getattr(os, 'getuid')()}"


def require_operator(allowlist: set[str]) -> str:
    operator = current_operator()
    if operator not in allowlist:
        raise AccessDenied()
    return operator
