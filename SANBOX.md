# Coding Agent 沙箱与权限系统技术设计

## 1. 目标

实现一个类似 Codex 的 Coding Agent 权限与沙箱系统，满足以下要求：

1. 模型不能直接操作宿主机。
2. 所有 Shell 命令和工具调用统一经过权限检查。
3. 默认采用最小权限原则。
4. 工作目录可写不代表 Agent 可以任意删除已有文件。
5. 网络权限、文件权限、系统权限相互独立。
6. 系统级依赖优先安装到 Docker 临时容器，不修改宿主机。
7. 高风险操作必须经过用户审批。
8. 权限限制由操作系统和 Tool Gateway 强制执行，不能依赖模型自行遵守。

---

# 2. 总体架构

```text
                         User
                           │
                           ▼
                          LLM
                           │
                           ▼
                    Tool Dispatcher
                           │
                           ▼
                     Policy Gateway
                           │
           ┌───────────────┼────────────────┐
           │               │                │
           ▼               ▼                ▼
    Capability Check   Risk Engine    Approval Manager
           │               │                │
           └───────────────┼────────────────┘
                           │
                           ▼
                     Tool Executor
                           │
        ┌──────────────────┼───────────────────┐
        │                  │                   │
        ▼                  ▼                   ▼
     Shell Tool         File Tool          External Tool
        │                  │                   │
        ▼                  ▼                   ▼
   Sandbox Runner    Safe File Backend    API / Browser /
        │                                  Git / DB ...
   ┌────┴─────┐
   │          │
   ▼          ▼
Bubblewrap   Docker
L0/L1/L2     L3

                      Host Executor
                           │
                           ▼
                          L4
                   必须显式用户审批
```

---

# 3. 核心安全原则

## 3.1 模型不能直接执行 Tool

禁止：

```text
LLM
 ↓
write_file()
```

必须：

```text
LLM
 ↓
Tool Request
 ↓
Policy Gateway
 ↓
Permission Check
 ↓
Risk Check
 ↓
Approval
 ↓
Tool Executor
```

所有工具必须统一经过 Policy Gateway。

---

## 3.2 Sandbox 和 Approval 分离

Sandbox 解决：

```text
技术上能不能做
```

Approval 解决：

```text
当前任务允不允许做
```

例如：

```text
/workspace/src/main.py
```

在 L1 中技术上可写。

但：

```text
删除 src/main.py
```

仍然必须经过删除策略判断。

---

# 4. 权限等级

## L0：READ_ONLY

用途：

* 代码分析
* 搜索
* 查看文件
* 制定修改方案

权限：

```text
Workspace: READ ONLY
System:    READ ONLY
Network:   默认关闭
Root:      禁止
```

允许：

```text
read_file
list_files
search_files
cat
grep
find
git status
git diff
git log
```

禁止：

```text
write_file
patch_file
delete_file
move_file
mkdir
touch
写 Shell 重定向
```

---

# 5. L1：WORKSPACE_WRITE

这是正常 Coding Agent 的默认执行权限, 工作区可读可写。

权限：

```text
Workspace: READ / WRITE
System:    READ ONLY
Network:   OFF
Root:      NO
```

允许：

```text
修改源码
创建文件
运行 formatter
运行 pytest
运行 Python
编译项目
写测试
生成代码
删除允许自动删除的临时文件
```

禁止：

```text
写 /etc
写 /usr
写 /var
访问宿主敏感目录
联网下载
sudo
apt install
```

## 5.1 Shell 中的运行时工具链可见性

L1 的“系统只读”不等于把宿主整个 `/`、`/home` 或当前 Shell 的环境变量复制给 Agent。
Bubblewrap 只提供一个隔离的标准目录骨架，并挂载 workspace 和受信任 runtime；因此原先位于宿主 Home 下的
Conda/NVM 工具会显示为 `command not found`。

当前实现会在 Agent 启动时由受信任的宿主侧代码发现并只读挂载：

