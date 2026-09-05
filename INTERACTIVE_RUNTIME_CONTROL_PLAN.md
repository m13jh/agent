# `python-agent` 交互式运行控制面与环境编排实施规划

## 0. 文档状态

- 基线：`76997ea os级别沙箱`
- 目标：让用户只需要启动 `python-agent`，就能在同一个终端会话中切换模型、切换对话、调整权限、控制网络、处理依赖安装和审批请求。
- 当前阶段：架构规划，尚未实现。
- 适用范围：当前 `python-agent` 的单进程 Agent、JSONL Session、Bubblewrap 沙箱、CLI/TUI、工具运行时和可注入审批服务。

本规划吸收“Sandbox 决定技术能力、Approval 决定策略是否放行”的两道门思想，但不直接复制外部 Agent 的全部实现。当前项目最重要的资产仍然是严格的事件日志、可回放 Session、可测试的工具边界和单 Driver 生命周期。

---

## 1. 结论先行

这个方向是合理的，也符合当前项目下一阶段的自然演进，但不能只修改 `sandbox.py` 或增加几个 CLI 参数。

当前实现的限制是结构性的：

1. `AgentPreset` 创建后不可变，模型、权限和运行限制在启动时固化。
2. `AgentLoop` 保存固定的 adapter/config，当前一条 Turn 内没有运行控制面。
3. `FullScreenTerminalUI` 负责展示和输入，但还不能改变 Agent 运行配置。
4. `SandboxRunner` 只为 `BashTool` 构造 Bubblewrap 命令，没有环境 Profile、网络 Profile 或运行时挂载配置。
5. `ApprovalService` 只有工具审批接口，没有待审批请求的终端交互 UI。
6. `AgentManager` 可以拥有多个 Agent，但 CLI 没有“当前 Session”概念，也没有终端内切换 Session 的控制器。
7. 当前 `Session Header` 的 capability fingerprint 包含模型、权限等启动能力；如果直接允许修改这些字段，恢复时会被现有指纹校验拒绝。

因此目标应该定义为：

```text
Terminal UI / CLI
        ↓
Runtime Control Plane
        ↓
AgentManager + 当前 Agent/Session
        ↓
每个 Turn 的不可变 Execution Snapshot
        ↓
Tool Runtime
        ↓
Permission Engine + Approval Service
        ↓
Sandbox Runner
        ↓
受限子进程
```

核心原则是：

> 运行中的操作不被控制命令直接改写；控制命令只改变下一个安全边界开始时使用的 Profile。

---

## 2. 当前基线与已有能力

### 2.1 当前启动链

现有 CLI 的主要路径位于 `src/python_agent/cli.py` 的 `_create_agent()`：

```text
解析 CLI 参数
    ↓
创建全部内置工具 Registry
    ↓
创建 FakeAdapter 或 DeepSeekAdapter
    ↓
创建不可变 AgentPreset
    ↓
创建 LiveEventBus
    ↓
创建 JsonlSessionStore
    ↓
AgentManager.create/resume
    ↓
Agent.followup
    ↓
Agent 单 Driver
```

启动参数目前决定：

- Provider 和 model
- `read-only` 或 `workspace-write`
- max steps、并发数、Turn 预算和模型重试
- 是否启用子 Agent
- Session Store 根目录
- 是否使用 `--approve-bash`

这些值一旦进入 Agent，当前没有公开的运行中修改入口。

### 2.2 当前已经可以复用的边界

| 现有组件 | 当前职责 | 对新目标的复用方式 |
| --- | --- | --- |
| `AgentManager` | 拥有、创建、恢复、释放 Agent | 作为多 Session 和当前 Agent 的所有权中心 |
| `Agent` | 单 Driver、Inbox、取消和 idle 收敛 | 保持单 Driver，增加运行 Profile 和控制命令边界 |
| `AgentLoop` | 模型 → 工具 → 模型 | 每个 Turn 固化一份 `ExecutionSnapshot` |
| `AgentPreset` | 创建时的不可变能力配置 | 作为默认值和静态能力上限，不再承载全部可变状态 |
| `Session` | 只追加事件和消息投影 | 记录 Profile 变更、环境状态和审批审计 |
| `LiveEventBus` | 实时观察事件 | 路由到当前 UI、后台 Session 通知和审批 UI |
| `ApprovalService` | 异步返回是否批准 | 继续作为统一审批协议，增加通用操作请求模型 |
| `ToolRuntime` | Pre/Execute/Post 策略 | 接入动态权限、网络 Profile 和结构化环境错误 |
| `SandboxRunner` | Bubblewrap 文件系统/网络隔离 | 增加 `SandboxSpec`、环境挂载和网络模式 |
| `JsonlSessionStore` | 事件耐久化和恢复 | 保存控制事件、环境快照和 Profile epoch |
| `FullScreenTerminalUI` | 单 renderer 全屏展示 | 增加控制命令反馈、审批对话框和 Session 切换 |

### 2.3 当前必须正视的实现事实

#### 沙箱只包住 Bash 子进程

当前文件工具、模型适配器、Session 写入和 Agent 本身都在宿主 Python 进程中运行。Bubblewrap 只在 `BashTool.execute()` 内启动命令。

因此“把当前 Conda 环境挂载到 `/opt/python-env`”首先影响的是沙箱中的 `bash`、`python`、`pytest` 和 `pip`，不会自动改变正在运行的 `python-agent` 进程本身。

这不是问题，但必须在设计中明确区分：

```text
Host Agent Runtime
    运行 python-agent、模型适配器、Session 和控制面

Sandbox Project Runtime
    运行模型要求执行的 bash/python/pytest/pip/npm 等命令
```

#### 当前 Bash 审批是“全有或全无”

`BashTool` 的静态能力声明是 `requires_approval=True`，CLI 只有在传入 `--approve-bash` 时才注入一个无条件返回 `True` 的回调。

所以当前行为是：

```text
没有审批服务 → 所有 Bash 拒绝
--approve-bash → 所有 Bash 自动批准
```

这不能直接满足“普通 `ls` 自动执行，删除和联网安装逐次确认”的目标，需要动态风险决策。

#### 当前 network fallback 不是严格断网

`SandboxRunner` 能使用 `--unshare-net` 时会隔离网络；如果主机不支持，它会退化为受限命令白名单。

受限命令白名单可以减少风险，但不能证明网络一定关闭。因此新设计的 `network=disabled` 必须在无法创建网络命名空间时 fail closed，而不能把“命令被限制”当作“网络已断开”。

---

## 3. 目标用户体验

### 3.1 默认启动

用户只执行：

```bash
python-agent
```

启动后显示：

