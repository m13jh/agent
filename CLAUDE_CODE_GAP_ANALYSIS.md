# `python-agent` 与 Claude Code 2.1.88 实现差距审计与改进建议

审计日期：2026-09-01
对照版本：`@anthropic-ai/claude-code-source` 2.1.88
审计对象：

- `/home/m13jh/projects/python_project/agent/python-agent`
- `/home/m13jh/projects/python_project/agent/collection-claude-code-source-code/claude-code-source-code`

## 结论先行

`python-agent` 已经不是一个只有“prompt → answer”的示例：它有类型化的模型边界、工具流水线、workspace 路径检查、审批、取消、单 Driver、Durable Inbox、JSONL 事件日志、崩溃尾部修复、并发工具、预算、重试、进程内子 Agent、可重放压缩、Skills 和 SQLite 派生索引。这些能力集中在约 60 个 Python 源文件里，结构清楚，适合作为可测试的 Agent 内核。

但它与 Claude Code 的差距不是“再加几个工具”这么简单，而是三个层次的差距：

1. **有若干当前就能触发的正确性和安全边界缺陷。** 最重要的是 Bash 没有 OS 级沙箱、通用流式协议没有真正执行完成性校验、观察者异常可以卡死 Agent、跨文件 patch 没有事务性。这些应在扩展能力之前修复。
2. **核心上下文和执行质量明显不足。** `python-agent` 使用五段静态系统提示，缺少项目指令、Git/环境上下文、自动 token 管理、自动压缩、模型 fallback、工具输出生命周期管理等 Claude Code 用来维持长任务质量的机制。
3. **产品/平台能力尚未覆盖。** 对照源码包含 40 余类工具、可配置权限规则、Bash/PowerShell 分析和沙箱、MCP、插件、动态 Skills、LSP、计划与任务板、后台任务、worktree、远程/桥接会话、SDK/结构化 I/O 和虚拟化 TUI；`python-agent` 只实现其中一小部分。

因此建议的目标不是复制约 51 万行的 Claude Code，而是保留 `python-agent` 的事件溯源内核，先补齐 P0 安全/正确性契约，再以小型、可替换的子系统逐步引入 Claude Code 中已经证明有价值的机制。

> **实现跟进（2026-09-01）**：审计后已在源码中补入通用流终止/`length` 执行闸门、
> EventBus 观察者隔离、bubblewrap Bash containment、文件事务与崩溃恢复、JSON-safe
> 工具结果、ToolCapabilities fail-closed 默认、子 Agent 能力继承、fork lineage 重置和
> 同步 child 结果去重。原下文的复现记录保留为修复前证据；当前回归测试为 106 passed。

## 1. 阅读范围、证据和可比性

### 1.1 实际扫描范围

| 项目 | 文件/代码量 | 说明 |
|---|---:|---|
| `python-agent/src/python_agent` | 60 个 Python 文件，约 9,952 行 | 逐文件阅读，包括入口、所有源模块和内置工具 |
| `python-agent/tests` | 15 个 Python 文件，约 3,238 行 | 全部测试文件逐文件阅读 |
| Claude Code `src` | 1,902 个 TS/TSX/JS 文件，约 513,237 行 | 全库文件清单、目录/LOC/符号扫描，并深读主执行链和每个关键子系统的入口文件 |
| Claude Code 测试 | 对照仓库没有 `test/spec/__tests__` 文件 | 不能把该仓库当作完整上游测试发布物 |