```text
当前 Python/Conda 环境（例如 miniconda3 和 envs/agent）
NVM 当前 Node 版本目录
/opt 下被 PATH 发现的具体工具目录
/usr 下的系统工具；Java 的 alternatives 目录按需挂载
```

只加入这些 runtime 的 `bin` 到沙箱 PATH，不传递宿主全部环境变量。Conda 初始化脚本通过
`BASH_ENV` 只读加载，所以 `conda --version` 和 `conda activate agent` 可以使用；Conda、
pip、npm、Cargo、Go 的缓存/全局前缀被重定向到沙箱临时目录或 workspace，避免写入宿主
Home。宿主 `/home`、`/root` 和 `/` 本身仍不会作为真实目录挂载；`ls /` 看到的顶层目录是
隔离骨架，不代表可以读取宿主内容。

workspace 初次不存在时，CLI 会先创建它；任务 manifest 不会把 workspace 根登记为 Agent
生成目录。这样根目录下的 `CMakeLists.txt` 与子目录文件具有相同的写入语义。

---

# 6. L2：WORKSPACE_NETWORK

用于需要网络的项目级操作。

权限：

```text
Workspace: READ / WRITE
System:    READ ONLY
Network:   ON
Root:      NO
```

主要用途：

```text
pip install
npm install
cargo fetch
go mod download
下载项目依赖
```

推荐：

```bash
python -m venv .venv
.venv/bin/pip install ...
```

项目依赖应写入：

```text
/workspace/.venv
/workspace/node_modules
```

不得写入宿主系统目录。


---

# 7. L3：CONTAINER_ADMIN

用于系统级依赖和高隔离运行环境。

执行后端：

```text
Docker
```

用途：

```text
apt install
apt remove
安装 gcc
安装 ffmpeg
安装 chromium
复杂系统级构建
运行不可信程序
```

容器结构：

```text
Container
/
├── etc          容器自己的
├── usr          容器自己的
├── var          容器自己的
├── tmp
└── workspace
      │
      └── 挂载用户项目
```

允许：

```text
容器内 root
容器内 apt
容器内修改 /etc
容器内修改 /usr
```

禁止：

```text
修改宿主 /etc
修改宿主 /usr
访问宿主 Docker socket
使用 --privileged
挂载宿主 /
```

---

# 8. Docker 管理要求

Docker 必须由可信的：

```text
ContainerManager
```

管理。

禁止将：

```text
/var/run/docker.sock
```

暴露给 LLM 或 Sandbox。

LLM 只能请求：

```text
在 L3 环境执行某个命令
```

LLM 不允许控制：

```text
image
volume
privileged
network mode
PID namespace
capabilities
Docker socket
host mount
```

这些参数必须由 ContainerManager 固定生成。

---

# 9. Docker 默认限制

推荐：

```bash
docker run --rm \
  --memory=2g \
  --cpus=2 \
  --pids-limit=256 \
  --security-opt=no-new-privileges \
  -v "$PROJECT:/workspace" \
  -w /workspace \
  agent-runtime:latest
```

必须禁止：

```text
--privileged
--pid=host
--network=host
-v /:/host
-v /var/run/docker.sock:/var/run/docker.sock
```

---

# 10. L4：HOST_ADMIN

L4 用于真正修改宿主 Linux。

包括：

```text
sudo
宿主 apt install
宿主 apt remove
systemctl
mount
修改 /etc
修改宿主服务
操作宿主 Docker
修改防火墙
```

所有 L4 操作：

```text
必须显式用户审批
```

不得自动升级至 L4。

---

# 11. Capability 模型

权限不要直接绑定 Tool 名称，应使用 Capability。

推荐定义：

```text
filesystem.workspace.read
filesystem.workspace.write

network.internet

process.execute

container.execute
container.admin

host.admin

external.read
external.write
external.delete
```

权限等级映射：

```text
L0
├── filesystem.workspace.read
└── process.readonly

L1
├── filesystem.workspace.read
├── filesystem.workspace.write
└── process.execute

L2
├── L1 全部权限
└── network.internet

L3
├── L2 全部权限
└── container.admin

L4
└── host.admin
```

