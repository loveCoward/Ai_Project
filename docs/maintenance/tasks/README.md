# Task Packages

每个 L2/L3 任务使用独立目录存放任务包和证据：

```text
docs/maintenance/tasks/<task-id>/
├── task.toml
├── red.json
└── review.json
```

`task.toml`、`red.json` 和 `review.json` 的字段定义与校验规则见
[`docs/development-workflow.md`](../../development-workflow.md)。本目录当前只提供格式说明，
尚未有试点任务；在 CI 的 `task-gate` job 从 advisory 切换为 enforce 前，缺失任务包不会被阻止合并。

## 最小示例

### `task.toml`

```toml
task_id = "2B-2/T18"
title = "Proposal solution analysis node"
tier = "L2"
status = "PASS"
goal = "Architecture analysis produces a validated, testable proposal section"
spec_refs = ["docs/superpowers/plans/Phase-Two-B-Implementation-v1.md"]
allowed_paths = [
  "src/presales/application/proposal_solution_analysis.py",
  "tests/presales/test_proposal_solution_analysis.py",
]
forbidden_paths = ["AGENTS.md", "STATUS.md", "migrations/**"]
test_files = ["tests/presales/test_proposal_solution_analysis.py"]
red_evidence = "docs/maintenance/tasks/2b-2-t18/red.json"
review_evidence = "docs/maintenance/tasks/2b-2-t18/review.json"
ac = ["AC07-a", "AC07-b"]

[real_env]
postgres = false
mcp = false
model = false
browser = false

[budget]
max_commits = 4
max_files_per_commit = 5
max_lines_per_commit = 400
max_task_lines = 800
```

### `red.json`

```json
{
  "task_id": "2B-2/T18",
  "base_sha": "<40-char sha>",
  "test_commit": "<40-char sha>",
  "test_files": {
    "tests/presales/test_proposal_solution_analysis.py": "<sha256>"
  },
  "command": "uv run pytest tests/presales/test_proposal_solution_analysis.py -q",
  "exit_code": 1,
  "failed_tests": ["test_missing_structure_fails"]
}
```

### `review.json`

```json
{
  "task_id": "2B-2/T18",
  "verdict": "PASS",
  "reviewer": "verifier-session",
  "implementer": "implementer-session",
  "reviewed_head": "<40-char sha>",
  "contract_sha256": "<task contract digest>",
  "ac_results": {
    "AC07-a": "PASS",
    "AC07-b": "PASS"
  },
  "commands": [
    {
      "command": "uv run pytest tests/presales/test_proposal_solution_analysis.py -q",
      "exit_code": 0
    }
  ],
  "environment_evidence": {
    "postgres": "not required"
  }
}
```

## 本地检查

```sh
python scripts/task_gate.py --repo-root . --task 2B-2/T18 --base origin/main --head HEAD
```

在任务包尚未提交、只在工作区存在时，`--task` 可以指向本地 `task.toml` 路径：

```sh
python scripts/task_gate.py --repo-root . --task docs/maintenance/tasks/2b-2-t18/task.toml --base origin/main
```

`task_gate.py` 只检查结构和证据，不替代 pytest、真实 PostgreSQL、浏览器或模型验证。
`contract_sha256` 是 `task.toml` 中 `task_id`、`tier`、`goal`、`allowed_paths`、
`forbidden_paths`、`test_files`、`ac`、`real_env` 和 `budget` 的规范化 SHA256；
可通过 `python -c "from scripts.task_gate import contract_digest; import tomllib; print(contract_digest(tomllib.load(open('docs/maintenance/tasks/<task-id>/task.toml','rb'))))"` 计算。