```text
[环境检测]
Python: /home/m13jh/miniconda3/envs/agent/bin/python
运行环境: Conda agent
项目环境: workspace/.venv
沙箱: Bubblewrap 已启用
权限: workspace-write
网络: disabled
[环境检测] 依赖检查通过
```

如果缺依赖：

```text
[环境阻塞] 项目环境缺少依赖：httpx, pytest
[setup] 需要在 workspace/.venv 中创建/安装依赖
[网络] 当前 setup 阶段默认断网
[审批] 是否允许本次 setup 临时联网？
```

批准后：

```text
[setup] 创建 workspace/.venv
[setup] 使用受限沙箱联网安装依赖
[setup] 安装完成，网络权限已关闭
[Agent] 可以开始执行任务
```

### 3.2 运行中控制命令

建议的第一版命令：

```text
/status                         查看当前 Session、模型、权限、网络和环境状态
/model                         列出可用 Provider/模型
/model deepseek/deepseek-chat   切换后续 Turn 使用的模型
/permission                    查看当前权限 Profile
/permission read-only          后续 Turn 只读
/permission workspace-write    后续 Turn 允许写 workspace
/network                      查看网络状态
/network off                  关闭后续操作网络
/network setup                只允许 setup 阶段临时联网
/network on                   请求批准后允许工作区级网络
/sessions                     列出可切换 Session
/switch <session-id>          切换到已有 Session
/new                          创建新的 Session
/fork [target-id]             从当前 Session 创建分支
/setup                        重新检查/准备项目环境
/cancel                       取消当前执行并清空 Inbox
/cancel keep                  取消当前执行但保留 Inbox
/continue                     继续暂停任务
/transcript                   查看当前 Session transcript
/tools                        展开或折叠工具详情
/help                         查看帮助
/exit                         退出
```

### 3.3 修改的生效语义

终端显示必须明确告诉用户修改何时生效：

```text
/model deepseek/deepseek-chat
[已排队] 模型已设置为 deepseek/deepseek-chat，将从下一个 Turn 生效。

/permission read-only
[已排队] 权限将从下一个 Step 生效；当前正在执行的工具保持原 Profile。

/network off
[已提交] 网络关闭请求已记录；当前网络操作将被取消，后续操作使用 disabled。
```

不允许给用户造成“命令已经改变正在执行的子进程权限”的错觉。Linux mount namespace、网络 namespace 和已经创建的子进程不能安全地被事后修改，控制面只能取消它并在新边界重新启动。

---

## 4. 目标架构：控制面与执行面分离

### 4.1 控制面

建议新增一个 `RuntimeController` 或 `AgentSupervisor`，负责：

- 保存当前选中的 Session/Agent。
- 管理多个 Agent Handle 的切换。
- 串行化模型、权限、网络和 Session 变更。
- 在 idle、Turn 边界和 Step 边界执行变更。
- 管理待审批请求。
- 启动和恢复环境 setup。
- 把控制事件写入当前 Session。
- 把后台 Session 的状态转成当前 UI 可见通知。

建议接口形状：

```python
class RuntimeController:
    @property
    def state(self) -> RuntimeState: ...

    async def set_model(self, profile: ModelProfile) -> ChangeResult: ...
    async def set_permission(self, profile: PermissionProfile) -> ChangeResult: ...
    async def set_network(self, policy: NetworkPolicy) -> ChangeResult: ...
    async def switch_session(self, session_id: SessionId) -> ChangeResult: ...
    async def create_session(self, ...) -> Agent: ...
    async def fork_current(self, ...) -> Session: ...
    async def ensure_environment(self, *, force: bool = False) -> EnvironmentReport: ...
    async def shutdown(self) -> None: ...
```

控制器内部必须有一把变更锁：

```python
self._transition_lock = asyncio.Lock()
```

所有 `/model`、`/permission`、`/network`、`/switch`、`/new` 和 `/fork` 都通过它执行，避免多个终端输入同时改变当前指针或 Profile。

### 4.2 执行面

执行面仍由当前的 `Agent`、`AgentLoop` 和 `ToolRuntime` 组成，但每个 Turn 开始时生成一份不可变快照：

```python
@dataclass(frozen=True, slots=True)
class TurnExecutionSnapshot:
    session_id: SessionId
    model: ModelProfile
    permission: PermissionProfile
    network: NetworkPolicy
    environment: EnvironmentSnapshot
    tools: tuple[str, ...]
```

该快照用于整个 Turn，至少包括当前模型请求和它产生的工具链。控制命令不会修改快照，只会修改下一次 Turn 使用的 Profile。

### 4.3 两道门

```text
LLM 生成 ToolCall
        ↓
Permission Engine
        ├── deny
        ├── ask user
        └── allow
        ↓
Sandbox Runner
        ├── 文件系统能力
        ├── 网络能力
        ├── 进程能力
        └── 环境挂载
        ↓
业务工具/子进程
```

两者不能互相替代：

- Sandbox 负责“技术上能不能做到”。
- Approval 负责“现在是否应该允许”。
- 风险分类器只能辅助审批，不得作为文件系统或网络的最终安全边界。

---

## 5. 运行状态模型

### 5.1 Permission Profile

第一版保留当前的两种文件权限，并把网络单独拆出来：

```text
read-only
    workspace RO
    system RO
    network disabled

workspace-write
    workspace RW
    system RO
    network disabled

workspace-network
    workspace RW
    system RO
    network approved/allowlist

host-admin
    第一版不实现
```

`workspace-network` 不建议直接作为 `permission_mode` 字段，而是：

```text
filesystem_permission = workspace-write
network_policy = approved
```

这样文件权限和网络权限始终是两个维度，避免出现“允许网络就意外得到宿主写权限”。

### 5.2 Network Policy

```python
NetworkMode = Literal[
    "disabled",
    "setup-approved",
    "allowlist",
    "full",
]
```

阶段划分：

| 模式 | 第一版行为 |
| --- | --- |
| `disabled` | 强制使用 network namespace；不可用时阻塞 |
| `setup-approved` | 只对环境 setup 的指定安装命令临时开放 |
| `allowlist` | 需要代理或网络 broker；没有代理时明确提示未实现 |
| `full` | 仅作为未来显式高风险 Profile，不默认开放 |

网络权限必须是一次操作或一个短生命周期 Scope，而不是进程启动时永久打开：

```text
用户批准 setup 联网
    ↓
安装依赖
    ↓
setup scope 销毁
    ↓
Agent 回到 network=disabled
```

### 5.3 Model Profile

模型 Profile 至少包含：

```python
@dataclass(frozen=True, slots=True)
class ModelProfile:
    provider: str
    model: str
    max_tokens: int
    temperature: float
    max_retries: int
```

模型切换规则：

