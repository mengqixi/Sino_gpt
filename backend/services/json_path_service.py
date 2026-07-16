from typing import Any


def json_path_get(data: Any, path: str) -> list[Any]:
    if not path:
        raise ValueError("返回字段路径为空")
    parts = path.split(".")
    current = [data]
    for part in parts:
        next_values: list[Any] = []
        for value in current:
            if part == "*":
                if isinstance(value, list):
                    next_values.extend(value)
                else:
                    raise ValueError(f"路径 {path} 中的 * 需要数组")
            elif isinstance(value, list):
                try:
                    next_values.append(value[int(part)])
                except (ValueError, IndexError):
                    raise ValueError(f"数组路径 {part} 无效")
            elif isinstance(value, dict):
                if part not in value:
                    raise ValueError(f"没有找到字段 {part}")
                next_values.append(value[part])
            else:
                raise ValueError(f"路径 {part} 无法继续读取")
        current = next_values
    return current