---

# 12. Tool 权限定义

每个 Tool 必须显式声明所需 Capability。

例如：

```text
read_file
→ filesystem.workspace.read

write_file
→ filesystem.workspace.write

patch_file
→ filesystem.workspace.write

delete_file
→ filesystem.workspace.write
→ delete risk check

web_search
→ network.internet

docker_exec
→ container.execute

host_command
→ host.admin
```

---

# 13. File Tool 安全规则

所有文件 Tool 必须：

1. 将输入路径解析为真实绝对路径。
2. 校验路径是否位于允许范围。
3. 防止 `../` 逃逸。
4. 防止 symlink 逃逸。
5. Workspace 外写操作直接拒绝。

例如：

```text
/workspace/src/a.py
→ ALLOW

../../etc/passwd
→ DENY

/workspace/link/passwd
link -> /etc
→ DENY
```

不能只检查字符串前缀。

---

# 14. 删除策略

删除操作必须作为独立高风险能力处理。

核心规则：

> 如果是 Agent 为当前任务生成的临时文件、缓存或测试产物，删除属于任务正常步骤，可以直接清理，并记录删除内容。

> 如果是用户已有的代码、文档、数据或未提交改动，即使位于工作目录，也不得自行删除，必须先征求用户同意。

> 如果删除目标范围不明确，或者涉及批量、递归删除，必须先确认。

> 如果删除需要宿主 root 权限，还必须额外经过 L4 提权审批。

> 工作目录可写只代表技术上允许修改，不代表 Agent 可以任意删除其中已有文件。

---

# 15. 删除风险等级

## D0：AUTO_DELETE

允许自动删除。

条件：

* Agent 当前任务自己创建。
* 明确属于临时文件。
* 明确属于缓存。
* 明确属于测试产物。
* 明确属于可重新生成的构建产物。

例如：

```text
/tmp/agent_xxx
__pycache__/
.pytest_cache/
*.tmp
*.log
build/
dist/
coverage/
Agent 当前任务生成的临时测试文件
```

执行后必须记录：

```text
Deleted:
- .pytest_cache/
- build/
- tmp/test_output.json
```

---

## D1：USER_EXISTING_FILE

包括：

```text
用户已有源码
已有文档
已有配置
已有数据
已有测试
已有脚本
```

例如：

```text
src/main.py
README.md
config.yaml
data/train.json
tests/test_api.py
```

即使当前是：

```text
L1 WORKSPACE_WRITE
```

也不能自动删除。

处理：

```text
必须 Approval
```

---

# 16. 未提交改动保护

删除前必须检查文件状态。

如果 Workspace 是 Git 项目：

```text
git status --porcelain
```

以下情况必须视为用户已有未提交数据：

```text
M file.py
A file.py
?? file.py
```

即：

```text
Modified
Added
Untracked
```

除非能够确认该文件是 Agent 当前任务刚创建的，否则：

```text
删除必须 Approval
```

特别注意：

```text
untracked != 临时文件
```

用户新建但尚未提交的文件可能非常重要。

---

# 17. Agent 生成文件追踪

为了判断文件是否为 Agent 自己创建，需要维护 Task File Manifest。

例如：

```json
{
  "task_id": "task_123",
  "created_files": [
    "/workspace/tmp/output.json",
    "/workspace/tests/generated_test.py"
  ],
  "generated_dirs": [
    "/workspace/build/"
  ]
}
```

只有满足：

```text
path ∈ 当前 task generated files
```

才能自动认为属于 Agent 自己生成。

不能仅依据：

```text
untracked
文件名包含 tmp
位于 workspace
```

判断文件可删除。

---

# 18. 批量与递归删除

以下操作必须视为高风险：

```text
rm -rf
delete_directory
delete_files([...大量文件])
find ... -delete
批量 glob 删除
递归清空目录
```

如果：

```text
所有目标均属于当前任务生成的临时产物
```

可以自动执行。

否则：

```text
必须 Approval
```

如果删除目标无法在执行前完整确定：

```text
必须 Approval
```