- 不能改变当前已经发出的 HTTP 请求。
- 不能改变当前 Step 已经生成的工具调用。
- idle 时立即应用。
- running 时记录为 `pending_profile`，从下一个安全边界生效。
- 如果用户使用 `/model --now`，必须先取消当前 Driver，再从新 Turn 使用新模型。
- Provider 或 API Key 不可用时，在切换前验证并拒绝，不能等到模型循环中才失败。

### 5.4 Environment Snapshot

环境快照只保存可审计、无秘密的数据：

```python
@dataclass(frozen=True, slots=True)
class EnvironmentSnapshot:
    kind: Literal["conda", "venv", "system"]
    interpreter: str
    prefix: str
    project_venv: str | None
    python_version: str
    dependency_fingerprint: str
    sandbox_backend: str
    network_namespace_supported: bool
```

禁止写入：

- API Key。
- 完整环境变量字典。
- 含 Token 的 URL。
- `.env` 内容。
- 可能泄露用户目录秘密的完整目录清单。

### 5.5 RuntimeState

```python
@dataclass(frozen=True, slots=True)
class RuntimeState:
    current_session_id: SessionId | None
    current_agent_id: SessionId | None
    model: ModelProfile
    filesystem_permission: Literal["read-only", "workspace-write"]
    network: NetworkMode
    environment: EnvironmentSnapshot | None
    environment_status: Literal[
        "unknown",
        "checking",
        "ready",
        "blocked",
        "installing",
    ]
    pending_approval: bool
    pending_changes: tuple[str, ...]
```

UI 只读这个状态快照，不直接读取或修改 `AgentLoop.config` 的内部字段。

---

## 6. 运行中修改的边界规则

| 操作 | 当前任务运行中 | 生效时间 | 说明 |
| --- | --- | --- | --- |
| 切换模型 | 允许排队 | 下一 Turn | 当前 HTTP 请求不变 |
| 降低权限 | 可请求立即取消 | 下一 Step 或取消后 | 不能事后重挂载 namespace |
| 提升 workspace 权限 | 必须审批 | 下一 Step/Turn | 不能扩大静态工具集合 |
| 关闭网络 | 允许立即取消当前网络操作 | 当前操作结束后 | 新操作使用 `disabled` |
| 开放网络 | 必须审批 | 新建子进程前 | 默认只限 setup 或一次调用 |
| 切换 Session | 默认等待或取消 | 目标 Session attach 后 | 不允许半个 Turn 切换上下文 |
| 修改 workspace | 禁止修改当前 Session | 新建 Session | Session Header 的 cwd 不可变 |
| 改 max steps/预算 | 只影响后续 Turn | 下一 Turn | 当前 Turn 快照保持稳定 |

必须区分“撤销”和“变更”：

- 撤销网络、取消工具和降低权限可以触发取消当前操作。
- 提升权限、开放网络和切换模型不能偷偷改写已经开始的操作。
- 所有变更都追加事件，保证重启后可以知道哪一 Turn 使用了什么 Profile。

---

## 7. Session 切换设计

### 7.1 AgentManager 作为所有权中心

当前 `AgentManager` 已经可以同时拥有多个 Agent，因此不需要另造一个 Session 进程池。需要新增的是控制器中的“当前指针”和 UI 路由：

```text
AgentManager
    ├── Agent A / Session A
    ├── Agent B / Session B
    └── Agent C / Session C

RuntimeController.current = Agent B
```

### 7.2 切换流程

```text
/sessions
    ↓
列出 Store 中的 Session Header 和当前内存 Agent
    ↓
/switch <id>
    ↓
检查当前 Agent 是否 running
    ├── idle → 直接切换
    ├── running → 等待、取消，或拒绝切换
    └── 有未保存审批 → 先处理审批
    ↓
加载/恢复目标 Session
    ↓
校验 preset、静态能力和环境兼容性
    ↓
UI 回放目标 Session
    ↓
当前指针切换
```

第一版建议：

- 当前 Session running 时，`/switch` 默认给出“请 `/wait` 或 `/cancel`”提示。
- 不在一次切换中自动取消用户正在执行的任务。
- 目标 Session 如果尚未在内存中，由 `AgentManager.resume()` 恢复。
- 目标 Session 的 workspace 必须来自持久化 Header，不能被 CLI 当前目录扩大。
- 旧 Agent 是否继续后台运行作为 P1；第一版优先保证 UI 不串流。

### 7.3 事件路由

当前部分 Loop 实时事件没有携带 `session_id`。需要统一补充：

```json
{
  "session_id": "...",
  "agent_id": "...",
  "turn": 2,
  "step": 3,
  "content": "..."
}
```

UI 规则：

- 当前 Session 的事件进入会话主视图。
- 非当前 Session 的事件进入轻量通知区或状态栏计数。
- 切换到后台 Session 时，从它的持久化事件和内存状态重新构建视图。
- 不用“全局最后一条 assistant 文本”判断当前 Session，必须按 ID 路由。

---

## 8. 自动检测 Python/Conda 环境

### 8.1 检测优先级

建议新建 `src/python_agent/environment/detect.py`，按用户要求检查：

```text
CONDA_PREFIX
    > VIRTUAL_ENV
    > sys.prefix
    > sys.executable / 系统 Python
```

但环境变量不能直接信任，必须验证：

1. 路径存在且是目录。
2. 路径经过 `resolve()` 后没有越过允许的根边界。
3. `bin/python` 或 `Scripts/python.exe` 存在且可执行。
4. 如果当前 `sys.executable` 位于该 prefix 外，记录 mismatch，而不是悄悄假设一致。
5. Conda 环境需要检查 `conda-meta/` 或明确的 Conda 标识。
6. 普通 venv 需要检查 `pyvenv.cfg`。

检测结果应包含来源：

```text
kind=conda
prefix=/home/m13jh/miniconda3/envs/agent
source=CONDA_PREFIX
interpreter=/home/m13jh/miniconda3/envs/agent/bin/python
```

如果 `CONDA_PREFIX` 是被手工伪造的无效路径，应报告环境不一致，并根据安全策略 fallback 到已验证的 `sys.prefix`，不能把任意环境变量路径直接挂载进沙箱。

### 8.2 环境挂载规则

只挂载当前环境：

```text
host: /home/m13jh/miniconda3/envs/agent
sandbox: /opt/python-env
mode: read-only
```

不挂载：

```text
/home/m13jh/miniconda3
其他 Conda env
conda 下载缓存
用户 home
~/.ssh
~/.aws
真实 .env
```

环境目录可能存在指向 prefix 外部的符号链接。挂载前需要做预检：

