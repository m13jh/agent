# python-agent 待改进事项与技术债清单

本文档记录当前 python-agent 已暴露的问题、根因、参考实现、改进方向和验收标准。
它是后续开发清单，不代表其中的方案已经全部实现。

## 1. 当前基线

截至 2026-08-29，项目已经完成：

- 阶段 0：Python 包骨架、pyproject.toml、Makefile、pytest、ruff、mypy。
- 阶段 1：Fake/DeepSeek Adapter、Session 事件日志、模型—工具—模型闭环。
- 阶段 2：Agent Handle、AgentManager、双队列 Inbox、followup/steer/inject、单 Driver、取消和 Live Event Bus。
- 阶段 3：Pre/Execute/Post Waterfall、权限和路径策略、审批、write_file、apply_patch、bash、输出裁剪和 spill。
- 阶段 2扩展：python-agent chat 交互式终端和流式文本显示。
- 阶段 4：JSONL SessionStore、严格版本/序号校验、Inbox 重放、Session resume、
  transcript 导出，以及物理/语义崩溃尾部的保守修复。
- 阶段 5：有界 parallel/exclusive 工具调度、模型顺序结果提交、Turn 级 Token/费用/
  wall-time 预算，以及可注入的模型请求有限重试策略。
- 阶段 6：进程内 SubagentManager、独立 child Session、多 Turn followup、interrupt、
  直接父级鉴权、工具子集、最大深度/数量、结果通知和 child-first dispose。

当前全量检查基线：

~~~text
ruff：通过
mypy：通过
pytest：63 passed
~~~

以下功能属于架构文档后续阶段，本文不把它们误记为当前缺陷：

- 阶段 7：上下文压缩、Session fork、Skills、SQLite、Web API/UI。

## 2. 已发生的问题：流式工具参数不完整

### 2.1 复现过程

用户在交互终端中输入了一个需要检查目录并创建复杂 CMakeLists.txt 的任务：

~~~text
在 test 文件夹下展示一下 cmakelist.txt 的使用，来一个比较复杂的例子
~~~

模型先后调用了两次 list_files：

1. 查看当前目录。
2. 查看 test 目录。

随后模型准备创建一个复杂的 CMake 文件，但终端出现：

~~~text
ModelError: invalid DeepSeek stream tool arguments at index 0:
Unterminated string starting at: line 1 column 44
~~~

这说明错误发生在工具真正执行之前。模型返回的工具参数 JSON 类似只生成了一部分：

