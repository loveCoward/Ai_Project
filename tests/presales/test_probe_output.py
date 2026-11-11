import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("case", ["without_probe", "existing_report", "missing_parent"])
def test_probe_output_rejected_before_starting_database(monkeypatch, tmp_path, capsys, case):
    spec = importlib.util.spec_from_file_location(
        "probe_output_runner", Path("scripts/test_presales_postgres.py")
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    output = tmp_path / "report.json"
    args = ["runner", "--pg-bin", str(tmp_path), "--probe-output", str(output)]
    if case == "existing_report":
        output.write_text("historical report", encoding="utf-8")
        args.append("--probe")
    elif case == "missing_parent":
        args[-1] = str(tmp_path / "missing" / "report.json")
        args.append("--probe")

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid output must be rejected before creating a database or subprocess")

    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(runner.subprocess, "run", forbidden)
    monkeypatch.setattr(runner.tempfile, "TemporaryDirectory", forbidden)
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2
    assert "--probe-output" in capsys.readouterr().err
    if case == "existing_report":
        assert output.read_text(encoding="utf-8") == "historical report"