- 检查 Python 可执行文件是否能在只挂载 prefix 的命名空间中启动。
- 检查 `sys.prefix`、标准库、site-packages 和动态库是否可见。
- 如果 Conda 环境依赖 prefix 外部文件，不能静默把整个 Conda 根目录挂进来；应提示使用项目 venv、复制运行时或未来的 disposable rootfs。

### 8.3 环境变量规则

沙箱中的环境变量由 `SandboxRunner` 显式构造，不继承宿主全部环境：

普通 venv：

```text
VIRTUAL_ENV=/workspace/.venv
PATH=/workspace/.venv/bin:/opt/python-env/bin:/usr/local/bin:...
不设置 CONDA_PREFIX
```

Conda 作为当前基础环境、且没有 project venv 时：

```text
CONDA_PREFIX=/opt/python-env
PATH=/opt/python-env/bin:/usr/local/bin:...
```

如果已经选择 `workspace/.venv` 作为项目执行环境，不能为了方便同时把它伪装成 Conda 环境。`CONDA_PREFIX` 和 `VIRTUAL_ENV` 的语义必须与实际执行的 Python 环境一致。

---

## 9. 自动识别项目依赖

### 9.1 依赖文件优先级

P0 先实现：

```text
pyproject.toml
requirements.txt
requirements/*.txt
```

后续识别：

```text
environment.yml
Pipfile
poetry.lock
uv.lock
package.json
```

`package.json` 属于 Node 项目依赖，不能混入 Python venv；应由独立的 Node Project Environment 处理。

### 9.2 不执行依赖文件中的任意代码

依赖发现阶段只解析数据：

- 使用 `tomllib`/`tomli` 读取 TOML。
- 使用 `packaging.Requirement` 解析 PEP 508 requirement。
- 有限支持 `requirements.txt` 的 `-r`/`-c` 递归。
- 拒绝或明确提示无法自动处理的动态 shell、任意 Python setup 脚本和未知扩展语法。

不能为了“识别依赖”而执行：

```bash
python setup.py
source activate
conda activate
项目自定义安装脚本
```

### 9.3 包名和 import 名不能直接混用

例如：

```text
发行包：python-dotenv
import：dotenv
```

因此检查策略应结合：

- `importlib.metadata` 查询已安装 distribution。
- `importlib.util.find_spec` 检查 import module。
- `packages_distributions()` 建立已知映射。
- 对无法从 distribution 推导 import 名的依赖，优先执行受限的 `python -m pip check` 或只报告“声明存在但 import 映射未知”。

### 9.4 当前项目依赖

当前 `python-agent/pyproject.toml` 的运行依赖包括：

```text
httpx>=0.27
pydantic>=2.0
prompt-toolkit>=3.0
python-dotenv>=1.0
tomli>=2.0; python_version < '3.11'
```

开发依赖包括：

```text
pytest
pytest-asyncio
ruff
mypy
```

这个项目可以作为第一组端到端测试样例，但不能把当前项目的依赖硬编码为所有 workspace 的依赖。

---

## 10. Project `.venv` 与 Setup 阶段

### 10.1 职责划分

```text
Host Agent Runtime
    当前 Conda/venv
    只读
    运行 python-agent 本身

Project Runtime
    workspace/.venv
    可写
    安装项目依赖、运行 pytest/python 等

Agent Runtime
    setup 完成后默认 network=disabled
```

不建议把项目依赖安装到当前 Conda 环境，也不建议让模型使用 `sudo pip install`。

### 10.2 Setup 流程

```text
启动 RuntimeController
        ↓
检测 Host Python/Conda
        ↓
定位 workspace
        ↓
解析依赖声明
        ↓
计算 dependency_fingerprint
        ↓
检查 workspace/.venv
        ├── 环境完整 → 写环境快照，继续 Agent
        ├── 环境不存在 → 创建 .venv
        └── 依赖缺失/版本不满足 → 请求 setup 审批
                                  ↓
                            临时 setup network
                                  ↓
                            .venv/bin/python -m pip install
                                  ↓
                            关闭网络并再次检查
```

创建和安装必须由 `EnvironmentSetupRunner` 完成，而不是由模型产生一串 Bash ToolCall。

### 10.3 安装命令

优先使用环境内 Python：

```bash
/workspace/.venv/bin/python -m pip install --disable-pip-version-check --no-input -r requirements.txt
```

不能只调用裸 `pip`，也不能依赖用户是否在 shell 中执行过 `source .venv/bin/activate`。

setup runner 应具备：

- 固定 timeout。
- stdout/stderr 上限和 spill。
- 独立进程组。
- 取消时终止整个进程组。
- 非零返回码结构化记录。
- setup 完成后再次执行依赖检查。
- 只在 setup scope 内开放网络。

### 10.4 Setup 状态缓存

建议在 workspace 内保存机器可读状态，例如：

```text
workspace/.python-agent/environment-state.json
```

该文件不应被普通 `list_files`/`search_text` 暴露。状态内容：

```json
{
  "version": 1,
  "dependency_fingerprint": "...",
  "python_version": "3.11.x",
  "interpreter_kind": "conda",
  "project_venv": ".venv",
  "last_check": "...",
  "status": "ready"
}
```

依赖声明变化、Python 大版本变化或 `.venv` 损坏时重新检查。第一版不自动删除 `.venv`，只提示用户或重新创建到明确的安全路径。

---

## 11. Sandbox Runner 改造规划

### 11.1 从参数拼接升级为 SandboxSpec

当前 `SandboxRunner.build()` 直接接收 command、workspace、cwd、writable。建议改为内部使用结构化规格：

```python
@dataclass(frozen=True, slots=True)
class SandboxSpec:
    workspace: Path
    cwd: Path
    filesystem: Literal["read-only", "workspace-write"]
    network: NetworkMode
    host_environment: EnvironmentSnapshot | None
    project_venv: Path | None
    phase: Literal["agent", "setup", "diagnostic"]
```

外部 API 可以暂时保留旧 `build()` 兼容调用，但内部必须统一走 `SandboxSpec`。

### 11.2 挂载布局

推荐布局：

```text
/usr /bin /sbin /lib ...       只读系统运行目录
/opt/python-env                当前 Conda/venv，只读
/workspace                     项目 workspace，按 Profile RO/RW
/tmp                           独立 tmpfs
/tmp/home                      沙箱临时 HOME
/proc                          proc
/dev                           受控 dev
```

项目 `.venv` 位于 `/workspace/.venv`，随 workspace 挂载，不需要额外暴露宿主路径。

### 11.3 不要盲目复制 `--ro-bind / /`

你贴的示意中使用了：

```bash
--ro-bind / / \
--bind "$PROJECT" "$PROJECT"
```

