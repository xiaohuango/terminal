# 第一阶段 · 数据过滤运行说明

从项目根目录运行。离线过滤使用 Python 3.10 或更新版本。

## 输入文件

过滤器读取 `data/recordings/`，每个 ID 子目录对应一条样本。例如任务 `tw_7192` 对应以下文件。

```text
data/recordings/
└── 7192/
    ├── info.json
    └── recording.txt
```

- `info.json` 保存任务 ID、标题、说明、来源和时长等元数据。当前样本的时长记为 `null`。
- `recording.txt` 保存由官网参考脚本派生的终端命令文本。过滤器从其中提取命令并进行静态筛查。文件里的命令在这个阶段作为文本读取。

首批和扩充批次的官网任务索引分别为 `data/terminalworld_verified_100.jsonl` 和 `data/terminalworld_additional_100.jsonl`。运行时将合计包含 200 条样本的 `data/recordings/` 传给过滤器。

## 执行命令

入口文件是 `data_filtering/filter_recordings.py`。该文件使用包内导入，因此从项目根目录使用 `python -m` 启动。

```powershell
Set-Location -LiteralPath 'F:\reappear\terminalWordall'
python -m data_filtering.filter_recordings --recordings-dir data/recordings --output-dir output
```

离线命令只运行本地规则并读取传入的已有结果。要自动核验链接并评分，可以使用已配置的 DeepSeek API。过滤器先检查隐私、凭据、CLI 和基础可行性，再核验支持的公开仓库或制品链接，并对剩余样本批量评分。

```powershell
python -m data_filtering.filter_recordings --recordings-dir data/recordings --output-dir output --scores data/recording_value_scores.json --check-links --model deepseek/deepseek-flash --workers 6
```

此命令使用环境变量 `DEEPSEEK_API_KEY`。`data/recording_value_scores.json` 保存已有的三条人工评分。模型评分按状态与操作的关联、任务复杂度、结果清晰度各给 0–3 分，再加 0–3 分的链接上下文分。默认要求总分至少 7、结果清晰度至少 2。样本里的终端文本由公开参考脚本派生，评分反映脚本和声明产物能提供的证据。

已经有依赖核验结果时，也可传入 `--url-index` 和实际文件路径。在线核验覆盖过滤器支持的公开仓库和下载站。缺少核验结论或重试后评分仍失败的样本会跳过，并保留原因。总分等级和评分失败处理参考 [TerminalWorld 官方评分脚本](https://github.com/terminalworld/TerminalWorld/blob/main/data_filtering/score_value.py)。

- `--url-index` 指向按任务目录 ID 索引的依赖核验文件。每条链接记录有 `url`、`accessible`，可选 `is_repo`。
- `--scores` 指向按任务目录 ID 索引的评分文件。每条评分包含 `state_action_alignment`、`task_complexity` 和 `signal_clarity`，各字段取 0–3 的整数。也可以配置 LiteLLM 后传入 `--model` 进行评分。

## 输出文件

| 文件 | 内容 |
| --- | --- |
| `output/filtered_recordings.json` | 所有通过规则和质量门槛的样本，按任务目录 ID 索引，包含任务说明、来源、识别出的命令和评分，供任务合成阶段使用。 |
| `output/filter_report.json` | 全量筛查报告，包含阈值、通过／淘汰／跳过的数量、原因汇总和逐条判定。 |
| `output/recording_value_scores.json` | 配置 `--model` 时写入，保存导入评分和本次取得的模型评分。重跑时可通过 `--scores` 复用。 |

重复运行会更新指定目录中的输出。批次结果见 `filter_report.json` 的 `summary`，逐条判定见 `records`。

## 扩充样本

从官网导入更多任务需要 Beautiful Soup 和 curl。导入器优先取官网标为 Verified 的任务，并按任务 ID 去重。每次扩充指定新的索引文件路径。

```powershell
python -m data_filtering.import_terminalworld_tasks --count 100 --manifest data/terminalworld_next_batch.jsonl
```

扩充后重跑过滤时，可以传 `--scores output/recording_value_scores.json` 复用前一批评分。同一条命令仍需保留 `--check-links` 和 `--model`，以核验链接并为新增任务评分。
