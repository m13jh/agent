"""定义 python-agent 对外暴露的领域异常类型。"""


class AgentError(Exception):
    """所有可预期 Harness 失败的基类，便于调用方统一捕获。"""


class ConfigurationError(AgentError):
    """配置内容无效，或者配置文件无法读取。"""


class SessionError(AgentError):
    """Session 事件日志违反格式或顺序约束。"""


class SessionNotFoundError(SessionError):
    """请求加载的 Session 在持久化存储中不存在。"""


class SessionConflictError(SessionError):
    """创建 Session 时目标 ID 已存在，拒绝覆盖已有事件历史。"""


class SessionFormatError(SessionError):
    """Header、JSONL 或事件版本不兼容，无法进行确定性回放。"""


class SessionRepairRequired(SessionFormatError):
    """日志尾部可安全修复，但调用方尚未显式授权修复操作。"""


class ProjectionError(SessionError):
    """事件无法安全投影为模型可见消息。"""


class ModelError(AgentError):
    """模型适配器调用失败，或者返回内容没有通过标准化校验。"""


class ToolError(AgentError):
    """工具调用无法完成，包括工具自身失败和运行策略拒绝。"""


class ToolNotFoundError(ToolError):
    """模型请求的工具没有出现在当前 ToolRegistry 中。"""


class ToolValidationError(ToolError):
    """工具参数不符合该工具声明的 JSON Schema 子集。"""


class AgentLimitError(AgentError):
    """Agent 达到了配置的步骤、Token 或其他执行上限。"""


class SubagentError(AgentError):
    """子 Agent 创建、控制或生命周期收敛失败。"""


class SubagentPermissionError(SubagentError):
    """调用方不是目标子 Agent 的直接父级，或请求了未授权能力。"""


class SubagentLimitError(SubagentError):
    """子 Agent 超过最大深度、数量、步数或其他委派限制。"""


class SkillError(AgentError):
    """Skill 清单、路径、内容或工具授权无效。"""