这个模型对理解“系统只读、项目可写”很有帮助，但不应未经审查直接应用到当前项目。只读挂载不等于不可见，`--ro-bind / /` 仍然可能让模型通过 Bash 读取：

```text
~/.ssh
~/.aws
各种 .env
云 CLI 配置
浏览器配置
其他用户数据
```

当前实现只挂载必要系统目录，虽然兼容性较窄，但默认暴露面更小。新版本应优先扩展“最小只读挂载集合”，需要 CA 证书、动态库或 `/etc` 文件时逐项增加，不要一次性暴露整个宿主根目录。

### 11.4 网络模式

`SandboxRunner` 需要明确处理：

```text
network=disabled
    必须 --unshare-net
    不支持 → EnvironmentBlocked

network=setup-approved
    只允许 setup runner 使用
    每次批准绑定具体依赖操作和命令摘要

network=allowlist
    没有代理/broker → 不可用

network=full
    第一版不开放给普通 Agent Tool
```

不允许把“当前环境检测到 network namespace 失败”自动转换为 host network。

### 11.5 Bubblewrap 可用性

当前找不到 bwrap 会拒绝 Bash，这是正确的 fail-closed 行为。新 setup runner 也必须遵循：

- agent phase 没有 bwrap：不能运行 Bash。
- setup phase 没有 bwrap：不能偷偷在宿主执行安装。
- 可以给用户明确提示如何手动安装 Bubblewrap，或未来选择 Docker/Podman 后端。
- 多平台后端作为 P2，不在第一版引入静默降级。

---

## 12. 审批系统与终端 UI

### 12.1 复用接口，不直接复用 `ApprovalPolicy`

当前 `ApprovalPolicy` 是 `ToolRuntime` 的 Pre Waterfall 中间件，只能处理一个 `ToolInvocation`。setup 是系统控制面操作，不应该伪装成模型工具调用。

正确复用方式是：

```text
复用 ApprovalService 协议
新增通用 ApprovalRequest 类型/字段
新增 InteractiveApprovalService
```

建议把现在强绑定工具的请求扩展为：

```python
class ApprovalRequest(BaseModel):
    request_id: str
    operation_type: Literal[
        "tool",
        "setup",
        "network",
        "permission",
        "host_access",
    ]
    title: str
    summary: str
    target: str | None
    impact: str
    sandbox: str
    network: str
    arguments: dict[str, Any] = {}
```

兼容工具调用时保留 `call_id` 和 `tool_name`，setup 请求不要强行伪造 ToolCall 身份。

### 12.2 InteractiveApprovalService

```text
Permission Engine 创建 ApprovalRequest
        ↓
InteractiveApprovalService.request()
        ↓
RuntimeController 记录 pending_approval
        ↓
FullScreenTerminalUI 显示审批卡片
        ↓
用户选择 allow-once / deny
        ↓
Future.set_result()
        ↓
继续或返回拒绝
```

审批请求必须由当前 UI renderer 管理，不能在全屏运行时直接调用 `input()` 或 `print()`，否则会破坏 prompt-toolkit 的屏幕状态。

纯文本/非 TTY 模式可以：

- 使用异步终端输入实现简化确认；或
- 没有明确交互能力时默认拒绝并返回 `environment_blocked`/`approval_required`。

不能为了让 CI 通过而自动批准高风险操作。

### 12.3 审批范围

默认只支持：

```text
本次操作
```

setup 可以有单独的短生命周期 Scope：

```text
允许本次依赖安装过程联网
```

不默认提供“本进程所有 Bash 永久批准”。如果未来支持“本 Session 允许某类操作”，也必须：

- 展示精确范围。
- 记录授权事件。
- 支持 `/revoke`。
- 在 Session 切换时重新确认。

### 12.4 Bash 风险分类

当前所有 Bash 都审批，需要改成动态判断。建议增加：

```python
class CommandRiskEngine:
    def assess(self, command: str, context: CommandContext) -> RiskAssessment: ...
```

它输出的是策略建议，不是安全隔离：

```text
safe
    pwd, ls, git status, python --version

workspace-write-risk
    rm -rf build, git clean, git reset --hard, chmod

network-risk
    pip install, npm install, curl, wget, git clone

host-risk
    sudo, apt, systemctl, docker socket, /etc 外部写入

unknown
    默认 ask 或 deny
```

不要只写：

```python
command.startswith("rm")
```

`python -c`、`find -delete`、重定向、shell 函数和脚本都可能产生副作用。最终安全仍由 namespace、权限和挂载决定。

### 12.5 Host Admin

第一版不实现“批准后脱离沙箱重试”的通用能力。原因是：

- 这会把模型请求升级为宿主机操作。
- `sudo` 认证、用户身份和系统状态不可由普通 ToolRuntime 安全接管。
- 当前项目没有 privileged helper、命令审计和回滚机制。

未来如需 apt/systemctl，应使用独立的 host helper 或 disposable development environment，不应直接让 Agent 获得宿主 root。

---

## 13. 环境错误、快速暂停与恢复

### 13.1 新增错误分类

建议在 `errors.py` 增加：

```python
class EnvironmentError(AgentError):
    """运行环境检查、依赖或沙箱能力失败。"""


class EnvironmentBlockedError(EnvironmentError):
    """需要用户改变环境/权限后才能继续。"""
```

错误应该携带结构化 code：

```text
environment_blocked
dependency_missing
dependency_install_failed
network_blocked
sandbox_unavailable
runtime_environment_invalid
```

### 13.2 ToolResult 扩展

当前 `ToolResult` 只有 `is_error`，Runtime 将异常转换成普通字符串。建议增加：

```python
error_code: str | None = None
error_category: str | None = None
pauses_task: bool = False
```

例如：

```json
{
  "call_id": "...",
  "name": "bash",
  "content": "缺少依赖 httpx，且当前网络未授权",
  "is_error": true,
  "error_code": "environment_blocked",
  "pauses_task": true
}
```

### 13.3 AgentLoop 行为

当某个工具返回 `environment_blocked`：

```text
记录所有已经产生的 tool/result
    ↓
关闭当前 Step，reason=environment_blocked
    ↓
关闭当前 Turn，reason=environment_blocked
    ↓
RunResult.task_status = paused
    ↓
通知 UI
    ↓
不再让模型继续搜索和重复执行命令
```

`task_status_for_finish_reason()` 必须把 `environment_blocked` 映射到 `paused`，不能落入当前“未知原因默认 completed”的分支。

### 13.4 重复失败熔断

第一版可以按以下规则实现：

```text
同一规范化命令 + 同一错误分类连续失败 2 次
    → 生成 failure circuit breaker 事件
    → 暂停当前任务

dependency/network/sandbox 环境错误出现 1 次
    → 立即暂停
```