~~~json
{
  "path": "test/CMakeLists.txt",
  "content": "cmake_minimum_required(...
~~~

content 字符串和整个 JSON 都没有闭合。

### 2.2 根因

当前 DeepSeek 适配器在收到流式工具参数后进行字符串拼接，流结束时立即执行：

~~~python
arguments = json.loads(parts["arguments"])
~~~

当前 AgentPreset.max_tokens 默认值是 2048。这个值限制单次模型输出，而不是整个
输入上下文窗口。复杂文件内容、工具名称、JSON 参数和普通解释文字都会共同消耗这次
输出预算。

仅凭当前错误不能绝对证明一定是 max_tokens 达限，也可能是 SSE 流提前断开；但错误
已经确定表明工具参数没有完整到达。当前日志没有充分记录最终 finish_reason 和流关闭
原因，因此无法进一步区分这两种情况。

### 2.3 影响

- 工具主体没有执行，文件没有被部分写入，这是安全的。
- Agent Driver 会收到 ModelError，并结束本次任务。
- 用户看不到明确的“输出达到上限”或“网络流提前断开”提示。
- 事故发生时没有自动重试；阶段 5 已加入通用有限重试，但仍不能续写半截工具参数。

## 3. 与 deepseek-harness 的实现差异

本项目目录中的 deepseek-harness 已经针对同类问题建立了多层保护。

### 3.1 SSE 帧和 DONE 严格校验

相关文件：

~~~text
deepseek-harness/packages/llm/llm-deepseek/src/sse.ts
~~~

它使用 SSE parser 处理网络分片、UTF-8、CRLF、多段 data，并要求正常收到完整事件
终止符和 DONE。流中途结束会统一产生 STREAM_CLOSED，不会把半截事件当成完整响应。

### 3.2 工具参数保持原始字符串

相关文件：

~~~text
deepseek-harness/packages/llm/llm-deepseek/src/translate.ts
deepseek-harness/packages/llm/llm/src/assembler.ts
~~~

DeepSeek 的 arguments delta 只作为字符串累积。适配器不会在收到最后一个网络片段时
直接对半截 JSON 调用 JSON.parse。

### 3.3 max-tokens 时不执行不完整工具

BlockAssembler 会根据最终结束原因组装响应。如果原因是 max-tokens，它会丢弃工具
调用 block，只保留安全的普通文本，防止残缺参数进入执行器。

### 3.4 错误进入 Agent 恢复边界

相关文件：

~~~text
deepseek-harness/packages/llm/llm/src/index.ts
deepseek-harness/packages/core/agent-loop/src/agent.ts
deepseek-harness/packages/llm/llm-retry/src/index.ts
~~~

适配器异常会转换为结构化终止 chunk。Agent Loop 再进入 agent/request-error，
由 retry policy 决定是否进行有界重试。错误码、次数、退避时间和取消信号都会参与判断。

### 3.5 工具输出使用 retention/spill

搜索和 Bash 的原始输出、保留项目数量、单行长度、UTF-8 边界和模型可见内容分别控制。
超长结果保存到归属于当前 Session 的 spill store，模型只收到预览和恢复路径。

因此它把两类截断分开处理：

~~~text
模型响应截断 → finish reason / request error / retry
工具输出截断 → retention / spill / recovery locator
~~~

## 4. P0：模型响应和工具参数

### P0-1：引入真正的流式响应组装器

目标：工具参数不能在适配器层被过早解析。

建议：

1. 扩展 ModelChunk，支持 tool_call_delta、finish_reason、usage 和 done。
2. DeepSeek Adapter 只解析完整 SSE payload，不解析工具参数 JSON。
3. 新增类似 BlockAssembler 的 Python 组装器，按 call index 累积 call id、工具名称和原始 arguments。
4. 只有收到完整结束事件后，才尝试解析工具参数。
5. 参数不完整时绝不执行工具。

### P0-2：显式处理不完整响应

需要区分：

~~~text
LLM_STREAM_CLOSED
LLM_OUTPUT_MAX_TOKENS
LLM_MALFORMED_RESPONSE
LLM_TRANSPORT_ERROR
~~~

模型响应结束时记录：

- finish_reason；
- 是否收到 DONE；
- 已接收 chunk 数量；
- 已累积工具参数长度；
- Provider request id（如果有）。

如果是 max_tokens，应记录安全的 incomplete 状态，而不是只抛出 Unterminated string。

### P0-3：增加可配置的输出预算

当前 CLI 没有暴露 max_tokens，默认固定为 2048。建议支持：

~~~bash
python-agent chat --max-tokens 8192
~~~

以及：

~~~dotenv
DEEPSEEK_MAX_TOKENS=8192
~~~

优先级建议：

~~~text
构造函数显式参数 > CLI 参数 > 环境变量/.env > AgentPreset 默认值
~~~

必须明确区分：

~~~text
max_tokens：单次输出预算
context_window：输入 + 输出的总上下文容量
~~~

### P0-4：增加模型请求错误恢复策略

阶段 5 已完成通用部分：`request/error`、`request/retry`、可注入异步策略、有限指数退避、
墙钟预算与取消协同均已实现。这里剩余的是把 P0-1/P0-2 的结构化 SSE 错误码接入策略，
从而精确区分缺少 DONE、max_tokens、畸形工具参数和普通传输错误。

建议流程：

~~~text
request/error 结构化事件
    ↓
判断是否可恢复
    ↓
有界指数退避
    ↓
重新请求或让模型改用较小工具调用
~~~

建议默认策略：

- 网络断开：有限次数重试；
- SSE 缺少 DONE：有限次数重试；
- max_tokens：提高预算或要求拆分任务，不无限重试；
- 参数结构错误：重新请求一次，并说明工具参数没有完整生成；
- 用户取消：不重试；
- 认证错误和余额错误：不重试。

### P0-5：补充大工具调用集成测试

至少覆盖：

- 大段 write_file 参数被截断时不执行写入；
- finish_reason=length 时不执行工具；
- SSE 没有 DONE 时生成结构化错误；
- 工具参数跨多个 delta 后能正确拼接；
- JSON 字符串包含中文、换行、引号和反斜杠；
- 第一次工具失败后，模型可以看到错误并自我修正；
- 大文件拆成多次小写入后可以完成任务。

## 5. P1：上下文和工具协议

### P1-1：减少目录扫描噪声

当前 list_files 还应默认忽略：

~~~text
.mypy_cache
.ruff_cache
.pytest_cache
.python-agent
~~~

同时继续限制最大项目数和最大返回字节数。

### P1-2：按字节限制工具输出

当前 Python 实现主要使用字符长度限制。需要进一步支持：

- 原始 stdout/stderr 使用字节上限；
- 保留 UTF-8 边界；
- 记录是否发生 retention 截断；
- spill 文件记录 owner、source、call id；
- 模型收到明确的恢复路径。

### P1-3：减少大文件直接放进 JSON 参数

当前 write_file 把完整文件内容放进 JSON 参数，复杂文件容易消耗大量输出预算。
建议：

- 优先使用小型 apply_patch；
- 对超大文件采用分块写入；
- 给工具增加明确的内容长度提示；
- 对 patch 参数采用原始文本或 freeform 传输。

### P1-4：改进模型请求日志

request/header 还应增加非敏感诊断字段：

~~~text
stream_enabled
max_tokens
context_window
finish_reason
received_chunk_count
request_id
~~~

禁止写入 API Key 和完整敏感环境变量。

## 6. 已解决但需要保留回归测试的问题：Bash 子进程等待

原始实现：

~~~python
asyncio.create_subprocess_exec(...)
await process.communicate()
~~~

在当前 WSL/沙箱环境中连续执行多个 Bash 时，进程可能已经结束，但 asyncio 的子进程
等待机制没有及时收到退出通知，导致工具等待到 60 秒超时。

现在的 bash.py 使用：

~~~text
subprocess.Popen 创建进程
    ↓
stdout/stderr 写入临时文件
    ↓
异步轮询 process.poll()
    ↓
超时或取消时终止整个进程组
~~~

这解决了当前环境下的重复执行问题，并通过了连续 Bash smoke test。后续仍需补充：

- 长时间命令超时测试；
- 子进程树回收测试；
- 取消中 Bash 的进程组回收测试；
- stdout/stderr 超长 spill 测试；
- Windows/WSL 行为差异测试。

## 7. 当前可用的规避方式

在 P0 改动完成前，复杂代码生成建议拆分任务：

~~~text
先只检查 test 目录，不要扫描整个项目。
先创建最小版本的 CMakeLists.txt，不要一次生成复杂完整文件。
不要修改文件，先分段展示 CMakeLists.txt 内容。
~~~

尽量指定窄目录，避免把缓存文件送入模型上下文。

不要把 --approve-bash 当成解决模型输出截断的办法；该开关只影响 Bash 审批，与工具
参数 JSON 是否完整无关。

## 8. 建议实施顺序

~~~text
P0-1 流式 BlockAssembler
    ↓
P0-2 完整响应/finish reason 错误分类
    ↓
P0-3 max_tokens CLI 和 .env 配置
    ↓
P0-4 结构化错误码接入现有重试策略
    ↓
P0-5 大工具调用集成测试
    ↓
P1-1/P1-2 上下文和工具输出治理
    ↓
阶段 7 按实际需求选择高级扩展
~~~

## 9. 待确认的产品策略

1. max_tokens 默认值是否从 2048 提高到 8192，还是继续保持低默认值并要求复杂任务显式配置？
2. 发生 max_tokens 截断时，是自动重试、自动拆分工具调用，还是只提示用户重新描述？
3. 大文件写入是继续使用 write_file，还是优先设计分块写入和 freeform patch 协议？
