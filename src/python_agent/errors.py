"""定义 python-agent 对外暴露的领域异常类型。"""


class AgentError(Exception):
    """所有可预期 Harness 失败的基类，便于调用方统一捕获。"""


class ConfigurationError(AgentError):
    """配置内容无效，或者配置文件无法读取。"""


class SessionError(AgentError):
    """Session 事件日志违反格式或顺序约束。"""


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
