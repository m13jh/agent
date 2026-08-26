"""定义带稳定排序信息的静态提示词段落。"""

from pydantic import BaseModel, ConfigDict, Field


class PromptSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    order: int
    content: str