例如：

```bash
rm -rf *
find . -type f -delete
```

默认不得自动执行。

---

# 19. 严格禁止删除范围

以下目标默认直接拒绝，不进入普通 Approval：

```text
.git/
整个 workspace
workspace 根目录
宿主 /etc
宿主 /usr
宿主 /var
~/.ssh
用户 Home 根目录
系统根目录 /
```

例如：

```bash
rm -rf .
rm -rf /*
rm -rf .git
```

处理：

```text
DENY
```

如确实存在合法需求，应进入专门的 Critical Destructive Approval 流程，不属于普通 L1 删除操作。

---

# 20. 删除目录规则

必须区分：

```text
delete_file
delete_directory
```

`delete_directory` 风险默认高于 `delete_file`。

目录删除前必须：

1. 枚举目标文件。
2. 判断文件数量。
3. 检查是否包含用户已有文件。
4. 检查是否包含未提交改动。
5. 检查是否包含敏感路径。
6. 计算整体 Risk Level。

如果目录中只要存在一个：

```text
用户已有文件
```

整个递归删除：

```text
必须 Approval
```

---

# 21. 删除决策流程

```text
Delete Request
      │
      ▼
Resolve Real Path
      │
      ▼
Inside Workspace?
   │          │
  NO         YES
   │          │
 DENY         ▼
        Sensitive Path?
          │       │
         YES      NO
          │       │
        DENY      ▼
             Task Generated?
               │       │
              YES      NO
               │       │
               ▼       ▼
          Temporary?   Existing User File
            │    │             │
           YES   NO            ▼
            │    │          Approval
            ▼    ▼
         AUTO  Approval
```

如果为批量或递归操作：

```text
再增加 Batch Risk Check
```

---

# 22. Shell 删除命令

即使通过 Shell：

```bash
rm file
```

也不能绕过删除策略。

Shell Tool 在执行命令前必须经过：

```text
Command Risk Analyzer
```

识别：

```text
rm
unlink
rmdir
find -delete
git clean
git reset --hard
Python shutil.rmtree
Python os.remove
```

Shell 分析器只作为审批策略层。

真正的 Workspace 边界仍然由 Bubblewrap 强制。

---

# 23. Shell 和 File Tool 必须使用同一删除策略

不能出现：

```text
delete_file("src/main.py")
→ Approval

但是

shell("rm src/main.py")
→ 自动执行
```

所有删除路径必须统一进入：

```text
DeletePolicyEngine
```

架构：

```text
FileTool ─────┐
              │
ShellTool ────┼──→ DeletePolicyEngine
              │
GitTool ──────┘
```

---

# 24. Git 高风险操作

以下操作必须进入高风险审批：

```text
git reset --hard
git clean -f
git clean -fd
git clean -fdx
git checkout -- .
git restore .
删除 branch
强制覆盖工作区
```

普通：

```text
git status
git diff
git log
```

只读执行。

---

# 25. 删除恢复机制

建议优先采用 Soft Delete。

例如：

```text
/workspace/.agent-trash/
└── task_123/
    └── src/main.py
```

删除实际执行：

```text
move file → .agent-trash
```

而不是直接：

```text
unlink
```

但以下临时内容可以直接永久删除：

```text
Agent 当前任务生成的 cache
build
tmp
测试输出
```

---

# 26. 工具审批策略

## 无需审批

```text
读取文件
搜索代码
修改已有源码内容
创建新源码
运行测试
运行 formatter
删除当前任务生成的临时文件
删除缓存
删除测试产物
```

---

## 需要审批

```text
删除用户已有代码
删除用户已有文档
删除用户已有数据
删除未提交改动
递归删除目录
批量删除未知目标
git reset --hard
git clean
大量覆盖源码
外部有副作用 Tool
```

---

## 直接拒绝

```text
L0 写文件
Workspace 外普通写操作
删除敏感系统路径
删除 .git
删除 Workspace 根目录
绕过 Permission Gateway
直接访问 Docker socket
```

---

# 27. Root 删除操作

