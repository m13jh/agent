"""确定性地组装系统提示词。

Prompt 模块只负责静态段落的排序和拼接，动态任务内容仍然通过 Session 的
``user/message`` 事件进入模型上下文。
"""

from python_agent.prompt.assembler import PromptAssembler
from python_agent.prompt.sections import PromptSection

__all__ = ["PromptAssembler", "PromptSection"]
