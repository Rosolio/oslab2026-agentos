# 课程资料 AgentOS

操作系统设计与实现课程大作业。按照 [总体设计方案](AgentOS_总体设计方案.md) 实现的单机、单 Agent、单执行槽位系统：监听课程 PDF，自动排队提取文本，生成摘要、知识点和复习题，并持久化任务进度。

## 快速运行

需要 Python 3.11+，当前支持 macOS / Linux（通过文件锁保证只有一个工作进程）。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python scripts/make_demo.py
agentos run
agentos status
```

默认 `mock` 模式不需要密钥、不发起模型请求，输出明确标记为离线演示。`run` 扫描目录、恢复上次中断的任务，并处理当前就绪任务；等待重试尚未到期时会退出，持续调度请用 `watch`。

后台与页面分别在两个终端运行，使用同一工作目录和环境变量：

```bash
source .venv/bin/activate
agentos watch
```

```bash
source .venv/bin/activate
streamlit run agentos/dashboard.py
```

将文本型 PDF 放入 `var/materials/`，页面默认在 <http://localhost:8501>。页面每两秒更新任务控制块、处理阶段、重试次数、错误、调用耗时和 Token 统计，可查看或下载结果。页面本身不启动调度器。

## 使用真实模型

支持 OpenAI 兼容的 Chat Completions 接口：

```bash
export AGENTOS_PROVIDER=openai
export OPENAI_API_KEY='填写你的密钥'
export AGENTOS_MODEL=gpt-4o-mini
export OPENAI_BASE_URL=https://api.openai.com/v1
agentos watch
```

`.env.example` 列出可配置项。程序读取已导出的环境变量，不自动加载 `.env`。不要将实际密钥提交到 Git。更换处理配置后需重启后台和页面。模型、接口地址、提示词版本、文本分块大小、输出预算及题目数量参与缓存键，凭据不写入任务数据库。

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `AGENTOS_INPUT` | `var/materials` | 监听目录，递归查找 PDF |
| `AGENTOS_DATA` | `var/data` | SQLite、版本快照和结果目录 |
| `AGENTOS_PROVIDER` | `mock` | `mock` 或 `openai` |
| `AGENTOS_MODEL` | `gpt-4o-mini` | 模型名称 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | 兼容接口基础地址 |
| `OPENAI_API_KEY` | 空 | 真实模型模式的密钥 |
| `AGENTOS_QUESTION_COUNT` | `5` | 复习题数量 |
| `AGENTOS_TIMEOUT` | `60` | 单次请求超时，秒 |
| `AGENTOS_MAX_RETRIES` | `3` | 每个任务的自动重试次数上限 |
| `AGENTOS_RETRY_DELAY` | `5` | 首次重试等待秒数，后续指数退避 |

## 核心机制

- **任务控制块与状态机**：SQLite 记录身份、内容版本、处理配置、队列序号、运行状态、业务阶段、检查点和错误。状态为 `ready → running → completed`，异常进入 `waiting` 或 `failed`。
- **FIFO 与等待队列**：就绪任务按入队序号执行；超时、网络错误、HTTP 429 和服务端错误进入等待，释放执行槽位。到期后加入队尾。解析错误、鉴权错误、空响应等直接失败。失败任务可通过页面或 `agentos retry TASK_ID` 从检查点重试。
- **检查点恢复**：文本提取、每个文本块的摘要、复习题生成后原子保存阶段和产物。启动时将遗留 `running` 任务恢复为 `ready`；已保存的模型结果不重复生成。模型请求完成但尚未保存时，仍可能重复调用。
- **事件与版本**：watchdog 回调只提交路径，独立线程合并事件并等待文件稳定，启动时补扫目录。文件以 SHA-256 标识，处理的是数据目录中的内容快照，源文件修改不会影响已入队版本。
- **文件级缓存**：相同内容与处理配置复用同一任务和成功结果；处理中也会去重。`sources` 保存不同路径与版本到任务的关联。失败任务保留失败状态，需显式重试。
- **统一工具层**：限制资料读取、快照读取和结果写入的目录范围；记录工具名称、调用状态、耗时和 Token。结果通过临时文件原子替换写入，模型文本不具备执行权限。
- **有界上下文**：默认每块 10,000 字符，每份资料最多 200,000 字符、50 MiB，最多 40 次模型调用（含失败请求），每次输出上限 2,000 Token。题目生成从各块笔记平均截取上下文，长资料的题目覆盖度需人工检查。当前按块展示摘要与知识点。

工作进程只执行与当前配置签名一致的任务，避免恢复时混用模型或题目要求。旧配置的任务会保留在页面；切回原配置可继续执行。

## 代码结构

```text
agentos/
  config.py       环境配置、处理要求和缓存签名
  store.py        SQLite TCB、就绪/等待队列、检查点与调用记录
  service.py      文件监听、启动扫描、去抖与单槽位调度
  runtime.py      提取 → 分块笔记 → 复习题 → 发布
  tools.py        PDF 提取、模型接口、访问范围和原子写入
  cli.py          watch / run / status / retry
  dashboard.py    Streamlit 状态与结果页面
scripts/make_demo.py
tests/test_workflow.py
```

运行数据默认存放在 `var/data/`，已加入 `.gitignore`。数据库是任务和进度的依据，结果文件位于 `results/`，原始版本位于 `snapshots/`；恢复或搬迁时应整体保留数据目录。

## 验证

```bash
pytest -q
ruff check .
```

测试覆盖完整 PDF 链路、内容/配置缓存失效、不可变版本快照、FIFO、到期重试入队尾、重试上限、检查点继续、真实子进程 SIGKILL 后恢复、单工作进程锁、目录越界、损坏 PDF、调用预算、启动补扫、实时监听，以及本地 HTTP 服务模拟的 429 重试与 Token 记录。

使用 `python scripts/benchmark.py` 可复现首次处理、内容未变缓存命中、强制全量基线和单文件修改四组对照。默认使用离线模式，统计调用次数、Token 与耗时；真实模型实验需要先配置环境变量，并会产生实际 API 调用。

真实模型的内容质量与外部接口可用性需要使用实际密钥和课程资料另行验收。第一版不含 OCR、PPTX、多任务并发和跨版本页级增量。当前 Token 统计使用接口返回的 usage；离线模式为 0，无法收到响应的调用无法统计服务端实际消耗。
