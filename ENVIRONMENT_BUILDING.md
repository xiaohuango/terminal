# 环境构建运行说明

入口：`python -B -m environment_building.build_environment`。依赖 Python 3.10+、可运行 Linux 容器的 Docker Engine；多服务另需 Docker Compose v2（支持 `up --wait`）。模型模式另需 LiteLLM，沿用第二阶段的模型和凭据配置。主机不执行参考解或计划中的安装命令。

## 离线计划

`--plans` 接收以第二阶段任务 ID 为键的 JSON 对象。每项可直接是一份计划，或 `{"attempts": [第一份计划, 修复计划]}`。修复计划必须完整；每次尝试使用独立镜像、容器、Compose 项目及卷。构建、初始检查、回放或最终检查失败后读取下一份计划，最多执行 `--max-attempts` 次。模型模式自动从同样的证据生成计划，再结合前一次日志修复。

```json
{
  "example": {
    "base_image": "python:3.12-slim-bookworm",
    "workdir": "/app",
    "system_packages": ["postgresql-client"],
    "runtimes": {"python": "3.12"},
    "setup": "python -m pip install --no-cache-dir psycopg[binary]==3.2.6",
    "initial_files": [{"path": "/app/input.txt", "content": "source data\n"}],
    "outputs": ["/app/result.txt"],
    "services": {
      "db": {
        "image": "postgres:16-bookworm",
        "environment": {"POSTGRES_PASSWORD": "local-test-password"},
        "healthcheck": {
          "test": ["CMD-SHELL", "pg_isready -U postgres"],
          "interval": "2s",
          "timeout": "2s",
          "retries": 30
        }
      }
    },
    "notes": "The unchanged reference must connect to db:5432 using these local test credentials."
  }
}
```

基础镜像应支持 apt；系统包由 Dockerfile 安装。`runtimes` 记录分析依据，实际运行时必须由基础镜像、系统包或 `setup` 安装；该字段本身不安装软件。`setup` 仅准备依赖，不运行参考解、不生成任务结果。`initial_files` 只允许初始输入，与 `outputs` 的同路径及父子路径重叠会被拒绝。初始文件为 UTF-8 文本，默认文件权限；二进制输入或特殊权限可由经过审阅的 `setup` 下载和设置。

`outputs` 必须包含说明要求的全部最终文件，也应包含参考解创建的中间代码，以检查这些文件没有提前进入镜像。运行前检查既有文件也有悬空符号链接；初始镜像中存在任一声明产物便拦截回放。运行后检查所有声明路径存在。修复时不能删除首份计划声明的产物。文件内容判定需后续独立验收，`verified` 不代表内容已与原录制逐字比对。

服务配置只支持镜像、字符串环境变量、可选命令数组和必需健康检查。生成的 Compose 通过服务健康状态控制 task 启动，默认网络中以服务名访问。整个参考解及前后检查均运行在同一新 task 容器内，不挂载主机目录或 Docker socket。参考解通过 `bash -e -o pipefail -s` 的标准输入注入，不加入构建上下文。每步输出及退出码写入日志，超时记为 `124`，结束时强制清理。

没有文件产出的纯服务任务、依赖宿主内核、嵌套 Docker、专有系统或无法在普通 Linux 容器里验证的任务，需要人工复核；不要为了通过检查虚构产物。模型可返回 `review_reason` 说明原因，报告将保留该诊断。`setup` 和参考解均是待执行代码，需要使用可信来源；这套计划结构不是对任意 Docker 构建代码的安全沙箱。

## 可复跑样例

仓库提供 `examples/environment_building/plans.json`，含第二阶段现有的 `19419`（Python cryptography）和 `331228`（Node extra-iterable）依赖计划。先运行 README 中的第二阶段离线样例，再执行第三阶段。Node 样例需使用对应的 `output/synthesis_llm_verified/tasks`；如该本地批次不存在，应先完成它的第二阶段合成。

Python 样例使用 `public.ecr.aws/docker/library/python:3.12-slim-bookworm` 分发地址；首次实际构建发现本机无法连接 Docker Hub 的认证端点，故切换分发源。基础镜像拉取、apt 和 pip 均需要网络；失败细节留在 `build.log`，不会被记为回放通过。

```powershell
python -B -m environment_building.build_environment --tasks-dir output/synthesis_demo/tasks --plans examples/environment_building/plans.json --ids 19419 --output-dir output/environment_demo
python -B -m unittest discover -s tests -v
```

检查批次报告的 `status`、`environment` 路径、每次尝试日志和 `build_metadata.json`。`generated` 只表示生成成功；`blocked` 表示 Docker Linux 引擎不可用；`verified` 才满足本阶段的构建和回放条件。重复运行不会复用旧容器或覆盖旧尝试。基础镜像标签可能变化，需要严格复现时将 `base_image` 改为镜像 digest，并固定安装版本。

另有合成的多服务集成样例，用来验证 Compose 健康检查、服务名解析、真实 Redis PING 和临时资源清理，不混入正式第二阶段批次：

```powershell
python -B -m environment_building.build_environment --tasks-dir examples/environment_building/tasks --plans examples/environment_building/service_plans.json --output-dir output/environment_service_smoke
```

## 本机验收记录（2026-11-21）

- `python -B -m unittest discover -s tests -v`：23 项全部通过，包括原有两阶段的回归和新增的 10 项环境构建测试。模型修复使用模拟响应验证，没有实际调用付费模型。
- `19419`：真实 Linux 镜像构建退出码 `0`，`/app/script.py` 和 `/app/result.txt` 初始不存在，原参考解回放退出码 `0`，最终路径检查与容器清理均通过。报告：`output/environment_demo_verified/environment_report.json`。
- `redis-smoke`：真实 Compose 启动 Redis，健康检查后从 task 容器连接 `redis:6379`，收到 `PONG` 后创建结果文件。初始检查、回放、最终检查和清理均退出 `0`。报告：`output/environment_service_smoke/environment_report.json`。
- Python 样例首次 Docker Hub 拉取失败及修复后的成功构建均保留在各自 `run-*` 目录；批次报告指向最近一次运行。
- `331228` 只生成了上下文，尚未实际构建和回放；没有将其标为 `verified`。
