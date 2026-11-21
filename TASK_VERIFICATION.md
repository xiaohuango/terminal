# 第五阶段 · 任务验证

本阶段接续第四阶段报告，实现[论文 §3.4 的三组试验和修订循环](https://arxiv.org/html/2605.22535v1#S3.SS4)。读取 `verified` 测试任务，核对第三、四阶段参考解哈希与任务说明，固定本次使用的镜像 ID，然后逐组创建全新容器。多服务任务使用独立 Compose 项目及临时卷，结束后清理。

| 试验 | 执行内容 | 验收规则 |
| --- | --- | --- |
| AllPassing | 完整原参考解 | 每条断言均为 `passed` |
| Nop | 不执行任何解题命令 | 每条断言均为 `failed` |
| Partial | 删除关键步骤的参考解 | 至少一条断言为 `failed` |

任一执行错误都会阻止验收，不能用来满足失败数量。空测试集、缺失/重复断言、非预期退出码也不能通过。三组均满足规则且清理成功时，任务才标为 `verified`。三组依次运行，彼此不复用可写容器状态。

## 运行

使用 Python 3.10+、Docker Desktop Linux 引擎。无需额外 Python 测试依赖；复用第三阶段镜像，不在第五阶段修改参考解或重建环境。容器缺少 Python 3 时，公共会话代码会尝试安装，失败单独记为依赖错误。请在项目根目录执行，使前序报告中的相对路径能正确解析。

```powershell
# 正式批次为空时输出零任务报告，不调用模型或 Docker
python -B -m task_verification.verify_tasks

# 已有 19419 样例：三个真实容器，不调用模型
python -B -m task_verification.verify_tasks --test-report output/test_generation_demo/test_generation_report.json --plans examples/task_verification/plans.json --ids 19419 --output-dir output/task_verification_demo

# 修订闭环演示：故意弱化首轮检查，拒绝后修复并重跑三组
python -B -m task_verification.verify_tasks --test-report output/test_generation_demo/test_generation_report.json --plans examples/task_verification/repair_plans.json --ids 19419 --output-dir output/task_verification_repair_demo

# 模型生成部分解计划，并按逐条结果提出修订
python -B -m task_verification.verify_tasks --test-report output/test_generation_demo/test_generation_report.json --model '<LiteLLM 模型 ID>' --ids 19419 --output-dir output/task_verification_model
```

`--plans` 与 `--model` 互斥。模型模式需安装 LiteLLM 并配置提供方凭据，会提交任务说明、参考解、检查定义及试验诊断。`--max-attempts` 默认 3，包含首轮；`--timeout` 默认每个步骤 300 秒，`--docker` 可指定 CLI 路径。出现拒绝或错误时，离线模式应用下一份修订，模型模式请求修订；每轮始终重跑三组。离线计划耗尽或预算耗尽仍未达标时保留 `rejected` 或 `error`，批次退出码为 `1`。Docker 不可用为 `blocked`，不伪造断言结果。未找到已验证镜像时应回到第三阶段恢复镜像。

## 部分解和修订计划

计划按任务 ID 索引，示例见 `examples/task_verification/plans.json`。`partial` 包含：

- `omit`：原参考解中唯一出现的完整文本块，删除的是影响任务结果的关键步骤。
- `replacement`：默认空字符串；只允许额外使用 `pass` 保持被清空的 Python 语句块语法有效。
- `reason`：说明删去哪一步及其预期影响。
- `requirement`：任务说明中的连续原文引用。

部分解保留其余参考流程，必须正常退出；语法错误、依赖缺失或命令异常不能充当 Partial 的断言失败。自动校验只能保证文本消融和引用有效，关键步骤的语义仍需计划作者或审阅者核对。

可选 `checks` 为完整检查数组，格式同第四阶段；缺省继承第四阶段。可选 `instruction` 为本轮完整说明；每条检查仍必须引用当前说明原文。可选 `revisions` 为后续修订数组，每项必须有 `reason`，可以更新 `checks`、`instruction`、`partial`；未写字段继承上一轮。修订应保留原任务目标，不能通过删除必要检查或修改预期目标凑齐验收。需要澄清验收要求时，把说明和测试一起修订；不覆盖前序产物，最终版本位于报告的 `verified_dir`。

## 逐条结果与错误

生成标准库 unittest 测试，并通过 `runner.py` 保存每条检查的 ID、状态、诊断。状态为 `passed`、`failed`、`error`。

缺失的要求产物、无效 UTF-8/JSON、缺失结构字段或记录索引属于可观测结果不符合要求，包装成带路径的断言失败。权限错误、导入/依赖问题和其他非预期异常保留为测试错误。执行器必须返回完整结构化结果和一致的退出码；不通过搜索日志中的 `FAIL` 判断。

| 错误类别 | 示例 |
| --- | --- |
| `container_start` | Docker 探测、创建或启动失败 |
| `test_dependency` | Python 安装失败、测试注入失败 |
| `solution_execution` | 参考解或部分解非零退出、超时 |
| `test_executor` | 测试异常、报告解析失败、结果不完整 |
| `cleanup` | 容器或 Compose 临时卷清理失败 |

错误记录与逐条断言分开存储；即使另有正常失败的断言，该组有执行错误仍不可接受。`runner.py` 返回 `0` 表示全部通过、`1` 表示断言失败、`2` 表示有测试异常，写入 `/logs/verifier/reward.txt` 的奖励只有全部通过时为 `1`。Nop/Partial 的奖励 `0` 可以对应试验验收成功。

## 产物

批次报告为 `output/task_verification/verification_report.json`。每次运行生成独立目录：

```text
tasks/<ID>/run-<随机ID>/
├── original_instruction.md
├── solution/solve.sh
├── verification_metadata.json
└── attempt-01/
    ├── revision.json
    ├── instruction.md
    ├── assertions.json
    ├── solution/partial.sh
    ├── tests/{test_state.py,test.sh,runner.py}
    ├── attempt.json
    ├── AllPassing/{trial.json,execution.json,logs/}
    ├── Nop/{trial.json,execution.json,logs/}
    └── Partial/{trial.json,execution.json,logs/}
```

## 本次真实复现结果

2026-11-21 使用已有 `19419` 的 Fernet 任务和第三阶段真实镜像运行。部分解删除 `f.write(m.decode())`，换成语法占位 `pass`；保留加密、解密与输出文件创建，最终文件为空。

| 运行 | AllPassing 通过/失败 | Nop 通过/失败 | Partial 通过/失败 | 结论 |
| --- | --- | --- | --- | --- |
| 常规样例，第 1 轮 | 2 / 0 | 0 / 2 | 1 / 1 | verified |
| 受控修订演示，第 1 轮 | 1 / 0 | 0 / 1 | 1 / 0 | rejected |
| 修订后，第 2 轮 | 2 / 0 | 0 / 2 | 1 / 1 | verified |

修订演示首轮故意只保留文件存在检查，因此未能识别空文件。根据 Partial 的逐条结果，第二轮补回 `hello world` 精确内容断言，并新建三个容器重跑。原说明已明确内容和换行要求，无需修改说明。两次运行共九个独立容器，均无执行错误并完成清理；报告分别位于 `output/task_verification_demo/`、`output/task_verification_repair_demo/`。

这是对指定消融样例的判别验证，并不能证明测试拒绝所有不完整方案，也不能单凭最终文件内容证明所有内部加密步骤都执行过。正式批次目前没有任务，本次结果属于样例复现，不代表全量数据已通过。模型模式提供入口，本次使用离线计划，未调用外部模型。

回归检查：`python -B -m unittest discover -s tests -v`，49 项全部通过，覆盖逐条规则、错误分类、无效执行器结果、产物缺失、修订后全量重跑及前四阶段回归。
