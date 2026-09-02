"""把工具结果转换为有界且确定性的 JSON-safe 值。"""

from __future__ import annotations

import base64
import json
import math
from datetime import date, datetime, time
from pathlib import Path
from typing import Any


class JsonSerializationError(ValueError):
    """工具结果无法安全表示为 Session 事件。"""


def to_json_safe(
    value: Any,
    *,
    max_depth: int = 20,
    max_nodes: int = 10_000,
) -> Any:
    """返回一个可被严格 JSON 编码器接受的确定性值。

    工具实现属于应用代码，可能意外返回 ``Path``、bytes、set、非有限浮点数或任意对象。
    在 Runtime 边界归一化可以避免这些值进入只追加 Session 的校验器。这里有意不截断字符串；
    模型可见内容的保留上限由 ``OutputPolicy`` 负责。
    """

    state = {"nodes": 0}

    def convert(current: Any, depth: int) -> Any:
        state["nodes"] += 1
        if state["nodes"] > max_nodes:
            raise JsonSerializationError(f"tool result exceeds {max_nodes} JSON values")
        if depth > max_depth:
            raise JsonSerializationError(f"tool result exceeds JSON depth {max_depth}")

        if current is None or isinstance(current, (str, bool, int)):
            return current
        if isinstance(current, float):
            if not math.isfinite(current):
                raise JsonSerializationError("tool result contains a non-finite float")
            return current
        if isinstance(current, Path):
            return str(current)
        if isinstance(current, (datetime, date, time)):
            return current.isoformat()
        if isinstance(current, bytes):
            return {
                "type": "bytes",
                "base64": base64.b64encode(current).decode("ascii"),
                "size": len(current),
            }
        if isinstance(current, dict):
            converted: dict[str, Any] = {}
            for key, item in current.items():
                if not isinstance(key, str):
                    raise JsonSerializationError(
                        f"tool result object key must be text, got {type(key).__name__}"
                    )
                converted[key] = convert(item, depth + 1)
            return converted
        if isinstance(current, (list, tuple)):
            return [convert(item, depth + 1) for item in current]
        if isinstance(current, (set, frozenset)):
            # set 没有固定顺序。按安全表示排序可以让落盘结果保持确定性，同时不调用任意
            # 对象的 repr/str 方法。
            converted_items = [convert(item, depth + 1) for item in current]
            return sorted(
                converted_items,
                key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
            )
        raise JsonSerializationError(
            f"tool result contains unsupported value type {type(current).__name__}"
        )

    result = convert(value, 0)
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 转换后保留的防御性检查
        raise JsonSerializationError(f"tool result is not JSON serializable: {exc}") from exc
    return result


__all__ = ["JsonSerializationError", "to_json_safe"]