命令签名只保存 hash 或截断摘要，避免把命令中的 Token 写入日志：

```text
failure_signature = sha256(normalized_command + error_code)
```

重复失败计数需要区分 Turn 和 Task：

- Turn 计数用于防止一个模型循环原地打转。
- Task 计数用于防止用户 `/continue` 后无限重复同一失败。

### 13.5 `/continue` 语义

环境阻塞后：

```text
/setup
    ↓
重新检查/安装依赖
    ↓
成功 → /continue 可用
失败 → 仍保持 paused
```

`/continue` 不应直接绕过环境检查，也不应把 `environment_blocked` 改写成普通 `completed`。

---

## 14. Session 事件与恢复模型

### 14.1 控制事件

建议增加以下事件类型：

```text
runtime/profile_changed
runtime/session_selected
runtime/session_created
environment/detected
environment/check
environment/setup_start
environment/setup_end
environment/blocked
approval/requested
approval/resolved
```

这些事件不应成为普通模型消息，应该：

- 使用 `ignorable=True`；或
- 加入 projection 的控制事件白名单。

事件数据必须 JSON-safe，不能保存秘密。

### 14.2 Profile epoch

当前 capability fingerprint 把配置整体作为恢复约束。动态切换后建议拆成两层：

```text
Static Capability Envelope
    workspace 根目录
    工具集合
    excluded paths
    静态策略实现
    最大预算/最大深度

Runtime Profile Epoch
    当前 provider/model
    filesystem permission
    network mode
    setup 状态
```

静态能力仍然必须严格匹配；模型、网络和部分运行权限可以通过事件记录为多个 epoch。

建议：

- 工具集合、workspace、excluded paths 不能在当前 Session 中无审批扩大。
- 模型可以切换，实际值由每个 `request/header` 记录。
- 网络和权限变更由 `runtime/profile_changed` 记录。
- 新 Agent 恢复时默认使用 Session 最近一次有效 Profile，但如果当前环境不兼容，先阻塞，不静默降级。

### 14.3 Header 与事件版本

新增字段时要遵守现有 Header/Event 版本规则：

- 可选兼容字段可以在旧 Header 上提供默认值。
- 新增必需语义或改变回放含义时，升级版本号。
- 旧版本事件不能因为新增 UI 控制功能而无法投影。
- `environment/blocked` 等事件必须在恢复和 transcript 中有明确、可预测的处理方式。

### 14.4 Session 切换不复制历史

切换只改变当前指针：

```text
Session A events.jsonl
Session B events.jsonl
        ↓
RuntimeController.current_session_id
```

不能把 Session A 的消息追加到 Session B，也不能用 UI 缓存作为当前对话事实源。UI 切换时必须从目标 Session 事件重新投影。

---

## 15. AgentLoop 与 Agent 改造

### 15.1 AgentLoop 使用动态 Profile Provider

当前 `AgentLoop` 直接从 `self.config` 读取 provider、model、max steps、permission 等配置。建议改成：

```text
AgentPreset
    创建时静态默认值和上限

RuntimeProfileProvider
    当前待生效 Profile

run_turn()
    读取一次 Profile
    创建 TurnExecutionSnapshot
    后续 Step 使用同一 Turn Snapshot
```

为了降低改动风险，第一版可以保留 `AgentPreset`，新增一个 `AgentRuntimeProfile`，让 `AgentLoop` 在 `run_turn()` 开始时合并两者，而不是修改 frozen Pydantic 对象。

### 15.2 模型路由

当前已有 `ModelRouter`，但 CLI 默认直接创建一个 adapter。要支持终端切换，建议：

1. 启动时创建一个可用 Provider Registry。
2. `ModelRouter` 按当前 Profile 的 provider 解析 adapter。
3. DeepSeek adapter 可以延迟初始化，但切换前必须验证 API Key、base URL 和 timeout。
4. Fake adapter 继续作为离线默认 Provider。
5. 每次 `request/header` 写入实际 provider/model。

切换模型不会重写历史，也不会复制 user/message；下一次模型请求自然看到同一个 Session 投影。

### 15.3 工具 Profile

工具 Registry 的静态集合和当前可执行权限必须分开：

```text
ToolRegistry
    当前进程已注册的工具

Static Allowed Tools
    Session 能够使用的工具上限

Runtime Permission Profile
    当前 Turn 是否允许写、联网、执行高风险工具
```

用户可以在预先注册的工具集合内切换权限；不应让运行中的普通命令动态加载任意 Python 插件并立即获得能力。

### 15.4 取消和 Profile 变更

控制器调用 `Agent.cancel()` 后，必须等待：

- Driver Task。
- 当前 model request。
- 并发工具 Task。
- Bash 子进程组。
- 待审批 Future。

只有全部收敛后，才将新的 Profile 设为当前。这样不会出现 UI 显示“网络已关闭”但旧网络子进程仍然运行的窗口。

---

## 16. CLI/TUI 改造

### 16.1 `FullScreenTerminalUI`

当前 TUI 已经是单 renderer，适合继续扩展：

- 底部状态栏显示当前 Session、模型、权限、网络、setup 状态。
- 增加控制命令反馈块。
- 增加审批卡片和按键绑定。
- 增加 Session 列表选择界面。
- 非当前 Session 的后台事件只显示通知，不污染主对话。
- setup 期间禁用可能改变当前 workspace 的冲突操作，或排队处理。

审批卡片示例：

```text
┌─ Approval required ─────────────────────────┐
│ 操作：安装项目 Python 依赖                   │
│ 目标：PyPI                                    │
│ 影响：写入 workspace/.venv                   │
│ 网络：仅 setup 阶段临时开放                   │
│ 沙箱：Bubblewrap 仍然启用                     │
│                                              │
│ [Enter] 允许本次   [Esc] 拒绝                 │
└──────────────────────────────────────────────┘
```

### 16.2 输入分层

建议不要继续在 `_dispatch_chat_line()` 中堆叠更多字符串判断，增加：

```text
ChatLineNormalizer
    ↓
RuntimeCommandParser
    ├── AgentInput
    └── RuntimeCommand
            ↓
RuntimeController
```

这样 `/model`、`/switch` 和 `/network` 的错误可以在进入 Agent 前被确定性地验证，也便于测试。

### 16.3 Plain 模式

`--plain` 仍然必须支持全部核心控制能力：

- 命令解析不依赖全屏布局。
- 审批使用明确的文本提示。
- 无 TTY 时不自动批准。
- Session 切换后打印新的 Session ID 和 Profile。

---

## 17. 建议的模块拆分

### 17.1 新增模块

