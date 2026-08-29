# python-agent

`python-agent` 是一个小而清晰的原生 Python Agent Harness。当前版本已完成架构文档中的
阶段 0—5：除“模型 → 工具 → 模型”闭环、Agent Handle 和安全工具流水线外，还提供
JSONL Session 持久化、崩溃恢复、有界工具并发、Turn 预算和模型请求有限重试。

## 安装

在本目录执行：

```bash
python -m pip install -e '.[dev]'
```

## 运行离线示例

默认使用可控的 Fake Adapter，因此不需要 API Key：

```bash
python-agent run "你好，介绍一下自己"
```

读取工作区内的文件：

```bash
python-agent run "读取 README.md" --demo-read README.md
```

## 使用 DeepSeek

在 `python-agent` 项目根目录创建 `.env`（可以复制 `.env.example`），配置连接参数：

```dotenv
DEEPSEEK_API_KEY=sk-your-key-here
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_TIMEOUT_SECONDS=120
DEEPSEEK_MODEL=deepseek-chat
```

代码会优先读取构造函数显式参数，其次读取已经存在的环境变量，最后读取项目 `.env`。
`.env` 默认不会覆盖已经由 Shell 或 Conda 设置的同名环境变量。建议保护密钥文件：

```bash
chmod 600 .env
```

配置完成后运行：

```bash
python-agent run "检查项目结构" --provider deepseek
```

也可以直接使用 Python API：

```python
import asyncio

from python_agent import AgentLoop, FakeAdapter
from python_agent.tools.builtins import EchoTool
from python_agent.tools.registry import ToolRegistry


async def main() -> None:
    registry = ToolRegistry([EchoTool()])
    agent = AgentLoop(FakeAdapter(), registry)
    result = await agent.run("hello")
    print(result.answer)
    print(result.session.transcript())


asyncio.run(main())
```

## 本地检查

```bash
ruff format --check .
ruff check .
mypy
pytest
```

## 阶段 2 Agent Handle

阶段 2提供由 `AgentManager` 持有的 Agent Handle。`followup` 开启新的 Turn，`steer`
在下一个 Step 注入纠偏信息，`inject` 只写入上下文但不会唤醒 idle Agent：

```python
import asyncio

from python_agent import AgentManager, FakeAdapter


async def main() -> None:
    manager = AgentManager()
    agent = await manager.create(FakeAdapter())
    await agent.inject("这是静默上下文")
    await agent.followup("请完成任务")
    await agent.when_idle()
    print(agent.last_result.answer)
    await manager.shutdown()


asyncio.run(main())
```

一个 Agent 同时只会拥有一个 Driver Task；所有 Inbox 操作都会记录为
`agent/inbox/spliced`，因此可以通过事件重放恢复待处理消息。真正的 JSONL 磁盘持久化
属于后续阶段。

## 阶段 3 工具策略与安全执行

所有工具调用都会经过三条 Waterfall：Pre 负责参数、路径、权限和审批，Execute 负责
超时，Post 负责结果规范化、裁剪和 spill。写入工具需要明确指定 workspace-write：

```bash
python-agent run "更新 README" \
  --permission-mode workspace-write
```

`bash` 默认需要审批；没有审批服务时会在工具主体执行前返回错误。只有在明确确认本次
进程中的所有 Bash 调用都可信时，才使用：

```bash
python-agent run "检查项目构建" \
  --permission-mode workspace-write \
  --approve-bash
```

超长工具结果会保存到 workspace 下的 `.python-agent/tool-output/`，模型上下文只收到
预览、总长度和文件路径。应用层也可以传入自己的 `ApprovalService`、Pre/Execute/Post
策略或 `spill_directory`。

CLI 会实时显示模型文本增量、工具调用和工具结果。DeepSeek 使用 SSE 流式接口；Fake
Adapter 也会切分文本，用于离线验证同样的显示流程。

## 交互式终端

在真实终端中先激活 `agent` 环境，再启动 chat：

```bash
conda activate agent
python-agent chat
```

交互模式支持以下输入：

```text
普通文本             → followup，开启新的 Turn
/steer 内容          → 在下一步纠偏
/inject 内容         → 写入上下文但不唤醒 idle Agent
/cancel              → 取消并清空待处理输入
/cancel keep         → 取消但保留 Inbox
/status              → 查看 Agent 和 Inbox 状态
/transcript          → 查看当前 Session
/wait                → 等待当前任务完成
/exit                → 退出
```

模型在后台运行时仍然可以输入下一条 followup 或 steer；实时输出由 Live Event Bus
转发到终端。

