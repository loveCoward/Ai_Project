"""Validate a task package and its red/review evidence before merge."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TIER_CAPS = {
    "L1": {
        "max_commits": 2,
        "max_files_per_commit": 3,
        "max_lines_per_commit": 200,
        "max_task_lines": 600,
    },
    "L2": {
        "max_commits": 4,
        "max_files_per_commit": 5,
        "max_lines_per_commit": 400,
        "max_task_lines": 800,
    },
    "L3": {
        "max_commits": 8,
        "max_files_per_commit": 5,
        "max_lines_per_commit": 400,
        "max_task_lines": 2400,
    },
}
FINAL_STATUSES = {"PASS", "MERGED"}
L1_REQUIRED = ("task_id", "title", "tier", "status", "goal", "allowed_paths", "ac")
L2_REQUIRED = L1_REQUIRED + ("test_files", "red_evidence", "review_evidence")
REQUIRED_RED = (
    "task_id",
    "base_sha",
    "test_commit",
    "test_files",
    "command",
    "exit_code",
    "failed_tests",
)
REQUIRED_REVIEW = (
    "task_id",
    "verdict",
    "reviewer",
    "implementer",
    "reviewed_head",
    "contract_sha256",
    "ac_results",
    "commands",
)
TASK_ROOT = "docs/maintenance/tasks"
CONTRACT_FIELDS = (
    "task_id",
    "tier",
    "goal",
    "allowed_paths",
    "forbidden_paths",
    "test_files",
    "ac",
    "real_env",
    "budget",
)


class GateError(RuntimeError):
    pass


@dataclass
class CommitChange:
    sha: str
    files: int
    lines: int


@dataclass
class GateResult:
    task_id: str = ""
    tier: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    commits: int = 0
    changed_files: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def fail(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def _run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise GateError(f"git {' '.join(args)} failed: {detail}")
    return completed


def _text(repo: Path, *args: str) -> str:
    return _run(repo, *args).stdout.strip()


def _resolve_commit(repo: Path, revision: str) -> str:
    return _text(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return (
        _run(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0
    )


def _changed_files(repo: Path, base: str, head: str) -> list[str]:
    output = _text(repo, "diff", "--name-only", f"{base}...{head}")
    return [line for line in output.splitlines() if line]


def _commit_files(repo: Path, commit: str) -> list[str]:
    output = _text(repo, "show", "--pretty=format:", "--name-only", commit)
    return [line for line in output.splitlines() if line]


def _commit_changes(repo: Path, base: str, head: str) -> list[CommitChange]:
    changes: list[CommitChange] = []
    for sha in _text(repo, "rev-list", "--reverse", f"{base}..{head}").splitlines():
        files = 0
        lines = 0
        output = _text(repo, "show", "--numstat", "--format=", sha)
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            files += 1
            additions, deletions = parts[0], parts[1]
            if additions.isdigit():
                lines += int(additions)
            if deletions.isdigit():
                lines += int(deletions)
        changes.append(CommitChange(sha=sha[:12], files=files, lines=lines))
    return changes


def _glob_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*" and index + 1 < len(pattern) and pattern[index + 1] == "*":
            parts.append(".*")
            index += 2
        elif character == "*":
            parts.append("[^/]*")
            index += 1
        elif character == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(character))
            index += 1
    parts.append("$")
    return re.compile("".join(parts))


def _matches(path: str, patterns: list[str]) -> bool:
    return any(_glob_regex(pattern).match(path) for pattern in patterns)


def _safe_relative(value: str, label: str) -> str:
    raw = value.strip()
    if not raw:
        raise GateError(f"{label} must be a non-empty repository-relative path: {value!r}")
    if raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", raw):
        raise GateError(f"{label} must be a repository-relative path: {value!r}")
    normalized = raw.replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise GateError(f"{label} must be a repository-relative path: {value!r}")
    return path.as_posix()


def contract_digest(data: dict[str, Any]) -> str:
    payload = {key: data.get(key) for key in CONTRACT_FIELDS}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _evidence_path(repo: Path, manifest_dir: Path, value: str, label: str) -> Path:
    path = (repo / _safe_relative(value, label)).resolve()
    task_dir = manifest_dir.resolve()
    try:
        path.relative_to(task_dir)
    except ValueError as exc:
        raise GateError(
            f"{label} must be inside {task_dir.relative_to(repo.resolve()).as_posix()}"
        ) from exc
    return path


def _require_string(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GateError(f"{label}.{key} must be a non-empty string")
    return value.strip()


def _require_string_list(data: dict[str, Any], key: str, label: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise GateError(f"{label}.{key} must be a non-empty list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise GateError(f"{label}.{key} must contain non-empty strings")
        result.append(item.strip())
    return result


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise GateError(f"{label} not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GateError(f"{label} must contain a JSON object")
    return data


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _discover_manifest(repo: Path, task: str | None, base: str, head: str) -> Path:
    if task:
        candidate = Path(task)
        if not candidate.is_absolute():
            candidate = repo / candidate
        if candidate.is_dir():
            candidate = candidate / "task.toml"
        if candidate.is_file():
            return candidate.resolve()
        for name in dict.fromkeys([task.replace("/", "-"), task.replace("/", "-").lower()]):
            task_id_path = repo / TASK_ROOT / name / "task.toml"
            if task_id_path.is_file():
                return task_id_path.resolve()
        raise GateError(f"task manifest not found for {task!r}")

    changed = _changed_files(repo, base, head)
    changed_manifests = [
        path for path in changed if path.startswith(f"{TASK_ROOT}/") and path.endswith("/task.toml")
    ]
    if len(changed_manifests) == 1:
        return (repo / changed_manifests[0]).resolve()
    if len(changed_manifests) > 1:
        raise GateError("multiple task manifests changed; pass --task")

    available = sorted((repo / TASK_ROOT).glob("*/task.toml"))
    if len(available) == 1:
        return available[0].resolve()
    if not available:
        raise GateError(f"no task manifest under {TASK_ROOT}; pass --task")
    raise GateError("multiple task manifests available; pass --task")


def _task_dir(repo: Path, manifest_path: Path) -> Path:
    resolved = manifest_path.resolve()
    try:
        resolved.relative_to(repo.resolve())
    except ValueError as exc:
        raise GateError("task manifest must be inside the repository") from exc
    if resolved.name != "task.toml":
        raise GateError("task manifest must be named task.toml")
    return resolved.parent


def _manifest_budget(data: dict[str, Any], tier: str) -> dict[str, int]:
    budget = data.get("budget", {})
    if not isinstance(budget, dict):
        raise GateError("budget must be a table")
    caps = TIER_CAPS[tier]
    limits: dict[str, int] = {}
    for key, cap in caps.items():
        value = budget.get(key, cap)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise GateError(f"budget.{key} must be a positive integer")
        if value > cap:
            raise GateError(f"budget.{key}={value} exceeds the {tier} cap of {cap}")
        limits[key] = value
    return limits


def _validate_paths(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    allowed = [
        _safe_relative(path, "allowed_paths")
        for path in _require_string_list(data, "allowed_paths", "task")
    ]
    forbidden_raw = data.get("forbidden_paths", [])
    if not isinstance(forbidden_raw, list):
        raise GateError("task.forbidden_paths must be a list")
    forbidden: list[str] = []
    for path in forbidden_raw:
        if not isinstance(path, str):
            raise GateError("task.forbidden_paths must contain strings")
        forbidden.append(_safe_relative(path, "forbidden_paths"))
    if any(pattern in {"*", "**", "/**"} for pattern in allowed):
        raise GateError("allowed_paths must not grant the whole repository")
    return allowed, forbidden


def _validate_manifest(data: dict[str, Any], path: Path) -> tuple[str, str, dict[str, int]]:
    tier = _require_string(data, "tier", "task")
    if tier not in TIER_CAPS:
        raise GateError(f"task.tier must be one of {sorted(TIER_CAPS)}")
    required = L1_REQUIRED if tier == "L1" else L2_REQUIRED
    for key in required:
        if key not in data:
            raise GateError(f"task.{key} is required for {tier}")
    task_id = _require_string(data, "task_id", "task")
    _require_string(data, "title", "task")
    _require_string(data, "goal", "task")
    _require_string(data, "status", "task")
    _require_string_list(data, "ac", "task")
    _validate_paths(data)
    limits = _manifest_budget(data, tier)
    if tier != "L1":
        test_files = [
            _safe_relative(item, "test_files")
            for item in _require_string_list(data, "test_files", "task")
        ]
        if not test_files:
            raise GateError("task.test_files must not be empty")
        _require_string(data, "red_evidence", "task")
        _require_string(data, "review_evidence", "task")
    real_env = data.get("real_env", {})
    if not isinstance(real_env, dict):
        raise GateError("task.real_env must be a table")
    for key, value in real_env.items():
        if not isinstance(value, bool):
            raise GateError(f"task.real_env.{key} must be a boolean")
    return task_id, tier, limits


def _check_scope(
    repo: Path,
    manifest: dict[str, Any],
    manifest_dir: Path,
    base: str,
    head: str,
    result: GateResult,
) -> None:
    allowed, forbidden = _validate_paths(manifest)
    task_dir = manifest_dir.relative_to(repo).as_posix()
    allowed.append(f"{task_dir}/**")
    changed = _changed_files(repo, base, head)
    result.changed_files = len(changed)
    for path in changed:
        if _matches(path, forbidden):
            result.fail(f"forbidden path changed: {path}")
        elif not _matches(path, allowed):
            result.fail(f"path outside allowed_paths: {path}")

    if manifest["tier"] != "L1":
        declared = {_safe_relative(item, "test_files") for item in manifest["test_files"]}
        for path in changed:
            if (path.startswith("tests/") or path.startswith("test/")) and path not in declared:
                result.fail(f"test file changed but not declared in test_files: {path}")


def _check_budget(
    repo: Path,
    base: str,
    head: str,
    limits: dict[str, int],
    result: GateResult,
) -> None:
    changes = _commit_changes(repo, base, head)
    result.commits = len(changes)
    if not changes:
        result.fail("no commits found between base and head")
    if len(changes) > limits["max_commits"]:
        result.fail(f"{len(changes)} commits exceed max_commits={limits['max_commits']}")
    total_lines = 0
    for change in changes:
        total_lines += change.lines
        if change.files > limits["max_files_per_commit"]:
            result.fail(
                f"commit {change.sha} changes {change.files} files; "
                f"max_files_per_commit={limits['max_files_per_commit']}"
            )
        if change.lines > limits["max_lines_per_commit"]:
            result.fail(
                f"commit {change.sha} changes {change.lines} lines; "
                f"max_lines_per_commit={limits['max_lines_per_commit']}"
            )
    if total_lines > limits["max_task_lines"]:
        result.fail(f"task changes {total_lines} lines; max_task_lines={limits['max_task_lines']}")


def _check_red(
    repo: Path,
    manifest: dict[str, Any],
    manifest_dir: Path,
    head: str,
    result: GateResult,
) -> None:
    if manifest["tier"] == "L1":
        return
    task_id = manifest["task_id"]
    try:
        red_path = _evidence_path(repo, manifest_dir, manifest["red_evidence"], "red_evidence")
    except GateError as exc:
        result.fail(str(exc))
        return
    red = _load_json(red_path, "red.json")
    for key in REQUIRED_RED:
        if key not in red:
            result.fail(f"red.json.{key} is required")
    if result.errors and any(error.startswith("red.json.") for error in result.errors):
        return
    if red.get("task_id") != task_id:
        result.fail("red.json.task_id does not match task.toml")
    command = red.get("command")
    if not isinstance(command, str) or not command.strip():
        result.fail("red.json.command must be a non-empty string")
    if red.get("exit_code") != 1:
        result.fail("red.json.exit_code must be 1 (tests failed, not a collection error)")
    failed_tests = red.get("failed_tests")
    if not isinstance(failed_tests, list) or not failed_tests:
        result.fail("red.json.failed_tests must be a non-empty list")

    try:
        red_commit = _resolve_commit(repo, str(red["test_commit"]))
    except (GateError, KeyError) as exc:
        result.fail(f"red.json.test_commit is not a valid commit: {exc}")
        return
    if not _is_ancestor(repo, red_commit, head):
        result.fail("red.json.test_commit is not an ancestor of head")
    try:
        red_base = _resolve_commit(repo, str(red["base_sha"]))
    except (GateError, KeyError) as exc:
        result.fail(f"red.json.base_sha is not a valid commit: {exc}")
        red_base = ""
    if red_base and not _is_ancestor(repo, red_base, red_commit):
        result.fail("red.json.base_sha is not an ancestor of test_commit")

    red_files = red.get("test_files")
    if not isinstance(red_files, dict):
        result.fail("red.json.test_files must be an object of path to sha256")
        return
    relative_red = red_path.relative_to(repo.resolve()).as_posix()
    for path in _commit_files(repo, red_commit):
        if path == relative_red:
            continue
        if not (path.startswith("tests/") or path.startswith("test/")):
            result.fail(f"red commit changes a non-test file: {path}")

    declared = manifest["test_files"]
    for path in declared:
        clean_path = _safe_relative(path, "test_files")
        file_path = repo / clean_path
        if not file_path.is_file():
            result.fail(f"declared test file does not exist: {clean_path}")
            continue
        expected = red_files.get(clean_path)
        if not isinstance(expected, str):
            result.fail(f"red.json.test_files has no hash for {clean_path}")
            continue
        actual = _sha256(file_path)
        if actual != expected:
            result.fail(f"test file changed after red evidence: {clean_path}")


def _check_review(
    repo: Path,
    manifest: dict[str, Any],
    manifest_dir: Path,
    head: str,
    result: GateResult,
) -> None:
    if manifest["tier"] == "L1":
        return
    try:
        review_path = _evidence_path(
            repo, manifest_dir, manifest["review_evidence"], "review_evidence"
        )
    except GateError as exc:
        result.fail(str(exc))
        return
    review = _load_json(review_path, "review.json")
    for key in REQUIRED_REVIEW:
        if key not in review:
            result.fail(f"review.json.{key} is required")
    if result.errors and any(error.startswith("review.json.") for error in result.errors):
        return
    if review.get("task_id") != manifest["task_id"]:
        result.fail("review.json.task_id does not match task.toml")
    if review.get("verdict") != "PASS":
        result.fail("review.json.verdict must be PASS")
    if review.get("contract_sha256") != contract_digest(manifest):
        result.fail("review.json.contract_sha256 does not match task.toml")
    reviewer = review.get("reviewer")
    implementer = review.get("implementer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        result.fail("review.json.reviewer must be a non-empty string")
    if not isinstance(implementer, str) or not implementer.strip():
        result.fail("review.json.implementer must be a non-empty string")
    if (
        isinstance(reviewer, str)
        and isinstance(implementer, str)
        and reviewer.strip() == implementer.strip()
    ):
        result.fail("reviewer and implementer must be different")

    try:
        reviewed_head = _resolve_commit(repo, str(review["reviewed_head"]))
    except (GateError, KeyError) as exc:
        result.fail(f"review.json.reviewed_head is not a valid commit: {exc}")
        return
    if not _is_ancestor(repo, reviewed_head, head):
        result.fail("review.json.reviewed_head is not an ancestor of head")
    task_dir = manifest_dir.relative_to(repo).as_posix()
    for path in _changed_files(repo, reviewed_head, head):
        if not path.startswith(f"{task_dir}/"):
            result.fail(
                f"changes after reviewed_head are outside the task evidence directory: {path}"
            )

    ac_results = review.get("ac_results")
    if not isinstance(ac_results, dict):
        result.fail("review.json.ac_results must be an object")
    else:
        for ac in manifest["ac"]:
            if ac_results.get(ac) != "PASS":
                result.fail(f"review.json.ac_results[{ac!r}] must be PASS")
    commands = review.get("commands")
    if not isinstance(commands, list) or not commands:
        result.fail("review.json.commands must be a non-empty list")
    else:
        for index, item in enumerate(commands):
            if not isinstance(item, dict):
                result.fail(f"review.json.commands[{index}] must be an object")
                continue
            if not isinstance(item.get("command"), str) or not item["command"].strip():
                result.fail(f"review.json.commands[{index}].command is required")
            if item.get("exit_code") != 0:
                result.fail(f"review.json.commands[{index}].exit_code must be 0")

    real_env = manifest.get("real_env", {})
    evidence = review.get("environment_evidence", {})
    if not isinstance(evidence, dict):
        result.fail("review.json.environment_evidence must be an object")
    else:
        for name, required in real_env.items():
            if required and not str(evidence.get(name, "")).strip():
                result.fail(f"review.json.environment_evidence.{name} is required")

    if manifest["tier"] == "L3":
        human = review.get("human_approval")
        if not isinstance(human, dict) or human.get("approved") is not True:
            result.fail("review.json.human_approval.approved must be true for L3")
        elif not isinstance(human.get("name"), str) or not human["name"].strip():
            result.fail("review.json.human_approval.name is required for L3")


def validate_task(
    repo: Path,
    task: str | None = None,
    base: str | None = None,
    head: str = "HEAD",
) -> GateResult:
    repo = repo.resolve()
    head_sha = _resolve_commit(repo, head)
    if base is None:
        for candidate in ("origin/main", "main"):
            if _run(repo, "rev-parse", "--verify", candidate, check=False).returncode == 0:
                base = _text(repo, "merge-base", candidate, head_sha)
                break
    if base is None:
        raise GateError("cannot determine base; pass --base")
    base_sha = _resolve_commit(repo, base)
    manifest_path = _discover_manifest(repo, task, base_sha, head_sha)
    manifest_dir = _task_dir(repo, manifest_path)
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GateError(f"task.toml is not valid TOML: {exc}") from exc
    if not isinstance(manifest, dict):
        raise GateError("task.toml must contain a table")
    task_id, tier, limits = _validate_manifest(manifest, manifest_path)
    result = GateResult(task_id=task_id, tier=tier)
    if manifest.get("status") not in FINAL_STATUSES:
        result.fail(f"task.status must be one of {sorted(FINAL_STATUSES)}")
    _check_scope(repo, manifest, manifest_dir, base_sha, head_sha, result)
    _check_budget(repo, base_sha, head_sha, limits, result)
    _check_red(repo, manifest, manifest_dir, head_sha, result)
    _check_review(repo, manifest, manifest_dir, head_sha, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--task", help="Task id or path to task.toml")
    parser.add_argument(
        "--base", help="Base revision; defaults to merge-base with origin/main or main"
    )
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--mode", choices=["enforce", "advisory"], default="enforce")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable result")
    args = parser.parse_args(argv)
    try:
        result = validate_task(args.repo_root, task=args.task, base=args.base, head=args.head)
    except GateError as exc:
        result = GateResult(errors=[str(exc)])
    payload = {
        "status": "PASS" if result.ok else "FAIL",
        "task_id": result.task_id,
        "tier": result.tier,
        "commits": result.commits,
        "changed_files": result.changed_files,
        "errors": result.errors,
        "warnings": result.warnings,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"task-gate: {result.task_id or 'unknown'} ({result.tier or '?'})")
        for warning in result.warnings:
            print(f"  [warning] {warning}")
        for error in result.errors:
            print(f"  [error] {error}")
        print(f"  commits={result.commits} changed_files={result.changed_files}")
    if payload["status"] == "FAIL" and args.mode == "enforce":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