如果删除目标需要：

```text
sudo
root
host.admin
```

则删除策略和 Host Admin 策略叠加。

必须同时满足：

```text
Delete Approval
+
L4 Host Admin Approval
```

例如：

```bash
sudo rm /etc/example.conf
```

流程：

```text
Delete Risk
   ↓
HIGH
   ↓
需要 Host Admin
   ↓
L4 Approval
   ↓
用户显式确认
```

不得因为已经批准“删除文件”就自动获得 root。

---



# 28. 网络模式与交互审批的 CLI 语义

权限等级只描述可授予的最大 Capability，网络仍是独立的运行时开关。CLI 默认使用：

```text
--network-mode disabled
```

因此 L2/L3/L4 即使拥有 `network.internet` Capability，未显式打开网络时仍然不能联网。
需要联网时必须同时传：

```bash
--network-mode full --approve-network
```

L3 还需要 `--enable-container`；容器使用 `agent-runtime:latest` 的自身工具链，不继承宿主
Conda/NVM。镜像没有安装 `curl` 时，网络可能已经可用但命令仍会返回 `command not found`；
可以使用镜像已有的 Python，或在同一次临时容器命令中安装依赖。

交互式 `chat` 不传 `--approve-bash` 时，Bash、网络 Scope、已有文件删除、批量 patch 和
容器调用会在终端显示审批摘要：

```text
y / yes  → 批准当前请求
n / no   → 拒绝当前请求
/exit    → 拒绝未完成请求并退出
```

`--approve-bash` 是非交互自动批准开关，适合 CI 或明确的 disposable workspace；因此传入
它时不会再弹出人工审批。一次性 `run` 没有输入循环，未提供审批服务时保持拒绝。

原则：

```text
无法证明安全删除
=
需要 Approval
```

不能：

```text
无法判断
=
自动允许
```

---

# 31. 全局 Tool 执行流程

```text
LLM Tool Request
       │
       ▼
Tool Dispatcher
       │
       ▼
Capability Check
       │
       ├── 不满足
       │      ↓
       │   PermissionDenied
       │
       ▼
Risk Engine
       │
       ├── SAFE
       │      ↓
       │    Execute
       │
       ├── APPROVAL
       │      ↓
       │   User Approval
       │
       └── DENY
              ↓
            Reject
```

---

# 32. 最终权限矩阵

| 操作                      | L0 |       L1 |       L2 |          L3 |       L4 |
| ----------------------- | -: | -------: | -------: | ----------: | -------: |
| 读 Workspace             |  ✅ |        ✅ |        ✅ |           ✅ |        ✅ |
| 修改 Workspace            |  ❌ |        ✅ |        ✅ |           ✅ |        ✅ |
| 创建文件                    |  ❌ |        ✅ |        ✅ |           ✅ |        ✅ |
| 删除任务临时文件                |  ❌ |        ✅ |        ✅ |           ✅ |        ✅ |
| 删除用户已有文件                |  ❌ | Approval | Approval |    Approval | Approval |
| 删除未提交改动                 |  ❌ | Approval | Approval |    Approval | Approval |
| 批量递归删除                  |  ❌ | Approval | Approval |    Approval | Approval |
| 删除 `.git`               |  ❌ |        ❌ |        ❌ | ❌ Workspace | Critical |
| 网络                      |  ❌ |        ❌ |        ✅ |           ✅ |        ✅ |
| `pip install` 到 `.venv` |  ❌ |        ❌ |        ✅ |           ✅ |        ✅ |
| 写宿主 `/etc`              |  ❌ |        ❌ |        ❌ |           ❌ | Approval |
| 容器内 `/etc`              |  ❌ |        ❌ |        ❌ |           ✅ |        ✅ |
| 容器内 `apt install`       |  ❌ |        ❌ |        ❌ |           ✅ |        ✅ |
| 宿主 `apt install`        |  ❌ |        ❌ |        ❌ |           ❌ | Approval |
| `sudo`                  |  ❌ |        ❌ |        ❌ |       容器内允许 | Approval |

---