```text
src/python_agent/environment/
    __init__.py
    types.py
    detect.py
    dependencies.py
    setup.py
    snapshot.py

src/python_agent/runtime/
    __init__.py
    types.py
    controller.py
    commands.py
    profiles.py

src/python_agent/approval/interactive.py
```

### 17.2 现有模块改造

| 文件 | 主要改造 |
| --- | --- |
| `core/lifecycle.py` | 增加 `environment_blocked` 等暂停原因 |
| `core/agent.py` | 允许 Controller 管理 Profile 变更和当前 Agent 状态 |
| `core/agent_loop.py` | 每 Turn 固化 Execution Snapshot，识别结构化环境错误 |
| `core/agent_manager.py` | 支持当前 Agent/Session 路由和动态 Profile 恢复 |
| `config.py` | 保留不可变 preset，增加环境/网络默认配置或独立配置模型 |
| `errors.py` | 增加 EnvironmentError、结构化错误 code |
| `tools/types.py` | 扩展 ToolResult error code 和暂停语义 |
| `tools/runtime.py` | 保留结构化错误，不把环境阻塞降级成普通文本 |
| `tools/policies.py` | 支持动态审批原因和风险结果 |
| `tools/builtins/bash.py` | 使用 SandboxSpec，返回结构化失败信息 |
| `tools/sandbox.py` | 增加挂载、网络和 phase Profile |
| `approval/service.py` | 扩展通用审批请求，保持旧工具 API 兼容 |
| `session/events.py` | 增加控制事件数据约束/版本支持 |
| `session/projection.py` | 处理新的 ignorable 控制事件 |
| `session/jsonl_store.py` | 保存环境状态和 Profile 事件，继续保证 fsync |
| `cli.py` | 创建 Controller、命令解析和 setup 生命周期 |
| `terminal_ui.py` | 审批 UI、Session 切换和运行状态展示 |

### 17.3 不建议的拆分方式

不建议：

- 把环境检测塞进 `DeepSeekAdapter`。
- 把依赖安装塞进 `BashTool` 的普通执行逻辑。
- 把 `/model` 解析塞进 `AgentLoop`。
- 让 UI 直接修改 `AgentLoop.config`。
- 用新的全局变量保存当前 Session。
- 用事件总线 observer 返回值控制是否允许执行。

这些做法会混合 Provider、UI、策略和生命周期，最终难以恢复和测试。

---

## 18. 分阶段实施顺序

### Phase 0：控制面基础和兼容层

目标：不改变现有安全语义，先建立运行时状态和变更边界。

任务：

1. 新增 `RuntimeState`、`ModelProfile`、`NetworkPolicy`、`PermissionProfile`。
2. 新增 `RuntimeController`，管理一个当前 Agent。
3. 把现有 CLI 创建逻辑包到 Controller 初始化流程中。
4. 增加 `/status`、`/model` 查询、`/permission` 查询、`/network` 查询。
5. 给实时事件补充 `session_id`/`agent_id`。
6. 保留旧 CLI 参数作为兼容入口，但在文档中标记为启动时默认值。

验收：

- `python-agent` 正常运行。
- `/status` 能显示当前状态。
- 没有任何控制命令可以修改当前正在执行的请求。
- 现有 Session/工具测试不退化。

### Phase 1：终端内模型和 Session 切换

目标：实现用户最直接感知的交互切换。

任务：

1. CLI 使用 `ModelRouter` 而不是固定单 adapter。
2. 增加 `/model` 列表、验证和切换。
3. 增加 `/sessions`、`/switch`、`/new`、`/fork`。
4. 重构 `FullScreenTerminalUI` 的当前 Session 投影和事件过滤。
5. 将模型/Profile 变更追加到 Session 事件。
6. 重新设计 capability fingerprint，拆分静态能力和动态 Profile。

验收：

- running 时切换模型只对后续 Turn 生效。
- 切换 Session 后消息、工具块和状态不串线。
- 重启后每个 Session 仍能恢复最近一次 Profile。
- 不能通过切换模型扩大工具或 workspace 范围。

### Phase 2：动态权限和 Interactive Approval

目标：去掉“无审批全拒绝/`--approve-bash` 全放行”的二元行为。

任务：

1. 新增 `InteractiveApprovalService`。
2. TUI 增加审批卡片、允许一次和拒绝。
3. 引入 `CommandRiskEngine`，普通安全命令自动执行，危险命令询问。
4. `ApprovalPolicy` 支持动态审批原因，但仍由 Runtime 统一调用。
5. 对批准结果记录 `approval/requested` 和 `approval/resolved`。
6. `--approve-bash` 改为兼容/调试选项，显示弃用提示，不作为普通工作流。

验收：

- 普通 `pwd`/`ls` 不弹审批。
- `rm -rf build`、`git reset --hard`、权限修改会确认。
- 没有 TTY 或没有明确批准时，高风险操作 fail closed。
- 取消 Agent 时待审批请求不会悬挂。

### Phase 3：Sandbox Profile 和网络控制

目标：让权限和网络 Profile 真正改变下一次沙箱执行。

任务：

1. 引入 `SandboxSpec`。
2. 增加当前环境只读挂载。
3. 增加 project `.venv` PATH/VIRTUAL_ENV 处理。
4. `network=disabled` 无法隔离时直接阻塞。
5. `network=setup-approved` 暂时只给 setup runner。
6. Bash 结果增加 sandbox mode、network mode、returncode 和 error code。

验收：

- Agent phase 默认不能联网。
- 当前 Conda 环境只能以只读方式挂载。
- workspace `.venv` 可以写入依赖。
- 系统目录不能因为 `workspace-write` 变成可写。
- bwrap 不可用时不会静默执行宿主 Bash。

### Phase 4：自动环境检测、依赖 setup 和快速暂停

目标：用户无需填写模块名或手工准备 `.venv`。

任务：

1. 实现 Conda/venv detector。
2. 实现 `pyproject.toml` 和 requirements 解析器。
3. 实现 project `.venv` 检查和创建。
4. 实现独立 `EnvironmentSetupRunner`。
5. 接入 setup 专用审批和临时网络 Scope。
6. 增加 EnvironmentSnapshot 和 dependency fingerprint。
7. 增加 `environment_blocked`、结构化 ToolResult 和 Agent paused 语义。
8. 增加重复失败熔断。

验收：

- 依赖完整时启动不打扰用户。
- 缺依赖时不让模型反复尝试 Bash。
- setup 网络审批只影响 setup，不影响后续 Agent 网络。
- 安装失败后任务暂停并给出可执行提示。
- `/setup` 成功后 `/continue` 可以继续任务。

### Phase 5：环境缓存和跨进程恢复

目标：减少重复安装并增强恢复能力。

任务：

