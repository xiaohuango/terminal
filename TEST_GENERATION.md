# 测试生成运行说明

入口：`python -B -m test_generation.generate_tests`。需要 Python 3.10+ 和 Docker Linux 引擎；服务任务另需 Docker Compose v2。模型模式沿用第二阶段的 LiteLLM 配置，将任务说明、参考解、执行前后状态和差异发送给所选模型。离线 `--reviews` 模式只替换模型判断，仍然真实采集状态并执行测试。

## 与第三阶段衔接

默认读取 `output/environment_building/environment_report.json`，使用其中 `records` 的任务。`--environment-report` 可指定其他批次；`--ids` 筛选任务，未指定时处理报告中的全部任务。上游非 `verified` 状态不能进入测试生成。空报告无需模型和 Docker，输出零任务报告。

从报告指向的运行目录读取说明和参考解，先核对参考解 SHA-256 与第三阶段的验证记录一致。从最终成功环境读取 `plan.json` 和 Compose 配置，优先使用保留镜像；镜像缺失则用原构建上下文重新构建。请在项目根目录运行，报告中的相对路径以当前目录解析。

每次使用新的普通容器，或独立 Compose 项目、服务和临时卷。参考解通过标准输入执行；测试文件通过编码数据注入 `/tests`。不挂载主机目录，不在主机执行参考解。容器缺少 `python3` 时，先通过 apt 安装验证运行时；因此这类环境需要包源网络访问，安装日志保留在 `verifier_runtime.log`。

## 状态与生成

采集根为 `/app`、计划工作目录和所有声明产物路径。快照保存路径、文件类型、符号链接目标、字节数、SHA-256 及小文件的 UTF-8 内容，不保存时间戳、inode 或 PID。差异包含 `created`、`modified`、`deleted`；因此同大小内容变化也能识别，单纯更新时间不会算内容变化。

单次最多采集 4000 个条目；64 KiB 以内的文本保存内容；64 MiB 以内文件计算完整内容哈希。超过文本阈值只省略文本，保留大小和哈希；超出条目或哈希阈值、权限读取失败等会记录错误并中止生成，避免把不完整快照当成完整证据。快照不会跟随目录符号链接。

采集前检查第三阶段声明的产物不存在；参考解必须退出 `0`，且声明产物全部出现，才会调用模型。模型返回检查计划，或返回 `review_reason` 提示需要人工复核。生成器只接受支持的声明式操作，并编译成 `tests/test_state.py`；不会执行模型返回的任意 Python 或 Bash。`tests/test.sh` 使用容器内 Python 标准库 unittest，初始化奖励为 `0`，全部通过后写 `1`，并返回原测试退出码。

## 离线检查格式

`--reviews` 为按任务 ID 索引的对象。每项包含非空 `checks` 数组。例如：

```json
{
  "example": {
    "checks": [
      {
        "id": "records_count",
        "requirement": "Save exactly two records in /app/result.json.",
        "path": "/app/result.json",
        "kind": "json",
        "selector": ["records"],
        "op": "count",
        "expected": 2
      }
    ]
  }
}
```

`requirement` 必须是对应 `instruction.md` 的连续原文，生成器会逐条检查；`id` 为唯一的小写字母、数字和下划线标识符，以字母开头。每个生成的测试方法仅包含一项验收断言；引用与期望值嵌入测试文件，同时保留在 `assertions.json`。

| 字段 | 支持值与含义 |
| --- | --- |
| `path` | 绝对 Linux 路径，禁止根路径和 `..` 穿越。 |
| `kind` | `file`、`directory`、`absent`、`text`、`json`、`csv`、`entries`。 |
| `selector` | 可选，仅 JSON/CSV 使用。按顺序访问字段名或整数索引。CSV 解析为字典行列表，字段值为字符串。 |
| `op` | `eq` 精确相等；`contains` 包含；`count` 比较长度；`keys` 比较字典字段集合。 |
| `expected` | 稳定的预期值。路径类型检查只能用 `eq` 和 `true`；计数必须是非负整数。 |

`text` 严格按 UTF-8 解码，保留原换行字节，不调用 `strip()`。`entries` 只比较目标目录直接子项名称的排序列表。常见易变字段如 `timestamp`、`pid`、`mtime`、`created_at` 不能作为 selector；其他语义上的随机值需在计划复核时排除。原文引用校验能保证来源存在，不能自动证明检查完整或语义等价，仍需审阅检查计划。

## 验证和输出

生成后在参考解结果容器运行测试，必须退出 `0` 且奖励为 `1`。再启动另一套全新环境，直接运行同一测试，必须退出 `1`、奖励为 `0` 且包含实际断言失败，不能只靠缺文件读取异常或导入失败充当有效反例。两套环境均在 finally 中清理容器和临时卷；清理失败同样不能标为通过。

```text
output/test_generation/
├── test_generation_report.json
└── tasks/<ID>/run-<随机ID>/
    ├── instruction.md
    ├── before.json
    ├── after.json
    ├── delta.json
    ├── assertions.json
    ├── test_metadata.json
    ├── tests/test_state.py
    ├── tests/test.sh
    ├── reference/execution.json
    ├── reference/logs/             # 状态采集、回放、正例、奖励及清理
    └── baseline/logs/              # 全新环境反例、奖励及清理
```

批次状态为 `verified`、`blocked` 或 `error`。Docker Linux 引擎不可用为 `blocked`；生成、回放、测试或清理失败为 `error`，诊断和已生成内容均保留；有未通过任务时批次退出 `1`。每次使用新运行目录，不覆盖旧证据。默认超时每步 300 秒，重建镜像 1200 秒，可用 `--timeout`、`--build-timeout` 调整，`--docker` 指定 Docker CLI。

最终状态检查验证可观察产物；无法单靠最终文件证明使用过某个库、执行过某种算法或连接过服务。现有 Fernet 样例只断言说明要求的输出路径和 `hello world` 精确内容，不把随机密钥和密文当作验收条件，也不把中间 `script.py` 当作新增要求。完整过程约束或其他暂不支持的观测需人工复核或扩展检查类型。初始环境反例也不替代全面的错误实现测试。

## 可复跑验收

```powershell
python -B -m unittest discover -s tests -v

python -B -m test_generation.generate_tests --environment-report output/environment_demo_verified/environment_report.json --reviews examples/test_generation/reviews.json --ids 19419 --output-dir output/test_generation_demo

python -B -m test_generation.generate_tests --environment-report output/environment_service_smoke/environment_report.json --reviews examples/test_generation/reviews.json --ids redis-smoke --output-dir output/test_generation_service
```

本机验收（2026-11-21）：39 项回归测试全部通过，其中新增 16 项测试覆盖内容差异、原文溯源、结构化字段、记录数量、错误内容、换行、非法 UTF-8、模型上下文、回放失败、缺失 Docker 和资源清理等。模型路径使用模拟响应验证，未调用付费模型。`19419` 和 `redis-smoke` 均完成真实 Docker 状态采集和正反例验证，报告分别保存在 `output/test_generation_demo/test_generation_report.json` 与 `output/test_generation_service/test_generation_report.json`。正式上游批次仍为空，未将样例混入正式任务。
