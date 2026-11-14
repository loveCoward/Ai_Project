import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from scripts.task_gate import GateError, contract_digest, validate_task


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_task(
    repo: Path,
    tier: str = "L2",
    allowed_paths: list[str] | None = None,
    ac: list[str] | None = None,
    real_env: dict[str, bool] | None = None,
) -> Path:
    task_dir = repo / "docs/maintenance/tasks/2b-2-t18"
    task_dir.mkdir(parents=True)
    allowed = allowed_paths or ["src/example.py", "tests/test_example.py"]
    criteria = ac or ["AC-01", "AC-02"]
    env = real_env or {}
    lines = [
        'task_id = "2B-2/T18"',
        'title = "Example task"',
        f'tier = "{tier}"',
        'status = "PASS"',
        'goal = "Expose an observable result"',
        f"allowed_paths = {json.dumps(allowed)}",
        f"ac = {json.dumps(criteria)}",
    ]
    if tier != "L1":
        lines.extend(
            [
                'test_files = ["tests/test_example.py"]',
                'red_evidence = "docs/maintenance/tasks/2b-2-t18/red.json"',
                'review_evidence = "docs/maintenance/tasks/2b-2-t18/review.json"',
            ]
        )
    max_commits = 1 if tier == "L1" else 4
    lines.extend(["[budget]", f"max_commits = {max_commits}"])
    if env:
        lines.append("[real_env]")
        lines.extend(f"{name} = {str(value).lower()}" for name, value in env.items())
    manifest = task_dir / "task.toml"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def write_red(
    repo: Path,
    red_commit: str,
    base: str,
    test_file: Path,
    failed_tests: list[str] | None = None,
) -> None:
    task_dir = repo / "docs/maintenance/tasks/2b-2-t18"
    payload = {
        "task_id": "2B-2/T18",
        "base_sha": base,
        "test_commit": red_commit,
        "test_files": {test_file.relative_to(repo).as_posix(): digest(test_file)},
        "command": "uv run pytest tests/test_example.py -q",
        "exit_code": 1,
        "failed_tests": failed_tests or ["test_example_behavior"],
    }
    (task_dir / "red.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_review(
    repo: Path,
    head: str,
    contract_sha256: str,
    reviewer: str = "verifier",
    implementer: str = "implementer",
    ac: list[str] | None = None,
    environment_evidence: dict[str, str] | None = None,
    human_approved: bool | None = None,
) -> None:
    task_dir = repo / "docs/maintenance/tasks/2b-2-t18"
    payload = {
        "task_id": "2B-2/T18",
        "verdict": "PASS",
        "reviewer": reviewer,
        "implementer": implementer,
        "reviewed_head": head,
        "contract_sha256": contract_sha256,
        "ac_results": {key: "PASS" for key in (ac or ["AC-01", "AC-02"])},
        "commands": [{"command": "uv run pytest tests/test_example.py -q", "exit_code": 0}],
        "environment_evidence": environment_evidence or {},
    }
    if human_approved is not None:
        payload["human_approval"] = {"approved": human_approved, "name": "release-owner"}
    (task_dir / "review.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def task_contract_digest(repo: Path) -> str:
    path = repo / "docs/maintenance/tasks/2b-2-t18/task.toml"
    return contract_digest(tomllib.loads(path.read_text(encoding="utf-8")))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Task Gate Test")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src/example.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests/test_example.py").write_text(
        "from src.example import VALUE\n\n\ndef test_value():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    commit(root, "base")
    return root


def prepare_l2(repo: Path) -> tuple[str, str]:
    base = git(repo, "rev-parse", "HEAD")
    write_task(repo)
    commit(repo, "docs: freeze task package")
    (repo / "tests/test_example.py").write_text(
        "from src.example import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    red_commit = commit(repo, "test: add failing test [RED]")
    write_red(repo, red_commit, base, repo / "tests/test_example.py")
    (repo / "src/example.py").write_text("VALUE = 2\n", encoding="utf-8")
    green = commit(repo, "feat: implement behavior")
    write_review(repo, green, task_contract_digest(repo))
    head = commit(repo, "docs: record review")
    return base, head


def test_l2_task_with_complete_red_and_review_evidence_passes(repo: Path):
    base, head = prepare_l2(repo)

    result = validate_task(repo, task="2B-2/T18", base=base, head=head)

    assert result.ok, result.errors
    assert result.tier == "L2"
    assert result.commits == 4


def test_path_outside_allowed_paths_fails(repo: Path):
    base, head = prepare_l2(repo)
    (repo / "README.md").write_text("unexpected\n", encoding="utf-8")
    commit(repo, "docs: change unrelated file")

    result = validate_task(repo, task="2B-2/T18", base=base, head="HEAD")

    assert any("path outside allowed_paths: README.md" in error for error in result.errors)


def test_red_evidence_must_point_at_a_real_ancestor_commit(repo: Path):
    base, head = prepare_l2(repo)
    red_path = repo / "docs/maintenance/tasks/2b-2-t18/red.json"
    payload = json.loads(red_path.read_text(encoding="utf-8"))
    payload["test_commit"] = "deadbeef"
    red_path.write_text(json.dumps(payload), encoding="utf-8")
    head = commit(repo, "docs: corrupt red pointer")

    result = validate_task(repo, task="2B-2/T18", base=base, head=head)

    assert any("test_commit is not a valid commit" in error for error in result.errors)


def test_test_file_change_after_red_fails(repo: Path):
    base, head = prepare_l2(repo)
    (repo / "tests/test_example.py").write_text(
        "from src.example import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n\n"
        "def test_weakened():\n    assert True\n",
        encoding="utf-8",
    )
    head = commit(repo, "test: weaken after red")

    result = validate_task(repo, task="2B-2/T18", base=base, head=head)

    assert any("test file changed after red evidence" in error for error in result.errors)


def test_reviewer_and_implementer_must_be_different(repo: Path):
    base = git(repo, "rev-parse", "HEAD")
    write_task(repo)
    commit(repo, "docs: freeze task package")
    (repo / "tests/test_example.py").write_text(
        "from src.example import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    red_commit = commit(repo, "test: add failing test [RED]")
    write_red(repo, red_commit, base, repo / "tests/test_example.py")
    (repo / "src/example.py").write_text("VALUE = 2\n", encoding="utf-8")
    green = commit(repo, "feat: implement behavior")
    write_review(repo, green, task_contract_digest(repo), reviewer="same", implementer="same")
    head = commit(repo, "docs: record review")

    result = validate_task(repo, task="2B-2/T18", base=base, head=head)

    assert any("reviewer and implementer must be different" in error for error in result.errors)


def test_l3_requires_human_approval_and_environment_evidence(repo: Path):
    base = git(repo, "rev-parse", "HEAD")
    write_task(repo, tier="L3", real_env={"postgres": True})
    commit(repo, "docs: freeze task package")
    (repo / "tests/test_example.py").write_text(
        "from src.example import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    red_commit = commit(repo, "test: add failing test [RED]")
    write_red(repo, red_commit, base, repo / "tests/test_example.py")
    (repo / "src/example.py").write_text("VALUE = 2\n", encoding="utf-8")
    green = commit(repo, "feat: implement behavior")
    write_review(repo, green, task_contract_digest(repo), human_approved=False)
    head = commit(repo, "docs: record review")

    result = validate_task(repo, task="2B-2/T18", base=base, head=head)

    assert any("environment_evidence.postgres is required" in error for error in result.errors)
    assert any("human_approval.approved must be true" in error for error in result.errors)


def test_l3_with_human_approval_and_environment_evidence_passes(repo: Path):
    base = git(repo, "rev-parse", "HEAD")
    write_task(repo, tier="L3", real_env={"postgres": True})
    commit(repo, "docs: freeze task package")
    (repo / "tests/test_example.py").write_text(
        "from src.example import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    red_commit = commit(repo, "test: add failing test [RED]")
    write_red(repo, red_commit, base, repo / "tests/test_example.py")
    (repo / "src/example.py").write_text("VALUE = 2\n", encoding="utf-8")
    green = commit(repo, "feat: implement behavior")
    write_review(
        repo,
        green,
        task_contract_digest(repo),
        environment_evidence={"postgres": "PostgreSQL 18.6 disposable loopback"},
        human_approved=True,
    )
    head = commit(repo, "docs: record review")

    result = validate_task(repo, task="2B-2/T18", base=base, head=head)

    assert result.ok, result.errors
    assert result.tier == "L3"


def test_l1_does_not_require_red_or_review(repo: Path):
    base = git(repo, "rev-parse", "HEAD")
    write_task(repo, tier="L1", allowed_paths=["README.md"], ac=["AC-01"])
    (repo / "README.md").write_text("updated\n", encoding="utf-8")
    head = commit(repo, "docs: update readme")

    result = validate_task(repo, task="2B-2/T18", base=base, head=head)

    assert result.ok, result.errors


def test_contract_change_after_review_fails(repo: Path):
    base, head = prepare_l2(repo)
    task_path = repo / "docs/maintenance/tasks/2b-2-t18/task.toml"
    manifest = tomllib.loads(task_path.read_text(encoding="utf-8"))
    manifest["ac"] = ["AC-01"]
    task_path.write_text(
        "\n".join(
            [
                f'task_id = "{manifest["task_id"]}"',
                f'title = "{manifest["title"]}"',
                f'tier = "{manifest["tier"]}"',
                f'status = "{manifest["status"]}"',
                f'goal = "{manifest["goal"]}"',
                f"allowed_paths = {json.dumps(manifest['allowed_paths'])}",
                f"ac = {json.dumps(manifest['ac'])}",
                f"test_files = {json.dumps(manifest['test_files'])}",
                f'red_evidence = "{manifest["red_evidence"]}"',
                f'review_evidence = "{manifest["review_evidence"]}"',
                "[budget]",
                f"max_commits = {manifest['budget']['max_commits']}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    head = commit(repo, "docs: change frozen contract")

    result = validate_task(repo, task="2B-2/T18", base=base, head=head)

    assert any("contract_sha256 does not match" in error for error in result.errors)


def test_evidence_file_outside_task_directory_fails(repo: Path):
    base, head = prepare_l2(repo)
    task_path = repo / "docs/maintenance/tasks/2b-2-t18/task.toml"
    task_path.write_text(
        task_path.read_text(encoding="utf-8").replace(
            'review_evidence = "docs/maintenance/tasks/2b-2-t18/review.json"',
            'review_evidence = "docs/maintenance/tasks/other/review.json"',
        ),
        encoding="utf-8",
    )
    head = commit(repo, "docs: redirect evidence")

    result = validate_task(repo, task="2B-2/T18", base=base, head=head)

    assert any("review_evidence must be inside" in error for error in result.errors)


def test_missing_manifest_raises_gate_error(repo: Path):
    with pytest.raises(GateError, match="task manifest not found"):
        validate_task(repo, task="missing", base="HEAD", head="HEAD")


def test_cli_exit_code_reflects_failure_in_enforce_mode(repo: Path, capsys):
    from scripts.task_gate import main

    exit_code = main(
        ["--repo-root", str(repo), "--task", "missing", "--base", "HEAD", "--head", "HEAD"]
    )

    assert exit_code == 1
    assert "task manifest not found" in capsys.readouterr().out


def test_cli_exit_code_is_zero_for_advisory_failure(repo: Path):
    from scripts.task_gate import main

    exit_code = main(
        [
            "--repo-root",
            str(repo),
            "--task",
            "missing",
            "--base",
            "HEAD",
            "--head",
            "HEAD",
            "--mode",
            "advisory",
        ]
    )

    assert exit_code == 0