1. `.venv` 状态缓存和依赖 fingerprint。
2. 环境快照校验和损坏检测。
3. setup 日志 retention/spill。
4. Session 重启后恢复 blocked/setup 状态。
5. 子 Agent 环境 Profile 继承和隔离。

### Phase 6：Disposable Development Environment

目标：处理 apt、系统库、Node/浏览器等不能安全写入 host 的场景。

任务属于后续平台工程：

- Docker/Podman/bubblewrap rootfs。
- disposable rootfs 生命周期。
- overlay filesystem。
- host helper 和明确的 elevated approval。
- 网络 proxy/broker。
- 多平台后端。

第一版不实现 host admin，也不把 `sudo apt install` 作为普通 Agent 能力。

---

## 19. 测试与验收矩阵

### 19.1 控制面

| 场景 | 预期 |
| --- | --- |
| idle 时切换模型 | 下一 Turn 使用新模型 |
| running 时切换模型 | 当前请求不变，变更排队 |
| running 时 `/switch` | 默认提示等待或取消 |
| Session A 切换到 B | UI 和事件按 Session 隔离 |
| 切换不存在 Session | 明确错误，不改变当前指针 |
| `/new` 后退出 | 新 Header 已持久化 |
| `/fork` | 新 Session 独立追加，来源字段正确 |

### 19.2 权限和审批

| 场景 | 预期 |
| --- | --- |
| read-only 执行 write_file | Pre 阶段拒绝 |
| workspace-write 执行普通 ls | 不需要高风险审批 |
| 删除 workspace 文件 | 根据风险策略确认 |
| 修改系统目录 | 沙箱技术上拒绝 |
| 无审批服务运行高风险操作 | fail closed |
| 审批 UI 等待时取消 Agent | Future、Driver、子进程全部收敛 |
| 审批异常 | 视为拒绝，并写结构化事件 |

### 19.3 网络和沙箱

| 场景 | 预期 |
| --- | --- |
| network disabled + bwrap 支持 | 使用 `--unshare-net` |
| network disabled + bwrap 不支持网络 namespace | `environment_blocked` |
| setup-approved | 只对 setup runner 生效 |
| setup 结束 | Agent 新工具回到 disabled |
| 当前 Conda 挂载 | `/opt/python-env` 只读 |
| 整个 miniconda 根目录 | 不挂载 |
| workspace `.venv` | 位于 workspace，可按 Profile 写入 |
| 外部 symlink | 不能越过 workspace/环境边界 |

### 19.4 环境和依赖

| 场景 | 预期 |
| --- | --- |
| 依赖完整 | 不执行安装 |
| `.venv` 不存在 | setup 阶段创建 |
| 依赖缺失、网络未批准 | 立即暂停 |
| 用户批准联网 | 安装成功后关闭网络 |
| pip 返回网络错误 | 结构化 `network_blocked` 或 install failure |
| `pyproject.toml` 动态配置 | 不能执行任意代码，明确提示 |
| 包名/import 名不一致 | 使用 metadata/mapping，不简单字符串猜测 |
| 重复失败 2 次 | 熔断并暂停 |

### 19.5 恢复和回放

| 场景 | 预期 |
| --- | --- |
| 重启后恢复最近 Profile | 不静默换模型/权限 |
| 新控制事件 | 不破坏旧 projection |
| environment blocked 事件 | Session 可加载，任务保持 paused |
| setup 中进程崩溃 | 不自动重复未知副作用；按状态提示恢复 |
| Session fork | 不复制控制器当前内存状态，只复制事件快照 |
| UI 回放 | 从事件重建，不依赖旧 UI 缓存 |

---

## 20. 安全底线和非目标

### 20.1 必须保持的底线

1. 不因为“用户想像 Codex 一样方便”就默认开放宿主网络。
2. 不因为依赖安装失败就自动退出 Bubblewrap。
3. 不把当前 Conda 环境以可写方式挂载。
4. 不把整个 `~/miniconda3`、用户 home 或 `/` 暴露给模型。
5. 不把 `.env`、API Key 和完整环境变量写入 Session。
6. 不用风险分类器替代 Linux 内核隔离。
7. 不允许 `--approve-bash` 成为普通用户唯一的安全入口。
8. 不让 UI 直接改变正在运行的 subprocess 权限。
9. 不让任意未知依赖文件执行任意安装脚本而没有 setup 沙箱和审批。
10. 不在第一版实现无审计的 host-admin/elevated runner。

### 20.2 第一版明确不做

- 自动 `sudo apt install` 到宿主机。
- 没有代理支持的域名 allowlist。
- 让 Agent 运行在整个宿主根文件系统上。
- 任意动态安装插件并立即扩展静态工具集合。
- 跨进程自动重建完整子 Agent 树。
- 浏览器、LSP、PTY 和远程执行。

---

## 21. 推荐的最小可行版本

如果要控制一次改造的风险，建议第一轮只交付下面这条闭环：

```text
python-agent
    ↓
RuntimeController
    ↓
/status /model /permission /network /sessions /switch
    ↓
AgentManager 多 Agent 所有权
    ↓
TurnExecutionSnapshot
    ↓
InteractiveApprovalService
    ↓
SandboxSpec(network=disabled/workspace-write)
```

第一轮先不自动安装任意项目依赖，只实现：

- 环境检测。
- 依赖缺失报告。
- 环境阻塞和暂停。
- `/setup` 入口的接口预留。

第二轮再加入：

- `workspace/.venv` 创建。
- setup 专用安装器。
- setup 临时网络批准。
- 环境快照缓存。

这样可以先验证“运行时控制面”本身，不会把模型切换、Session 切换、审批 UI、Bubblewrap 挂载和 pip 安装同时塞进一个不可回滚的大改动。

---

## 22. 最终目标状态

最终用户只需要知道几条稳定命令：

```text
python-agent
/model ...
/permission ...
/network ...
/switch ...
/setup
/continue
```

用户不需要知道：

- `CONDA_PREFIX` 如何挂载。
- venv 的 PATH 如何拼接。
- Bubblewrap 参数是什么。
- 依赖模块和发行包名称是否相同。
- 网络 namespace 是否可用。
- 哪个工具需要怎样构造审批请求。

系统内部则保持清晰的责任链：

```text
用户控制命令
    ↓
Runtime Control Plane
    ↓
Profile / Session / Environment 状态
    ↓
Permission Engine + Approval
    ↓
Sandbox Runner
    ↓
AgentLoop / ToolRuntime
    ↓
Session 事件日志
```

这比继续增加 `--runtime-environment`、`--runtime-module` 或 `--approve-bash` 更符合项目目标：用户操作的是意图和 Profile，系统负责把它们转换成可审计、可恢复、受 Linux 内核约束的执行环境。