终端显示的 `你>` 是提示符，不需要手动输入。若从示例中误粘贴了一个或多个 `你>`，
chat 会自动移除并显示 `[输入修正]`，因此 `你> /transcript` 仍会按本地命令执行。

提交消息后会看到明确反馈：idle 时显示 `[已提交]`；已有任务运行时显示 `[已排队]` 和
当前队列长度。每次真实模型请求会显示 Provider/模型、开始状态、完成/失败/取消状态和
耗时。运行期间输入 `/status` 还能查看当前 Turn/Step 与已等待秒数，例如：

```text
状态：running
next_turn：1 条
next_step：0 条
模型请求：Turn 2 / Step 2 → deepseek/qwen3.7-flash，已等待 12.4 秒
```

如果误提交的请求仍在运行，使用 `/cancel` 清空当前执行和排队输入，再重新输入；后续
普通消息不会丢失，但在单 Driver 规则下只会于当前任务结束后按 FIFO 顺序执行。

## 阶段 4 持久化与恢复

CLI 默认把每次 Session 保存到当前 workspace 的 `.python-agent/sessions/<session-id>/`：

```text
header.json
events.jsonl
```

每条事件会先写入 JSONL、flush 并 fsync，成功后才进入内存事件列表。一次性运行结束时
会在 stderr 显示 Session ID；交互模式启动时也会显示。使用相同 Provider、模型、权限和
步数配置继续会话：

```bash
python-agent run "继续检查剩余文件" --resume SESSION_ID
python-agent chat --resume SESSION_ID
```

查看 Session、导出 transcript：

```bash
python-agent sessions
python-agent transcript SESSION_ID transcript.txt
```

恢复默认严格校验 Header/Event 版本、连续 seq、JSONL 完整性、未知必需事件以及
Turn/Step/工具结果配对。进程崩溃可能留下最后一行或未闭合工具调用，此时普通 load 会
明确失败；确认目标 Session 后可显式修复：

```bash
python-agent repair SESSION_ID
python-agent chat --resume SESSION_ID --repair-session
```

物理截断前会创建 `events.jsonl.repair-backup*`。恢复不会自动重放状态不明的工具，而是
追加一个错误 `tool/result` 并关闭原 Step/Turn，避免写操作产生重复副作用。

Python API 可以把 Store 交给 Manager：

```python
from pathlib import Path

from python_agent import AgentManager, AgentPreset, FakeAdapter, JsonlSessionStore

store = JsonlSessionStore(Path(".python-agent"))
config = AgentPreset(id="coding-v1", workspace=Path.cwd())
manager = AgentManager(session_store=store, presets={config.id: config})

agent = await manager.create(FakeAdapter(), config=config)
session_id = agent.id

# 进程重启后：重放 Inbox；存在可唤醒消息时自动恢复单 Driver。
agent = await manager.resume(session_id, FakeAdapter())
```

## 阶段 5 并发、预算与模型重试

同一模型响应中的工具不再全部串行执行。工具通过
`is_concurrency_safe(arguments)` 声明本次调用是否可并发：连续只读调用进入有界滚动池，
写入、Bash 或其他 exclusive 工具形成屏障。调用可以乱序完成，但 `tool/result` 始终按
模型给出的 call 顺序写入 Session。取消时会排空所有已创建 Task，并为每个 call 生成结果。

CLI 可以配置并发数和 Turn 级预算：

```bash
python-agent chat \
  --max-parallel-tools 4 \
  --max-turn-tokens 20000 \
  --max-turn-seconds 300 \
  --model-max-retries 2 \
  --model-retry-base-delay-seconds 0.5
```

费用预算优先使用 Provider 在 usage 中返回的费用；Provider 不返回时可以显式配置单价：

```bash
python-agent run "执行受预算限制的任务" \
  --max-turn-cost-usd 0.10 \
  --input-cost-per-million-tokens 0.50 \
  --output-cost-per-million-tokens 2.00
```

预算达到后，Agent 会以 `token_budget`、`cost_budget` 或 `wall_time` 正常关闭 Turn，并把
累计 Token、费用和耗时写入 `turn/end`。如果模型已返回工具调用但预算不足，所有调用都会
得到明确的错误 `tool/result`，工具主体不会执行。

模型请求失败会写入 `request/error`；默认策略对传输/响应错误有限指数退避，对认证、配额
和请求参数错误不重试。每次获准重试还会写入 `request/retry`，但不会复制 user/message 或
创建额外 Step。应用可以通过 `request_retry_policy` 注入自己的异步决策策略。

## 待改进清单

当前问题、根因、优先级、参考 `deepseek-harness` 的实现和验收标准见
[IMPROVEMENTS.md](IMPROVEMENTS.md)。
