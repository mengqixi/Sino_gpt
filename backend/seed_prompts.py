import json

from .config import PROMPT_DIR
from .database import db_session, now_iso


PROMPT_SEEDS = [
    {
        "file": "01_color_change.txt",
        "name": "\u6307\u5b9a\u90e8\u4f4d\u6362\u8272-\u7ed3\u6784\u9501\u5b9a\u7248",
        "task_type": "color_change",
        "variables": ["target_color", "output_count", "extra_requirements"],
    },
    {
        "file": "02_material_replace.txt",
        "name": "\u6750\u8d28\u66ff\u6362-\u7ed3\u6784\u9501\u5b9a\u7248",
        "task_type": "material_replace",
        "variables": ["target_material", "output_count", "extra_requirements"],
    },
    {
        "file": "04_model_showcase.txt",
        "name": "\u6a21\u7279\u5c55\u793a\u56fe-\u7535\u5546\u5c55\u793a\u7248",
        "task_type": "model_showcase",
        "variables": [
            "model_showcase_requirement",
            "wearing_method",
            "scene",
            "outfit",
            "bag_length_cm",
            "bag_width_cm",
            "bag_height_cm",
            "output_count",
            "extra_requirements",
        ],
    },
    {
        "file": "05_custom_generate.txt",
        "name": "自定义生成-可编辑版",
        "task_type": "custom_generate",
        "variables": ["extra_requirements", "output_count"],
    },
]


def _read_prompt_file(file_name: str) -> str:
    return (PROMPT_DIR / file_name).read_text(encoding="utf-8-sig")


def seed_prompt_templates() -> None:
    with db_session() as conn:
        for item in PROMPT_SEEDS:
            path = PROMPT_DIR / item["file"]
            if not path.exists():
                continue
            content = _read_prompt_file(item["file"])
            existing = conn.execute(
                "SELECT id FROM prompt_templates WHERE is_system = 1 AND original_file_name = ?",
                (item["file"],),
            ).fetchone()
            ts = now_iso()
            if existing:
                row = conn.execute("SELECT template_content FROM prompt_templates WHERE id = ?", (existing["id"],)).fetchone()
                old_content = row["template_content"] or ""
                is_legacy_color = item["task_type"] == "color_change" and "请一次生成至少 4 张不同角度" in old_content
                is_legacy_material = item["task_type"] == "material_replace" and "材质近景细节图" in old_content
                is_legacy_model = item["task_type"] == "model_showcase" and "【包袋与服装协调配色规则】" not in old_content
                if is_legacy_color or is_legacy_material or is_legacy_model:
                    conn.execute(
                        "UPDATE prompt_templates SET template_content = ?, updated_at = ? WHERE id = ?",
                        (content, ts, existing["id"]),
                    )
                continue
            conn.execute(
                "UPDATE prompt_templates SET is_default = 0 WHERE task_type = ?",
                (item["task_type"],),
            )
            conn.execute(
                """
                INSERT INTO prompt_templates (
                    name, task_type, template_content, variables_json, is_default,
                    is_system, original_file_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?)
                """,
                (
                    item["name"],
                    item["task_type"],
                    content,
                    json.dumps(item["variables"], ensure_ascii=False),
                    item["file"],
                    ts,
                    ts,
                ),
            )


def restore_system_prompt(template_id: int) -> bool:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM prompt_templates WHERE id = ? AND is_system = 1",
            (template_id,),
        ).fetchone()
        if not row:
            return False
        original_file_name = row["original_file_name"]
        path = PROMPT_DIR / original_file_name
        if not path.exists():
            return False
        conn.execute(
            "UPDATE prompt_templates SET template_content = ?, updated_at = ? WHERE id = ?",
            (_read_prompt_file(original_file_name), now_iso(), template_id),
        )
        return True
