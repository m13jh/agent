"""定义带稳定排序信息的静态提示词段落。

每个段落拥有唯一语义 ID、显式 order 和文本内容；组装时使用 order 加 ID 排序，
避免依赖字典插入顺序而导致不同运行产生不同的系统提示词。
"""

from pydantic import BaseModel, ConfigDict, Field


class PromptSection(BaseModel):
    """一个不可变的系统提示词段落定义。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    order: int
    content: str
