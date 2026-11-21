# TerminalWorld 复现

## 第一阶段 · 数据过滤

从已有终端录制开始。过滤器在本地读取录制并静态检查隐私、凭据、危险操作、TUI/GUI、运行平台、外部依赖和会话有效性。通过规则筛查的录制再按状态与操作的关联、任务复杂度、结果清晰度三个维度评分。实现参考了 [TerminalWorld 过滤脚本](https://github.com/terminalworld/TerminalWorld/tree/main/data_filtering) 和 [论文第 3.1 节](https://arxiv.org/html/2605.22535v1#S3.SS1)。

### 输入

```text
data/recordings/
└── 100135/
    ├── recording.txt
    ├── info.json       # 可选
    └── commands.json   # 可选
```

目录名是录制 ID。文本和 JSON 使用 UTF-8。元数据可提供 `title`、`description`、`environment`、`href`（或 `source_url`）、`duration_seconds`（或 `duration`）和 `required_urls`。时长支持秒、`MM:SS`、`HH:MM:SS` 或 `1m30s`。缺失时长记为未知。若命令无法从常见 shell 提示符提取，可以提供 `commands.json`，内容可为命令字符串数组，或含 `command` 字段的对象数组。

本地的 100 条 TerminalWorld 样本位于 `data/recordings/`。每个任务的 `info.json` 保留官方说明和来源，`recording.txt` 是由公开参考脚本派生的终端文本。该文本只提供输入命令，执行输出和时长均未知。原始任务索引为 `data/terminalworld_verified_100.jsonl`。

### 运行

在项目根目录使用 Python 3.10 或更新版本。先离线查看样本筛查结果。

```powershell
python -m data_filtering.filter_recordings --recordings-dir data/recordings --limit 30
```

可通过参数导入已有的上游评分和链接检测结果。运行示例见 [数据过滤运行说明](DATA_FILTERING.md)。

- `--url-index` 接收按录制 ID 索引的对象。每项示例为 `{"100135":[{"url":"https://github.com/example/project","accessible":true,"is_repo":true}]}`。可在元数据的 `required_urls` 补充必要链接，脚本还会从常见下载、仓库克隆和包安装命令识别字面链接。关键依赖不可访问时淘汰，缺少核验结果时待复核。
- 如需在线补充核验，添加 `--check-repos`。它对 GitHub、GitLab、Bitbucket 和 Codeberg 的公开仓库页面发送 HTTPS HEAD 请求，以页面可访问性作为初筛信号。其他链接可通过 `--url-index` 提供核验结果。
- `--scores` 接收按录制 ID 索引的对象，每项可直接包含三个评分字段，或采用上游的 `llm_score` 嵌套对象。三个字段为 `state_action_alignment`、`task_complexity` 和 `signal_clarity`，分别取 0–3 的整数。
- 如需现场评分，安装 LiteLLM 并配置所选提供方的凭据，再传 `--model <LiteLLM 模型 ID>`。模型只接收通过规则筛查的录制片段和标题、描述等信息。请确保这些内容可以发给所选提供方。也可同时指定 `--scores`，仅对缺少已有评分的样本调用模型。
- 默认至少 3 条命令、已知时长至少 10 秒、总分至少 7、结果清晰度至少 2。总分是三个维度之和加链接上下文分（0–3）。可通过 `--min-commands`、`--min-duration`、`--min-score` 和 `--min-signal-clarity` 调整。

### 输出

- `output/filtered_recordings.json` 保存通过规则和质量门槛的录制，按 ID 索引，供任务合成阶段使用。
- `output/filter_report.json` 保存本批次阈值、数量、逐条状态、原因和评分。缺少判定依据的样本标为 `skipped`，其原因可用于重跑。

可用 `--output-dir` 为不同批次指定独立目录。内置回归用例使用合成录制与模拟评分运行。

```powershell
python -B -m unittest discover -s tests -v
```

## 第二阶段 · 任务合成

合成入口读取 `output/filtered_recordings.json` 中的通过项，并从 `data/recordings/<ID>/` 读取录制原文和元数据。流程依次提取成功的命令、整理参考解、执行 `bash -n`、根据标题和描述生成任务说明，再逐条比对说明与参考解。说明给出最终目标、必要约束及产出文件的绝对路径和格式。流程顺序参考 [上游参考解提取](https://github.com/EuniAI/TerminalWorld/blob/main/task_synthesis/extract_solution.py) 和 [上游任务说明生成](https://github.com/EuniAI/TerminalWorld/blob/main/task_synthesis/generate_instruction.py)。

### 运行

在项目根目录运行。合成需要 Python 3.10 或更新版本，以及 Bash。Windows 优先使用 Git for Windows 自带的 Bash，也可通过 `--bash` 指定 Bash 可执行文件。

```powershell
python -B -m task_synthesis.synthesize_tasks
```

当前正式过滤清单为空，执行该命令会生成零任务报告。过滤阶段产出通过项后，安装 LiteLLM，按模型提供方的方式配置凭据，并填写模型 ID。该模式把通过项的录制、元数据和生成内容提交给配置的模型服务。

```powershell
python -m pip install litellm
python -B -m task_synthesis.synthesize_tasks --model '<LiteLLM 模型 ID>'
```

使用 `--input` 指定其他过滤批次，或用 `--ids` 仅处理该批次中的指定 ID。`--output-dir` 默认为 `output/synthesis`。任务的两个文件一同发布，相同内容可以在原目录重跑。生成内容有变时，可指定新的输出目录以保留已有批次。

| 输出 | 内容 |
| --- | --- |
| `tasks/<ID>/solution/solve.sh` | 通过语法检查及说明核对的 Bash 参考解。 |
| `tasks/<ID>/instruction.md` | 描述目标、产出路径、格式和验收条件的 Markdown。 |
| `synthesis_report.json` | 每条录制的处理状态、语法结果和验收项核对，包含对应的参考解行号。 |

有待复核项或错误时，批次以退出码 `1` 结束，报告中可查看原因。语法和一致性核对在静态模式下完成。依赖和环境的实际运行验证属于后续环境复现。

### 离线验收样例

下面的命令使用两个独立的待复核样本验证流程，正式过滤清单继续作为正式批次的输入。`19419` 通过两项核对并生成完整任务对。`11696` 的参考录制缺少两项工具安装，保留在报告中待复核。这个批次预期返回退出码 `1`。

```powershell
python -B -m task_synthesis.synthesize_tasks --input examples/task_synthesis/sample_input.json --reviews examples/task_synthesis/reviews.json --output-dir output/synthesis_demo
```

离线 `--reviews` 文件按 ID 提供人工确认的 `keep`、`goal`、`requirements`、`checks` 和 `omissions`。`keep` 是录制中按顺序保留的命令块，每项用含首尾的行号 `[起始行, 结束行]` 表示。首行保留主提示符，续行使用 `> `。验收项声明描述、产出路径和格式，核对项为每项提供状态、理由和生成后的 `solve.sh` 行号。该模式适合整理已有人工判断的录制和复跑核对结果。

## 第三阶段 · 环境构建

读取第二阶段发布的任务对，结合可选的原始录制提取系统包、运行时、初始文件及服务依赖。生成 `environment/Dockerfile`；有服务依赖时同时生成 `environment/compose.yaml`。随后构建镜像，在全新 Linux 容器中检查最终产物不存在，通过标准输入回放原参考解，再检查产物已生成。构建或执行失败时，将日志交给模型或下一份离线修复计划，重新构建并回放。实现流程参考[上游环境构建模块](https://github.com/terminalworld/TerminalWorld/tree/main/environment_building)，本地使用现有 LiteLLM 适配器和 Docker CLI，无需上游 Claude Agent SDK。

```powershell
# 正式批次：需先完成第二阶段，并运行 Docker Desktop 的 Linux 引擎
python -B -m environment_building.build_environment --model '<LiteLLM 模型 ID>'

# 现有离线样例：不调用模型，真实构建和回放
python -B -m environment_building.build_environment --tasks-dir output/synthesis_demo/tasks --plans examples/environment_building/plans.json --ids 19419 --output-dir output/environment_demo

# 仅生成上下文，状态为 generated，不算真实运行验收通过
python -B -m environment_building.build_environment --tasks-dir output/synthesis_demo/tasks --plans examples/environment_building/plans.json --ids 19419 --output-dir output/environment_preview --generate-only
```

空的正式合成目录生成零任务报告，不调用模型或 Docker。`--ids` 限定已发布的任务；`--max-attempts` 默认 3，`--build-timeout` 默认 1200 秒，`--replay-timeout` 默认每个执行步骤 300 秒。`--docker` 可指定 Docker CLI 路径。模型模式会把参考解、说明、录制和错误日志提交给配置的模型服务。

每次运行使用独立目录，保留全部尝试的上下文及日志。报告路径是 `output/environment_building/environment_report.json`，任务目录结构如下：

```text
tasks/<ID>/run-<随机ID>/
├── instruction.md
├── solution/solve.sh              # 原参考解副本，不放入镜像
├── signals.json
├── build_metadata.json
└── attempt-01/
    ├── plan.json
    ├── environment/               # docker build 的唯一上下文
    │   ├── Dockerfile
    │   ├── .dockerignore
    │   ├── setup.sh
    │   ├── initial/               # 可选初始输入
    │   └── compose.yaml           # 多服务任务才生成，JSON 形式的 YAML
    └── logs/                      # Docker 探测、构建、启动、前后检查、回放、清理
```

只有镜像构建成功、初始产物缺失检查通过、参考解退出码为 `0`、声明产物出现且清理成功时，才记为 `verified`。本阶段检查声明产物是否存在；内容正确性的独立测试属于后续阶段。Docker 不可用时保留已生成上下文并标为 `blocked`；未通过项令批次退出码为 `1`。镜像保留以供后续阶段使用，容器及 Compose 临时卷会清理。

离线计划格式、服务示例和验收边界见 [环境构建说明](ENVIRONMENT_BUILDING.md)。

## 第四阶段 · 测试生成

读取第三阶段报告中的 `verified` 任务，在新容器中采集原参考解执行前后的文件状态，按内容哈希识别新增、修改和删除，再结合任务说明生成断言。每条断言必须附带任务说明中的原文引用，并保存在 `assertions.json`。实现流程参考[上游测试生成脚本](https://github.com/terminalworld/TerminalWorld/blob/main/test_generation/generate_tests.py)。本地使用现有 LiteLLM 适配器生成声明式检查，再编译为标准库 unittest 测试，无需安装额外 Python 测试包。

```powershell
# 正式批次：第三阶段报告为空时，直接输出零任务报告
python -B -m test_generation.generate_tests

# 真实采集 Python 样例；离线检查计划，不调用模型
python -B -m test_generation.generate_tests --environment-report output/environment_demo_verified/environment_report.json --reviews examples/test_generation/reviews.json --ids 19419 --output-dir output/test_generation_demo

# 模型生成：从参考解实际执行前后状态和任务说明生成检查
python -B -m test_generation.generate_tests --environment-report output/environment_demo_verified/environment_report.json --model '<LiteLLM 模型 ID>' --ids 19419 --output-dir output/test_generation_model
```

产物位于 `output/test_generation/tasks/<ID>/run-<随机ID>/`，包括 `tests/test_state.py`、`tests/test.sh`、`before.json`、`after.json`、`delta.json`、`assertions.json` 和执行日志。`test.sh` 输出 `/logs/verifier/reward.txt`，通过为 `1`，失败为 `0`，同时保留测试退出码。只有参考解执行成功、生成测试在结果环境通过、在另一全新初始环境出现断言失败且清理成功，任务才标为 `verified`。

支持文件存在/缺失、目录、精确 UTF-8 文本、JSON 字段、CSV 字段/记录数和直接子目录项。断言不使用快照中的时间戳、临时进程 ID 或随机密文；也不会为说明未要求的中间脚本生成检查。模型无法用支持的稳定属性表达要求时应返回人工复核原因。详细格式、边界和多服务命令见 [测试生成说明](TEST_GENERATION.md)。

## 第五阶段 · 任务验证

读取第四阶段报告，在三个独立的新容器中分别执行完整参考解、空操作和删除关键步骤的部分解，保存逐条断言结果。AllPassing 必须全部通过，Nop 必须全部失败，Partial 至少一项失败；启动、依赖、执行器、解脚本和清理错误单独记录，不能充当断言失败。修订测试或说明后，在三个新容器中重新验证。

```powershell
# 真实样例三组验证
python -B -m task_verification.verify_tasks --test-report output/test_generation_demo/test_generation_report.json --plans examples/task_verification/plans.json --ids 19419 --output-dir output/task_verification_demo

# 实测“弱检查被拒绝 → 补充内容断言 → 三组重跑”闭环
python -B -m task_verification.verify_tasks --test-report output/test_generation_demo/test_generation_report.json --plans examples/task_verification/repair_plans.json --ids 19419 --output-dir output/task_verification_repair_demo
```

本次样例结果：AllPassing 两项通过，Nop 两项失败，Partial 一项通过、一项失败；修订演示第二轮同样通过验收。正式批次当前为空。入口支持离线修订计划和 `--model` 模式，默认最多三轮。逐条结果、错误类别、说明同步方式及复现边界见 [任务验证说明](TASK_VERIFICATION.md)。
