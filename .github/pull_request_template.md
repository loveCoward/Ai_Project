## 任务

- Task ID：
- 档位：L1 / L2 / L3
- 任务包：`docs/maintenance/tasks/<task-id>/task.toml`
- 基线 SHA：
- 合并后 SHA：

## 变更范围

- 实际改动：
- 明确不做：
- 允许路径：
- 是否包含生成文件：否 / 是（生成命令与来源）

## 验收条件

| AC | 断言 | 测试 | 结果 |
| --- | --- | --- | --- |
| AC-01 |  |  | PASS / FAIL |

## 先红证据

- Red commit：
- `RED.log` / `red.json`：
- 失败测试：
- 退出码：1
- 测试文件 hash 是否与最终版本一致：是 / 否（说明）

## 独立验证

- 验证者：
- 是否与实现者不同：是 / 否
- `REVIEW.md` / `review.json`：
- `contract_sha256`：
- 结论：PASS / CHANGES_REQUESTED / BLOCKED
- 复跑命令与结果：

## 真实环境

| 环境 | 是否使用 | 版本/模式 | 证据 |
| --- | --- | --- | --- |
| PostgreSQL | 否 |  |  |
| MCP | 否 |  |  |
| 真实模型 | 否 |  |  |
| 浏览器 | 否 |  |  |

## 风险与缺口

- 未实现/未验证：
- 已知风险：
- 回滚方式：

## 提交前检查

- [ ] 没有直接向 `main` 推送
- [ ] 一个 PR 只对应一个任务包
- [ ] 实现提交没有修改 red 测试或冻结的 AC
- [ ] 没有删除失败测试、新增 `skip`/`xfail` 或放宽断言
- [ ] `pymarkdown scan`、ruff、pyrefly、pytest 和前端检查已按范围执行
- [ ] `STATUS.md` 仅在验证 PASS 后由集成者更新