Claude Code 对照仓库自己的 README 明确说明：它是从 npm 单 bundle 提取的非官方、研究用途源码，发布包中约 108 个 `feature()` 分支模块不存在，内部功能和部分工具已被编译期 DCE 删除。见 [`README.md`](../collection-claude-code-source-code/claude-code-source-code/README.md#L1) 的 “Missing Modules Notice” 和 “Build Notes”。所以本文把它作为“当前可见实现和设计证据”，不把缺失的内部模块当作可交付 API，也不声称其未发布测试行为。

### 1.2 `python-agent` 当前基线

在项目指定的 `agent` Conda 环境（`/home/m13jh/miniconda3/envs/agent/bin/python`）实测：

```text
pytest -q                 -> 92 passed in 2.71s
ruff check .              -> All checks passed
mypy                      -> Success: no issues found in 60 source files
ruff format --check .     -> 失败：PYTHON_AGENT_TUTORIAL.md 有 3 处待格式化差异
coverage（source=src）    -> 78%（4,758 statements，1,056 未覆盖）
```

“测试全绿”不能等同于“生产边界完整”：覆盖率最低的区域正是 CLI 43%、DeepSeek 适配器 55%、搜索工具 47%；没有真实网络 SSE、跨进程写入、OS 沙箱、Windows/WSL、监听器失败、磁盘失败中途提交和子 Agent 能力继承的回归测试。

### 1.3 对照源码的关键入口

| Claude Code 机制 | 主要入口 |
|---|---|
| Agent 主循环、恢复、压缩、工具续接 | [`src/query.ts`](../collection-claude-code-source-code/claude-code-source-code/src/query.ts)、[`src/QueryEngine.ts`](../collection-claude-code-source-code/claude-code-source-code/src/QueryEngine.ts) |
| 工具契约和默认能力 | [`src/Tool.ts`](../collection-claude-code-source-code/claude-code-source-code/src/Tool.ts)、[`src/tools.ts`](../collection-claude-code-source-code/claude-code-source-code/src/tools.ts) |
| 流式工具并发和结果收敛 | [`src/services/tools/StreamingToolExecutor.ts`](../collection-claude-code-source-code/claude-code-source-code/src/services/tools/StreamingToolExecutor.ts)、[`src/services/tools/toolExecution.ts`](../collection-claude-code-source-code/claude-code-source-code/src/services/tools/toolExecution.ts) |
| 权限规则、工具专属检查、交互审批 | [`src/utils/permissions/permissions.ts`](../collection-claude-code-source-code/claude-code-source-code/src/utils/permissions/permissions.ts)、[`src/hooks/useCanUseTool.tsx`](../collection-claude-code-source-code/claude-code-source-code/src/hooks/useCanUseTool.tsx) |
| Bash 分析与 OS 沙箱 | [`src/tools/BashTool/BashTool.tsx`](../collection-claude-code-source-code/claude-code-source-code/src/tools/BashTool/BashTool.tsx)、[`src/tools/BashTool/bashSecurity.ts`](../collection-claude-code-source-code/claude-code-source-code/src/tools/BashTool/bashSecurity.ts)、[`src/utils/sandbox/sandbox-adapter.ts`](../collection-claude-code-source-code/claude-code-source-code/src/utils/sandbox/sandbox-adapter.ts) |
| 系统提示、Git/环境/CLAUDE.md 上下文 | [`src/constants/prompts.ts`](../collection-claude-code-source-code/claude-code-source-code/src/constants/prompts.ts)、[`src/utils/systemPrompt.ts`](../collection-claude-code-source-code/claude-code-source-code/src/utils/systemPrompt.ts)、[`src/context.ts`](../collection-claude-code-source-code/claude-code-source-code/src/context.ts) |
| 自动压缩、micro-compact、overflow 恢复 | [`src/services/compact/autoCompact.ts`](../collection-claude-code-source-code/claude-code-source-code/src/services/compact/autoCompact.ts)、[`src/services/compact/compact.ts`](../collection-claude-code-source-code/claude-code-source-code/src/services/compact/compact.ts)、[`src/services/compact/microCompact.ts`](../collection-claude-code-source-code/claude-code-source-code/src/services/compact/microCompact.ts) |
| 会话 JSONL、parentUuid 链、sidechain、resume/fork | [`src/utils/sessionStorage.ts`](../collection-claude-code-source-code/claude-code-source-code/src/utils/sessionStorage.ts)、[`src/history.ts`](../collection-claude-code-source-code/claude-code-source-code/src/history.ts) |
| API 流、usage、cache、重试、模型 fallback | [`src/services/api/claude.ts`](../collection-claude-code-source-code/claude-code-source-code/src/services/api/claude.ts)、[`src/services/api/withRetry.ts`](../collection-claude-code-source-code/claude-code-source-code/src/services/api/withRetry.ts) |
| 子 Agent、fork、worktree、后台/远程任务 | [`src/tools/AgentTool/AgentTool.tsx`](../collection-claude-code-source-code/claude-code-source-code/src/tools/AgentTool/AgentTool.tsx)、[`src/tools/AgentTool/forkSubagent.ts`](../collection-claude-code-source-code/claude-code-source-code/src/tools/AgentTool/forkSubagent.ts)、[`src/tasks/`](../collection-claude-code-source-code/claude-code-source-code/src/tasks/)、[`src/utils/worktree.ts`](../collection-claude-code-source-code/claude-code-source-code/src/utils/worktree.ts) |
| Skills、MCP、插件 | [`src/skills/loadSkillsDir.ts`](../collection-claude-code-source-code/claude-code-source-code/src/skills/loadSkillsDir.ts)、[`src/tools/SkillTool/SkillTool.ts`](../collection-claude-code-source-code/claude-code-source-code/src/tools/SkillTool/SkillTool.ts)、[`src/services/mcp/`](../collection-claude-code-source-code/claude-code-source-code/src/services/mcp/)、[`src/utils/plugins/`](../collection-claude-code-source-code/claude-code-source-code/src/utils/plugins/) |
| CLI、SDK、结构化 I/O、TUI | [`src/main.tsx`](../collection-claude-code-source-code/claude-code-source-code/src/main.tsx)、[`src/QueryEngine.ts`](../collection-claude-code-source-code/claude-code-source-code/src/QueryEngine.ts)、[`src/cli/`](../collection-claude-code-source-code/claude-code-source-code/src/cli/)、[`src/components/`](../collection-claude-code-source-code/claude-code-source-code/src/components/) |

## 2. 当前 `python-agent` 的实现画像

### 2.1 运行链和已经做对的事情

当前主链是：

```text
CLI/API
  -> AgentManager
  -> Agent（唯一 Driver Task）
  -> Inbox（next_turn / next_step）
  -> AgentLoop（Turn / Step）
  -> ModelAdapter / ModelRouter
  -> ToolRuntime（Pre -> Execute -> Post）
  -> Session Event
  -> derive_messages() -> 下一次 ModelRequest
```

关键实现分别在 [`core/agent.py`](src/python_agent/core/agent.py)、[`core/inbox.py`](src/python_agent/core/inbox.py)、[`core/agent_loop.py`](src/python_agent/core/agent_loop.py) 和 [`tools/runtime.py`](src/python_agent/tools/runtime.py)。当前内核的优点值得保留：

- `SessionEvent.seq` 连续、`SessionHeader/Event` 有独立版本号，事件数据拒绝非标准 JSON（[`session/events.py`](src/python_agent/session/events.py#L14)）。
- JSONL Store 遵循“先写入、flush、fsync，成功后才加入内存”，避免内存历史超过磁盘事实（[`session/session.py`](src/python_agent/session/session.py#L86)）。
- Inbox 的 insert/claim/replace/delete 全部进入事件，恢复时可以识别 claim 后尚未写入 `user/message` 的孤立消息（[`core/inbox.py`](src/python_agent/core/inbox.py#L189)）。
- 工具调用按模型顺序记账，连续只读调用进入有界并发池，exclusive 调用形成屏障，结果按模型顺序提交（[`tools/runtime.py`](src/python_agent/tools/runtime.py#L117)）。
- 路径 `resolve()` + `relative_to()` 能阻挡相对路径、`..` 和外部符号链接；敏感路径和动态 SessionStore 根也有过滤（[`tools/builtins/_paths.py`](src/python_agent/tools/builtins/_paths.py#L31)）。
- 修复策略默认严格失败，只有显式 `repair=True` 才修最后未换行的 JSONL 尾部或未闭合的最后 Turn/Step，并为物理修复留下备份（[`session/repair.py`](src/python_agent/session/repair.py#L1)）。
- `AgentPreset` 冻结且禁止额外字段，`mypy --strict` 通过，适配器协议和 ToolDefinition 都是可替换边界。

这些设计比“把所有状态塞进一个 REPL 类”更适合作为长期工程基础，不应因为追求 Claude Code 功能数而删除。

### 2.2 当前能力边界

实现明确只覆盖：Fake/DeepSeek、文本文件读写/patch、搜索/目录列表、审批和 workspace 路径策略、JSONL Session、单进程子 Agent、声明式 Skill、人工摘要压缩、SQLite 字面搜索和一个 prompt-toolkit TUI。README 也明确把远程 Provider、Code Mode、LSP、PTY、OS 沙箱、Web/RPC UI 列为未完成项。

这是一条合理的“小内核”路线，但下列实现细节与 README/教程中的安全承诺并不完全一致。

## 3. 与 Claude Code 的全功能差距矩阵

优先级含义：P0 = 可能造成越权、错误副作用、死锁或不可恢复数据不一致；P1 = 长任务质量、性能和可运维性；P2 = 明显产品能力缺失；P3 = 平台扩展。

| 领域 | `python-agent` 当前实现 | Claude Code 2.1.88 可见实现 | 不足、风险和改进方向 |
|---|---|---|---|
| 模型流协议 | `ModelChunk` 有文本、完整 `ToolCall`、finish reason、usage、`done`，但 `AgentLoop` 只把流当作“最终已组装列表” | `claude.ts` 逐个处理 `message_start`、`content_block_start/delta/stop`、`message_delta/stop`；原始 JSON 工具参数按 block 累积，stream watchdog 检测停流 | **P0**：当前通用适配器不验证 `done`、不保留 block 状态、不区分流关闭/length/畸形响应；应引入 Provider-neutral `StreamAssembler`，只有完整终止事件才产生可执行调用 |
| DeepSeek SSE | `aiter_lines()` 只处理单行 `data:`；直接 `json.loads`；要求 `[DONE]` 和 finish reason，但只对 DeepSeek stream 生效 | Anthropic SDK 原始 stream + 手工 content block assembler，记录 request id、TTFT、usage、stop reason，流中断可转非流式 fallback | **P0/P1**：多行 SSE、event/id、provider request id、非流式 length、Retry-After 和 idle watchdog 不完整；统一错误码并补真实网络/VCR 测试 |
| length/上下文溢出 | DeepSeek stream 在 `length` 时丢弃调用；`AgentLoop` 对普通/非流式 `AssistantResponse(finish_reason="length", tool_calls=[...])` 没有安全闸门 | `query.ts` 暂存并屏蔽 max-output 错误，可提高输出上限、最多恢复 3 次；413/context overflow 可 collapse/reactive compact 后重试 | **P0**：任何 `length`、未完成参数或缺少终止事件都必须“只产生错误结果，不执行工具”；随后有限提高预算或拆分任务 |
| API 重试和 fallback | 只对 `ModelError` 做基于字符串 marker 的有限指数退避；没有 `Retry-After`/jitter/request id；没有模型 fallback | `withRetry.ts` 识别 connection/408/409/429/5xx/529、`x-should-retry`、OAuth/Bedrock/Vertex 凭据，遵循 Retry-After、jitter、529 计数，可切 fallback model | **P1**：把异常从字符串升级为 `ModelError(code,status,retry_after,request_id)`；按 Provider 声明可重试性，支持主模型→备用模型并记录成本和切换原因 |
| usage、成本、缓存 | 有 prompt/completion/total 和可选美元字段；按 Turn 累计 Token/费用/墙钟 | 按模型累计 usage、cache read/create/delete、API duration、fast mode、task budget；请求参数会保持 prompt cache 前缀稳定 | **P1**：区分 input/output/cache token，预算要包含重试和摘要请求；请求 Header 记录诊断字段但应脱敏；可选 prompt cache 策略 |
| 工具契约 | Tool 只有 name/description/JSON Schema/timeout/`is_concurrency_safe`/execute；写工具和 Bash 通过名称集合识别 | `Tool` 还声明 `isReadOnly`、`isDestructive`、`isEnabled`、`interruptBehavior`、`validateInput`、`checkPermissions`、路径、搜索/读取语义、max result、渲染和 MCP 信息；`buildTool` 对并发和只读默认 fail closed | **P0**：名称不是安全策略。新增自定义危险工具可能被当作普通工具放行；引入 `ToolCapabilities`，默认 `read_only=false, destructive=true, open_world=true, concurrency_safe=false`，每个工具显式声明并在 Runtime 强制执行 |
| 参数验证 | 自己实现 JSON Schema 子集，覆盖 type/enum/const/required/range/length/pattern/items | 每个工具使用 Zod `safeParse`，调用前再跑工具专属 `validateInput`，错误可提示 Deferred ToolSearch | **P1**：支持 `oneOf/anyOf/allOf`, null union, format、条件 schema 或直接改用成熟 JSON Schema；限制正则复杂度，避免 ReDoS；错误应带结构化 path/code |
| Bash 权限 | 默认需 ApprovalService；`workspace-write` 只检查工具名称和 cwd；命令仍是任意 `bash -lc` | Bash 有 AST/argv 安全分析、只读命令判断、前缀/内容规则、敏感路径检查、交互审批、sandbox adapter、子进程/后台任务和环境处理 | **P0**：当前 `workspace-write` 不是 containment；批准后可以读写 workspace 外、访问网络、启动任意子进程。必须接 Landlock/bubblewrap（Linux/WSL2）、Seatbelt 或等价方案（macOS），Windows 使用受限进程/Job Object 或明确降级并阻止危险模式 |
| Shell 输出与交互 | Popen 写临时文件、轮询，结果在命令结束后一次返回；没有 PTY、后台任务、实时 stdout/stderr、持续 shell cwd | `BashTool` 支持进程组、超时、进度、前台/后台切换、输出文件、工具结果保留、图片输出和 shell cwd 语义；另有 PowerShell | **P1/P2**：增加 `ShellRunner`，区分 foreground/background/PTY，给输出设置字节上限和 spill quota；保留 stdout/stderr/exit code/被中断原因 |
| 文件读 | 读取完整 UTF-8 文件后再 splitlines，按字符/行窗口返回 | FileRead 支持读取上限、offset/limit、mtime cache、编码/换行、图片、PDF、Notebook，并能返回原生 image/document block | **P1/P2**：使用异步分块/按行读取；按字节和 token 双限额；加入 binary/media/PDF/notebook 处理或明确拒绝并引导专用工具 |
| 文件写/编辑 | `write_file` 原子替换单文件；`apply_patch` 先全部验证，随后逐文件 `write_text`/`unlink` | FileEdit 要求先读、检查 mtime/content 防止 stale write，支持唯一匹配/replace_all、编码和换行，文件历史可 rewind；FileWrite 同样记录前置状态 | **P0**：多文件 patch 不是事务，第二个文件失败会留下第一个文件；没有跨 Agent 文件锁、hash precondition、undo。应使用 stage→validate→backup→commit→rollback 的 `FileTransaction` |
| 输出裁剪/spill | `OutputPolicy` 以字符数裁剪，spill 文件名只含 call id，写入后不显式收紧权限/配额/owner | 每工具有 `maxResultSizeChars`；大型结果进入 tool-result store，有 preview、原始大小、路径和恢复提示；工具还可按语义自行限制 | **P1**：按 UTF-8 字节而非 Python 字符裁剪，记录截断原因；spill 按 Session/call 隔离、0600、quota、TTL、owner/source 元数据，避免跨 Session call id 覆盖 |
| 并发工具 | 连续并发安全工具进入 Semaphore 池；exclusive 形成 barrier，结果顺序稳定；仅在一个 Agent 内协调 | `StreamingToolExecutor` 可在模型仍流式输出时启动已完成的工具 block；区分 progress、queued、executing、completed，工具错误可中止 Bash siblings | **P1**：当前等整个模型响应后才执行，延迟更高；没有 sibling failure policy、progress channel、每工具 abort controller。先实现可靠的完整 block assembler，再选择性提前执行 |
| 取消/生命周期 | `cancel_event` + `Task.cancel()`；Runtime 尽力清理并发工具/Bash | AbortController 层级传播、工具 `interruptBehavior`（cancel/block）、synthetic tool_result、流资源释放、前后台 task 生命周期 | **P0**：EventBus 观察者异常会打破 Driver finally/idle；取消原因没有贯穿每个子资源。所有 cleanup 必须在 `try/finally`，观察者隔离且每个资源有 owner |
| 事件观察 | `LiveEventBus.emit` 按顺序 await 监听器，监听器异常直接向上抛；`emit_sync` 关闭 coroutine 但不报告 | Hook/stream/UI 事件和核心消息分层，进度/错误有独立通道；部分诊断 fire-and-forget，但关键状态有明确边界 | **P0**：观察者只能旁观，不能让 Agent 失败。每个 handler 单独 catch、超时、记录 observer_error；可配置 backpressure，不能阻塞 Session append |
| 系统提示 | `PromptAssembler.default()` 只有 identity/safety/workflow/tools/output 五段静态英文文本 | 动态生成工具指导、操作可逆性、安全规范、输出风格、模型/平台/日期/Git 状态、CLAUDE.md 层级记忆、MCP 指令、Skills、Agent persona、plan/auto mode | **P1**：缺少项目约定会显著降低代码任务质量；新增受信任的 `ProjectContextProvider`，加载 `AGENTS.md/CLAUDE.md` 等并标注来源、作用域和不可信边界，缓存静态前缀、动态部分单独管理 |
| 上下文压缩 | 只有显式 `ContextCompactor`，压缩完整旧 Turn；CLI 使用人工 `--summary`/`--summary-file` | autoCompact 按实际 context window/token 触发；micro-compact 清理旧 tool result；reactive overflow、context collapse、summary retry；压缩后恢复 plan/skills/recent files/async agents | **P1**：长会话会逐步超过 2048 输出/输入上下文；加入 token estimator、保留尾部、自动摘要 Provider、压缩失败 circuit breaker、摘要请求预算及恢复附件 |
| Session 真相源 | 事件类型、版本、连续 seq、strict projection、物理/语义 repair 很清晰；SQLite 是派生索引 | JSONL 记录 message/attachment/progress/summary/file-history/attribution/queue/sidechain/worktree，parentUuid 链支持 resume/fork/分支修复；写入使用 per-file queue 并可 flush | **P1**：当前每个事件同步 fsync，阻塞事件循环；`Session.messages()`/`next_step()` 每次全量扫描，长会话趋向 O(n²)；没有跨进程锁、hash、迁移链。保留严格事实源，增加异步有序 writer、索引投影和 schema migration |
| 崩溃恢复 | 对 claim orphan 和最后开放 Turn/Step 做保守修复，不自动重放状态不明工具 | 读取 parentUuid/leaf/sidechain，处理并行 tool result、tombstone、compact/snips、metadata、remote ingress、file history | **P1**：若已写 `user/message` 但模型尚未响应，repair 会关闭 Turn，原工作不会自动重新唤醒；需要显式 request state（accepted/started/completed）和可配置 resume policy |
| Fork | 精确复制事件快照，记录 `forked_from_session_id`；根用户 Session 测试通过 | `--fork-session`、fork subagent、sidechain、cache-safe inherited context、worktree/remote metadata | **P0/P1**：从 subagent fork 时当前实现保留旧 `parent_session_id/origin/delegation_depth`，会混淆 lineage；应清理 parent 字段并明确 fork vs delegation。还需 fork 预算/工具/权限快照 |
| 子 Agent | 仅进程内；独立 Session/Inbox/Driver；直接父级鉴权；深度和直接子数限制；结果 watcher inject 父 Inbox | AgentTool 支持 synchronous/background/fork/worktree/remote，Agent Definition/Markdown frontmatter/persona/model/permission mode，resume sidechain，后台任务和 task output | **P0/P1**：子 Agent 不继承父级自定义 `excluded_paths`、ApprovalService、policies/retry；多个 child 共享 workspace，可互相覆盖；`wait=true` 时结果同时 tool + user 注入；没有 child dispose、全局预算和跨进程树恢复 |
| 计划、Todo、任务板 | 没有计划模式、Todo 工具、持久任务图 | Enter/ExitPlanMode、TodoWrite、TaskCreate/Get/Update/List/Stop，任务有状态、依赖、owner、完成 Hook，结果持久化 | **P2**：复杂任务缺少可见计划和可恢复工作分解；先实现轻量 `PlanState`/task store，再接 Agent/团队 owner |
| Skills | `skill.toml + SKILL.md`，清单和按需加载有路径/大小/工具可用性校验 | 读取 user/project/managed/plugin/bundled/MCP 多来源；frontmatter 有 allowed-tools、model、context=fork、hooks、paths、disable-model-invocation、参数替换；支持动态按路径激活、优先级和去重 | **P1/P2**：当前 `allowed_tools` 只验证“注册表存在”，没有改变运行权限；没有多级来源、frontmatter、资源文件、参数替换、hooks/trust。应把 Skill 视为声明式能力包并显式授予最小权限 |
| MCP | 没有 MCP client/transport/auth/resource/tool registration | stdio/SSE/HTTP/WebSocket/SDK transport，动态 schema、资源、OAuth/PKCE/XAA、重连、server policy、MCP tool name normalization 和 elicitation | **P2**：这是外部生态能力，不应和 P0 混做；先定义 MCP adapter/生命周期，所有远程工具复用同一权限、SSRF、输出和审计接口 |
| 插件 | 没有插件安装、信任、市场或命令注入 | plugin manifest、marketplace、来源/作用域、路径校验、插件 Skills/Agents/MCP/hooks、启停/更新/错误 UI | **P2/P3**：插件是代码执行边界；实现前必须有签名/来源、权限声明、隔离和撤销策略 |
| 网络/Web | 没有 WebFetch/WebSearch | WebFetch 有 URL/domain permission、重定向限制、SSRF/网络策略、缓存和 markdown 上限；WebSearch 可限制 domains/uses | **P2**：不能直接把 HTTP client 暴露给模型；先实现 URL parser、DNS/私网阻断、redirect allowlist、响应大小和域名审批 |
| LSP/IDE | 没有 LSP、诊断、定义/引用、IDE bridge | LSP tool 覆盖 definition/references/hover/symbol/call hierarchy，文件变更通知 LSP/VS Code；有 IDE MCP transport | **P2**：代码质量和编辑反馈不足；可在工具协议稳定后增加独立 LSP service |
| CLI/SDK | `run/chat/sessions/transcript/repair/fork/compact/index/search`；仅 fake/deepseek；没有 max_tokens CLI；没有稳定 JSON/NDJSON SDK | `--print` text/json/stream-json、stream input/replay、`--max-turns`、`--max-budget-usd`、`--allowed/disallowed-tools`、`--tools`、`--mcp-config`、`--settings`、`--agents`、`--plugin-dir`、`--system-prompt`、`--add-dir`、`--fallback-model`、session id 等 | **P1/P2**：自动化集成和安全配置能力不足；先补 `--max-tokens`、`--output-format=json/stream-json`、stdin 协议和完整 preset fingerprint，再扩展配置层 |
| TUI/渲染 | 一个 prompt-toolkit renderer，能回放/折叠工具、滚动、显示轻量 Markdown；全屏 `assistant/delta` 直接忽略，等完整消息才渲染 | React/Ink 组件、Markdown marked + syntax highlight、virtualized message list/offscreen freeze、diff/file links、图片/粘贴、历史模糊搜索、vim/keybinding、permission dialogs、任务/团队面板 | **P2**：当前用户看不到全屏模式的实时文本，长会话全量重建 Buffer；实现增量文本块、虚拟列表、真正的 diff/高亮和审批 overlay |
| 可观测性/隐私 | 事件总线和 Session 有审计信息，没有 OTel/metrics/tracing/脱敏策略；request header 还保存完整 system/tool schema | analytics/OTel、API/tool spans、duration/TTFT/usage/cost、错误分类、按环境和 query chain 关联；同时有独立 privacy/telemetry 配置 | **P1**：应做本地默认、可选上报、字段级脱敏和 secret scrub；不要无条件复制 Claude 的第一方遥测行为 |
| 认证/配置/更新 | DeepSeek API key 从参数/env/.env 读入，只有一个 `AgentPreset`/CLI 参数层 | Anthropic OAuth/API key、Bedrock、Vertex、Foundry、keychain、安全存储、配置 source precedence、trust/onboarding、doctor/update/rollback | **P2/P3**：先抽象 CredentialProvider 和 settings layers；不要把密钥写入事件/日志；多 Provider 要有能力协商而不是字符串路由 |
| 工程质量 | Python 3.10、Pydantic、strict mypy、ruff；92 个测试，覆盖率 78% | 对照源码 tsconfig `strict=false`，测试不在该提取仓库；源文件大且含生成/feature-gated 内容 | Python 在类型可读性和可测试内核上反而更好；应补集成/property/fuzz/跨平台 CI，而不是按代码行数追平 |

## 4. 已复现的当前缺陷（不是功能数量差距）

以下实验使用 `agent` 环境、临时目录和内存，没有修改项目源代码。

### 4.1 `finish_reason=length` 的通用响应仍会执行工具（P0）

复现输出：

```text
length_with_tool_call_executed= 1
```

根因在 [`core/agent_loop.py`](src/python_agent/core/agent_loop.py#L244)：`_complete_response()` 收到流式 chunk 后直接把最后一批 `tool_calls` 放进 `AssistantResponse`，没有检查 `chunk.done`；在 [`run_turn()`](src/python_agent/core/agent_loop.py#L613) 之后，逻辑只判断 `if not response.tool_calls`，没有判断 `response.finish_reason == "length"`。因此 Fake/自定义/非流式 Adapter 若返回“工具调用 + length”，工具主体会执行。

DeepSeek 的专用 stream 在 [`deepseek_adapter.py`](src/python_agent/llm/deepseek_adapter.py#L251) 对 `length` 主动丢调用，只是 Provider 特例，不能替代协议层保证。

改进：

1. `ModelChunk` 增加严格的 `kind`/`stream_id`/`block_index`/`raw_arguments_delta` 状态，不再把 delta 假装成完整 `ToolCall`。
2. `StreamAssembler.finish()` 只有在收到合法终止事件、合法 finish reason 且每个工具 JSON 完整时，才输出 executable calls。
3. `length`、`stream_closed`、`malformed` 只产生不可执行的结构化 `ModelError` 或错误 `tool/result`，绝不进入 Runtime。
4. 对所有 Adapter（包括 complete-only）在 AgentLoop 入口再做一层防御性检查。

### 4.2 通用流缺少 `done` 仍会进入下一步/执行工具（P0）

复现输出：

```text
stream_without_done_executed= 1
```

`ModelChunk.done` 在 [`core/agent_loop.py`](src/python_agent/core/agent_loop.py#L253) 被读取但从未验证；`async for` 正常结束就被当成成功响应。`DeepSeekAdapter` 有 `[DONE]` 检查，但 `StreamingModelAdapter` 协议本身没有完成性契约。

改进：

- 将“网络流结束”和“Provider 明确发送终止帧”分开；没有终止帧统一为 `LLM_STREAM_CLOSED`。
- 把 `finish_reason`、收到的 chunk/event 数、累积参数字节数、Provider request id 写入 `request/end` 诊断事件。
- 用真实分片测试覆盖 UTF-8 边界、CRLF、多行 `data:`、事件乱序、半截 JSON、重复 DONE、取消和 idle timeout。

### 4.3 观察者异常可以让 Agent 永久处于 running（P0）

复现输出：

```text
observer_failure_when_idle= hung status= running driver_done= True
Task exception was never retrieved ... RuntimeError('observer failed')
```

[`core/agent.py`](src/python_agent/core/agent.py#L236) 在进入 `try` 之前就 `await self.event_bus.emit("agent/status", ...)`。订阅者抛异常后 Driver 直接结束，`_status` 没有回到 idle，`_idle_event` 也没有 set。即使异常发生在后续 `finally` 的状态通知，当前 [`LiveEventBus.emit`](src/python_agent/hooks/event_bus.py#L45) 仍会把它抛回调用者。

改进：

- `_drive()` 从第一条事件开始就包在 `try/finally` 中，finally 只做不抛异常的状态收敛。
- EventBus 对每个订阅者独立 `try/except`，记录 `observer/error`，可配置超时；observer 不能改变核心结果。
- `Session.event_listener` 同样不能让“已落盘事件”变成 Agent 错误；应将 listener 失败转为诊断事件。
- 增加 handler 抛异常、handler 超时、异步 handler 取消、无订阅者和重复 dispose 的测试。

### 4.4 `workspace-write` 不是 Bash 沙箱（P0）

复现输出：

```text
bash_can_read_outside_workspace= outside-readable
```

[`tools/policies.py`](src/python_agent/tools/policies.py#L225) 只按工具名限制 `bash`，[`builtins/bash.py`](src/python_agent/tools/builtins/bash.py#L47) 只用 `safe_path()` 检查 cwd，随后把任意命令交给 `bash -lc`。命令可以使用绝对路径、`cd`、网络、重定向、子 Shell、解释器和任意系统 API；环境变量只删除三个 API key（[`bash.py`](src/python_agent/tools/builtins/bash.py#L60)）。审批是“是否执行”的门，不是执行后的 containment。

Claude Code 的对应边界不是 prompt，而是 `sandbox-adapter.ts` 把权限规则转成文件读/写和网络限制，再由 sandbox-runtime/bubblewrap/Seatbelt 等 OS 机制执行；Bash 另有 AST 和只读/危险命令分析。

改进顺序：

1. 新增 `SandboxRunner` 抽象和 `SandboxPolicy`（workspace read/write、额外目录、deny paths、network allowlist、unix socket、资源上限）。
2. Linux/WSL2 默认 Landlock/bubblewrap，macOS Seatbelt，Windows 使用受限 token/Job Object；不支持的平台默认拒绝 Bash，而不是静默降级到裸 Shell。
3. 在 OS 沙箱之前做 shell AST 分析，只把结构化命令摘要交给审批 UI；不能用简单字符串黑名单。
4. 给 Bash 加显式“只读/破坏性/开放网络/需要交互”能力和逐次审批；`--approve-bash` 只能是明确的非交互部署选项，并在帮助中标注“全命令授权”的风险。

### 4.5 多文件 patch 会部分提交（P0）

复现输出：

```text
patch_error= True first_file_left_committed= True second_file_exists= False
```

[`ApplyPatchTool.execute`](src/python_agent/tools/builtins/apply_patch.py#L51) 的确先解析并验证所有操作，但 [`apply_patch.py#L78`](src/python_agent/tools/builtins/apply_patch.py#L78) 随后逐个写文件；没有 staging、备份、fsync、rollback 或恢复事件。磁盘满、权限变化、进程退出、第二个文件为目录等情况都会留下半个 patch。

改进：

- 建立 `FileTransaction`：读取并锁定所有目标 → 计算 hash/mtime precondition → 在同目录 staging → 生成完整 diff/预览 → 原子 commit → 失败 rollback。
- 删除操作先移入 Session 私有 trash/backup；事务提交和回滚各写结构化事件。
- 对跨 Agent 的相同路径使用 per-workspace/per-file lock；更复杂的并发编辑直接使用 worktree。
- `write_file` 和 patch 都按 UTF-8 字节 fsync 文件和目录，并收紧临时文件权限。

### 4.6 非 JSON 工具结果会在 Session 提交时炸掉（P1/P0 视工具来源）

复现输出：

```text
non_json_tool_result_outcome= ValidationError
```

`ToolResult.content` 是 `Any`（[`tools/types.py`](src/python_agent/tools/types.py#L51)）；`OutputPolicy` 只在超长时把内容序列化成文本，短结果保持原对象。工具返回 `Path`、bytes 或自定义对象时，Runtime 看似成功，但 `Session.append("tool/result")` 的 JSON validator 最后才失败，破坏整轮。

改进：在 Post 阶段统一 `to_json_safe()`，支持 `str/int/float/bool/null/list/object`，其他类型返回带类型名的错误结果；同时限制递归深度、对象数量和字节数。事件提交前必须经过同一序列化函数，不能把可执行对象或隐式 `repr` 送给模型。

### 4.7 Fork 的 lineage 在子 Agent 来源上错误保留（P0）

临时 Session 的复现输出：

```text
fork_from_child_lineage= parent subagent 1 child-source
```

[`JsonlSessionStore.fork`](src/python_agent/session/jsonl_store.py#L111) 只更新 `id`、`created_at`、`forked_from_session_id`，没有清除来源 Session 的 `parent_session_id`、`origin` 和 `delegation_depth`。当前测试只覆盖根用户 Session，所以未捕获这个分支。

改进：fork 必须有独立 lineage：`parent_session_id=None`、`origin="user"`（或明确的 `fork` 类型）、`delegation_depth=0`，同时保留 `forked_from_session_id` 和可选的 source capability fingerprint。补“从 root fork”和“从 child fork”两个测试。

### 4.8 子 Agent 没有继承父级自定义安全边界/审批（P0）

复现输出：

```text
child_inherits_custom_exclusion= False
child_inherits_approval_service= False
```

[`SubagentManager.start`](src/python_agent/subagents/manager.py#L187) 调用 `AgentManager.create()` 时只传 adapter、子工具、config、system prompt、workspace、parent id 等，没有传父 Agent 的 `excluded_paths`、`approval_service`、自定义 pre/execute/post policy、spill directory 或 retry policy。子配置虽然复制了 `approval_required`，但没有实际 `ApprovalService`。

这会产生两个方向的问题：

- 功能上，子 Agent 的 Bash 永远可能因为没有审批服务而拒绝，即使父级已接入 UI/审批。
- 安全上，父级通过 API 额外排除的私有目录不会传给 child；child 仍在同一 workspace 内执行。

改进：建立不可变 `CapabilitySnapshot`，由 parent 派生 child 时做单调收窄（workspace、paths、tools、network、budget、approver、hooks），并在 Header 中保存 fingerprint；拒绝任何未显式继承或扩大能力的 child。

### 4.9 `wait=true` 的子结果重复进入父上下文（P1）

复现输出显示同一 `CHILD_RESULT` 同时出现在：

```text
('tool', '{"answer": "CHILD_RESULT", ...}')
('user', '[subagent/result child_id=...]\nCHILD_RESULT')
```

[`subagents/tool.py`](src/python_agent/subagents/tool.py#L64) 在 `wait=true` 时把答案放进 `spawn_agent` 的 tool result；[`subagents/manager.py`](src/python_agent/subagents/manager.py#L347) 的 watcher 无论是否有人同步等待，都会向父 Inbox `inject` 同一答案。结果会浪费上下文，并可能让模型把同一事实当两条独立证据。

改进：为一次 submission 标记 `delivery_mode=wait|async`；同步等待只返回 tool result，异步 watcher 才 inject；或返回 child id 后统一由一条 `subagent/result` 事件投影。增加 wait/非 wait、父级已 idle/正在运行、重复 followup 的四组测试。

## 5. 重要但不应被忽略的质量和规模问题

### 5.1 Session 事件写入会阻塞事件循环

`JsonlSessionStore` 的 async API 内部直接做同步 `read_bytes`、`write`、`flush`、`fsync`；文档也明确说明当前不承诺多进程同时写同一 ID（[`jsonl_store.py`](src/python_agent/session/jsonl_store.py#L76)）。这保证了简单的 durable 顺序，却会在慢盘/NFS/大量工具输出时冻结 TUI、取消和其他 Agent。

建议保留两种语义而不是简单取消 fsync：

- **critical events**：用户输入、tool/write intent、tool result、turn end，进入每个 Session 的有序 writer，提供可等待的 durable ack。
- **diagnostic/progress events**：可批量、可丢弃或延迟 flush。

writer 可以运行在受控线程/异步文件后端，维护 per-session seq、文件锁和失败重试；`Session.append()` 的调用方仍能选择等待 durable ack。进程崩溃模型必须在设计文档中明确，而不是隐含在 `void enqueueWrite()`。

### 5.2 长 Session 的投影和编号趋向 O(n²)

`Session.messages()` 每次完整调用 `derive_messages()`（[`session.py`](src/python_agent/session/session.py#L136)），`next_turn()`/`next_step()` 也会扫描全量事件（[`session.py`](src/python_agent/session/session.py#L180)）。Agent 每个 Step 都重建系统 prompt、所有工具 Schema 和完整消息列表，长会话会同时放大 CPU、内存和日志体积。

建议：

- 事件仍是真相源，但维护受保护的增量 projection cache（last seq、message list、open call map）。恢复时一次重建，正常 append 增量更新。
- `request/header` 只记录 schema/prompt hash、非敏感摘要和必要版本，不重复写巨大的 system/tool 内容；需要审计时通过 content-addressed blob 引用。
- SQLite 使用 FTS5/倒排索引和增量更新；当前 `LIKE '%query%'` 既没有全文索引，也会同步阻塞调用线程。

### 5.3 工具实现会阻塞 Agent 事件循环

`ReadFileTool` 先把整个文件读入内存，`ListFilesTool` 先 `sorted(root.rglob("*"))` 再应用 `max_results`，`ApplyPatchTool` 和普通 `FunctionTool` 也可能执行同步 body。这些行为在小测试中没问题，在大仓库/大文件/阻塞自定义工具中会阻止取消和 UI 刷新。

建议所有工具声明执行类型：`async_io`、`thread_io`、`subprocess`、`cpu`；Runtime 对同步 body 使用受控线程池，对目录/文件使用分块读取，对搜索设置过程级超时和早停。

### 5.4 输出治理目前主要按字符，不按字节/语义

`OutputPolicy` 的 `text[:max_chars]` 是字符上限，不是 UTF-8 字节上限，因此实际请求字节数仍可能远超预期；Bash/rg 先把完整输出放入临时文件，再在结果阶段裁剪；spill 没有 Session owner、生命周期、全局 quota 和权限回归测试。Claude 的工具 contract 把 `maxResultSizeChars`、tool-specific output mapper、persisted path、preview 和 UI fidelity 分开。

建议统一 `OutputEnvelope`：

```text
raw bytes -> decode policy -> semantic preview -> model content
         -> persisted blob (session/call owner, size, checksum, ttl)
         -> UI rendering (可比模型内容更短)
```

### 5.5 Prompt 与项目上下文过于贫乏

[`prompt/assembler.py`](src/python_agent/prompt/assembler.py#L18) 的默认提示只告诉模型“你是 Python agent、工具输出不可信、给简洁答案”。它没有告诉模型当前 cwd、Git 分支/脏状态、项目规则、如何先读后写、何时使用 patch、如何报告验证结果，也没有 CLAUDE.md/AGENTS.md 层级和可信边界。

Claude 的 [`constants/prompts.ts`](../collection-claude-code-source-code/claude-code-source-code/src/constants/prompts.ts#L444) 把静态、动态、Session-specific sections 分开，[`context.ts`](../collection-claude-code-source-code/claude-code-source-code/src/context.ts#L116) 缓存 Git 和 CLAUDE.md 上下文；这种“静态 cache prefix + 动态 session suffix”的分层比简单拼一条长 prompt 更值得借鉴。

建议先实现最小版本：

- `ProjectInstructionProvider`：从 workspace 向上查找 `AGENTS.md`/`CLAUDE.md`，记录 path、scope、mtime、hash，支持显式禁用和额外目录。
- `EnvironmentContextProvider`：cwd、平台、shell、Git branch/status 的限长快照，失败不阻断主任务。
- `PromptSection` 增加 `source/trust/scope/cache_class`，项目文本标注为“用户/仓库数据”，不把其中的指令当作系统权限。
- 工具说明动态反映实际可用工具、权限模式和恢复方式。

### 5.6 压缩策略从“人工摘要”升级为“预算驱动的上下文管理器”

当前 `ContextCompactor` 只接受外部 `SummaryProvider`，CLI 默认是人工 `StaticSummaryProvider`（[`session/compaction.py`](src/python_agent/session/compaction.py#L44)），没有在请求前按实际 token 触发，也没有工具结果 micro-compact、overflow retry、压缩后恢复 plan/skill/file 状态。

Claude 的 `autoCompact.ts` 预留摘要输出 token、根据模型 context window 和 buffer 判定阈值；`microCompact.ts` 先清理旧工具结果；`query.ts` 在 413/max-output 时执行 collapse/reactive recovery，并设置失败 circuit breaker。

建议的 `ContextManager` 顺序：

1. 估算 system + tools + messages + output reserve。
2. 先清理可重建/低价值 tool result，并保留 locator。
3. 只压缩已关闭 Turn，摘要请求使用独立预算和同样的错误/重试协议。
4. 追加 `compact_boundary`/summary 事件，保留原事件和 source ranges。
5. 恢复最近访问文件、活动计划、未完成 child/task 和必要 Skill 元数据。
6. 若仍 overflow，有限次缩短 output、切换模型或提示用户拆分；禁止无界重试。

## 6. 分阶段改进路线

### P0：先把“不会越权、不会重复副作用、不会卡死”做成契约

#### P0-1 流式响应组装器

新增 `src/python_agent/llm/stream_assembler.py`（名称可调整）：

- 输入：Provider 事件/原始 delta。
- 状态：message id、block index、tool id/name、arguments raw bytes、finish reason、usage、done。
- 输出：`text_delta`（仅 UI）、`complete_response`、`stream_error`。
- 明确禁止：在 delta 阶段执行工具；`length`/缺 DONE/JSON 不完整时输出 executable calls。

验收：多 call 交错、多行 SSE、中文/转义字符、断流、length、取消、重复终止帧和 Provider request id 测试全部通过。

#### P0-2 生命周期和观察者隔离

- 重写 `Agent._drive()` 的 try/finally 结构。
- EventBus 增加独立 observer error/timeout channel，不让 UI/metrics/listener 改变 Agent 结果。
- `when_idle()` 在任何异常路径都可返回或抛出原始 DriverError，绝不无限等待。
- 为每个 Driver、工具组、Bash 进程组、child watcher 保留 owner 和 cancellation scope。

验收：监听器同步抛错、异步抛错、超时、取消时抛错、Manager shutdown 期间抛错均能回到 idle，且无 “Task exception was never retrieved”。

#### P0-3 Capability/Permission/Sandbox 三层

把当前按工具名称的 `PermissionPolicy` 扩展成：

```text
ToolCapabilities
  -> validateInput
  -> path/network/shell safety checks
  -> configured deny/ask/allow rules
  -> interactive approval (once/session/persistent)
  -> OS sandbox / process limits
  -> execute
```

工具默认按最危险能力处理；`read_only`、`destructive`、`open_world`、`requires_interaction`、`interrupt_behavior` 必须在定义中声明。Bash 的 workspace/write/network 限制必须由 OS 执行，不靠 system prompt。

验收：绝对路径/符号链接/`cd`/重定向/子 Shell/解释器/网络/环境秘密/子进程逃逸测试；没有可用沙箱时明确拒绝危险 Bash。

#### P0-4 文件事务和 stale-write 防护

- `FileTransaction` 支持单文件/多文件 stage、hash/mtime precondition、backup、commit、rollback。
- patch 提交前生成结构化 diff，提交后写 `file/change` 事件和 checksum。
- Session fork/child 若共享 workspace，默认拒绝并发写，或强制 worktree。
- spill、backup、temporary file 统一 0600/owner/session 权限。

验收：第二文件写失败、进程在 commit 各阶段退出、外部修改后 stale write、符号链接替换、跨平台换行/编码都不产生半提交。

#### P0-5 结果和配置快照

- Runtime Post 阶段统一 JSON-safe 化 ToolResult。
- `SessionHeader` 保存 capability/config fingerprint（模型、max output、工具 schema hash、权限规则 hash、workspace、sandbox mode、approval policy）。
- Resume 默认要求 fingerprint 匹配；允许变化时必须显式 `--resume-with-new-config` 并写事件。
- CLI preset id 不再手工拼接字符串；尤其不能遗漏 `max_tokens`、`approve_bash`、工具清单和自定义策略。

### P1：长任务质量、性能、可恢复性

#### P1-1 Provider 和重试层

实现一个 Provider-neutral `ModelClient`：

- Anthropic Messages、DeepSeek/OpenAI-compatible、Bedrock/Vertex 等作为独立 adapter。
- 连接池/客户端复用、HTTP status/header/body 结构化错误、Retry-After、jitter、取消和 stream idle timeout。
- 主模型 fallback、max-output escalation、context overflow recovery；每次尝试有 request id 和 usage/cost。
- `--max-tokens` 与环境/配置优先级：显式 API > CLI > env/.env > preset 默认。

#### P1-2 增量 Session projection 和 durable writer

- 保持 JSONL 为事实源；增加可验证的增量 projection cache。
- 每 Session 一个有序 writer，critical event 有 durable ack；诊断事件批量/可丢弃。
- 使用跨平台文件锁或 SQLite transaction 防止多进程同 ID 交叉追加。
- schema migration 从 version 1 开始可回放升级；repair 只改变明确的最后尾部，所有补偿都有 checksum/backup。

#### P1-3 ContextManager

- token estimator 和 context-window registry。
- 自动 compaction + micro-compact + overflow recovery。
- 保留最近 N 个完整 Turn、活动计划、工具调用配对、最近文件/Skill/child 状态。
- summary provider 失败 circuit breaker；摘要请求本身计入预算；可重复投影。

#### P1-4 项目上下文、工具治理和文件体验

- 可信的 `AGENTS.md/CLAUDE.md` 分层加载和动态失效。
- Read/FileEdit/FileWrite/Glob/Grep 四个清晰工具；文件读取按字节/token 限制，支持编码/二进制提示。
- FileEdit 要求先读并检测 mtime/hash；结果返回短摘要和 diff，不把大文件塞进 tool-call JSON。
- `search_text` 使用 fixed-string/regex 明确模式、rg 超时/早停、全局 byte cap 和 spill locator。

### P2：面向开发者的完整产品能力

按需求逐项增加，而不是把所有代码直接塞进 `AgentLoop`：

1. `PlanState` + Todo/Task store：任务状态、依赖、owner、resume 后恢复。
2. Background/PTY Shell：输出文件、TaskOutput、Stop、通知，所有 task 可取消和可回收。
3. Skills v2：frontmatter、来源优先级、`allowed_tools` 真正能力授予、参数替换、`context=fork`、hooks、资源文件和 trust。
4. MCP client：stdio/SSE/HTTP/WS、动态 schema、资源、OAuth、SSRF/域名策略、重连；MCP 工具与内置工具共用 Capability/Output/Session 协议。
5. WebFetch/WebSearch/LSP/IDE：各自的网络、文件、诊断边界和超时，不让 Bash 变成万能替代品。
6. 结构化 CLI/SDK：`--print --output-format=json|stream-json`、NDJSON input/replay、稳定 schema、错误码和 session id。
7. TUI v2：增量文本、虚拟化历史、Markdown/syntax/diff、文件链接、每次审批、历史搜索、图片粘贴、可配置 keybindings。

### P3：跨进程、远程和运维平台

- Agent definition/registry、fork/worktree/remote provider、跨进程父子树恢复。
- worktree 生命周期、脏变更保留、stale worktree 清理和显式合并策略。
- Remote bridge/RPC、JWT/OAuth/secure storage、连接/容量 backoff。
- 本地默认的可选 OTel/metrics，字段脱敏、用户可见开关、无 API key/完整工具输入泄露。
- CI 矩阵：Python 3.10/3.11、Linux/WSL/macOS/Windows；真实和 fake Provider；property/fuzz/security/performance test。

## 7. 测试补强清单

当前 92 个测试覆盖了已有 happy path 和若干阶段不变量，但应新增下列回归组：

### 流和模型边界

- `finish_reason=length` + 工具调用：0 次工具主体执行。
- stream 缺 DONE、缺 finish reason、HTTP 200 后半截关闭、超时、重复 DONE。
- 多个 tool block 交错 delta、UTF-8/反斜杠/换行/很大参数。
- 429/408/409/5xx/529、Retry-After、认证/余额不重试、fallback model。
- Adapter 返回未知 finish reason、usage 缺字段/额外字段、非对象 response。

### 安全和事务

- Bash 绝对路径、`../`、符号链接、`cd`、重定向、`bash -c`、Python/Node 解释器、网络、环境变量、后台子进程。
- 沙箱依赖缺失时的 fail-closed 行为和用户可读诊断。
- 多文件 patch 第二文件失败、删除失败、stale hash、外部并发修改、进程中途退出。
- 自定义危险工具默认拒绝/审批；`allowed_tools`、deny/ask/allow 优先级。
- ToolResult 返回 Path/bytes/object/cyclic object、深度/大小超限。

### 生命周期和恢复

- EventBus/Session listener 抛错不破坏 Driver；`when_idle()` 永不无期限挂起。
- cancellation 在模型、并发工具、Bash、审批 UI、child watcher 各阶段的资源回收。
- crash 窗口：insert→claim、claim→user、user→request、assistant tool call→result、result→turn end。
- root fork/child fork lineage；子 Agent 能力单调继承、custom exclusions/approval/policy 继承。
- `wait=true` 不重复注入；child dispose 后可重新创建，不被历史 completed child 永久占满配额。

### 性能和平台

- 百/千轮 Session 投影、内存、request build 和 SQLite rebuild 基准。
- 大文件/大目录/大量 rg 命中、stdout/stderr spill、Unicode byte boundary。
- 多进程同 Session 写入、NFS/慢盘 fsync、Windows/WSL/macOS 子进程和符号链接。
- 真实 TTY 与 pipe、JSON/NDJSON、终端 resize、长历史滚动和流式输出。

## 8. 不应直接照搬 Claude Code 的部分

Claude Code 源码提供了大量工程模式，但不应把它当作无条件规范：

- 代码里有大量 `feature()` 编译期分支、Ant-only 路径和源包缺失模块；复制名称并不会复制其后端服务、构建注入和运营约束。
- 其大规模遥测、GrowthBook/远程配置、隐藏/实验命令会带来隐私、可解释性和供应链风险；`python-agent` 应默认本地、显式 opt-in、字段脱敏。
- Claude 的 parentUuid/sidechain/metadata 修复是为其历史兼容和远程产品演化服务的；`python-agent` 可保持更简单的 typed event schema，但要把 lineage、补偿和迁移契约写清楚。
- `strict=false`、生成式/反编译 TS 和缺测试快照不能替代 `python-agent` 自己的 Python 类型、property test 和跨平台验证。
- 不要为了“工具数量对齐”先加入网络、插件、远程或自动执行；每个外部边界都必须先有 capability、approval、sandbox、output retention 和 audit 设计。

## 9. 建议的目标架构

在保留当前目录大体分层的前提下，可以演进为：

```text
AgentSupervisor
  ├─ AgentHandle / Inbox / CancellationScope
  ├─ SessionStore + OrderedDurableWriter + IncrementalProjection
  ├─ ContextManager（prompt sections / token budget / compact / recovery）
  ├─ ModelClient（provider adapters / StreamAssembler / retry / fallback）
  ├─ CapabilityRegistry
  │    └─ ToolRuntime
  │         ├─ InputValidator
  │         ├─ PermissionEngine（rules + approval + hooks）
  │         ├─ SandboxRunner（filesystem/network/process）
  │         ├─ ToolScheduler（parallel/exclusive/progress）
  │         └─ OutputStore（preview/spill/checksum/TTL）
  ├─ Task/Plan/Skill/MCP/Plugin services
  └─ CLI/SDK/TUI/RPC consumers
```

一次工具调用的强制顺序应是：

```text
raw model delta
  -> complete stream block
  -> schema + capability validation
  -> path/network/shell safety
  -> deny/ask/allow decision
  -> OS sandbox + resource limits
  -> transactional execute
  -> JSON-safe output + retention/spill
  -> durable tool/result event
  -> next ModelRequest projection
```

这条顺序同时吸收了 Claude Code 的关键经验和 `python-agent` 现有的事件溯源优点：模型可以失败和重试，工具可以并发，但任何副作用都必须在完整输入、明确授权、可回滚边界和可审计提交之后发生。

## 10. 最终判断

如果目标是“可用于真实代码库的 Python Agent”，优先级应是：

1. 立即修复流完成性、EventBus 死锁、Bash containment、patch 事务、结果 JSON 化和 child 能力继承。
2. 然后补自动上下文管理、项目指令、文件 stale-write、结构化 CLI/SDK、Provider/retry/fallback 和增量持久层。
3. 最后按产品需求选择 Plan/Task、后台任务、MCP/Skills/插件、LSP/IDE、TUI、worktree 和远程桥接。

`python-agent` 当前最有价值的资产是小而清晰的事件内核、严格恢复语义、类型边界和可测试性；Claude Code 最值得借鉴的是围绕这些边界建立的生产级能力契约，而不是它的全部代码规模或隐藏平台依赖。
