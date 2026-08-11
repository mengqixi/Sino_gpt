from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


API_CONFIG_COLUMNS = (
    "config_name",
    "api_type",
    "api_base_url",
    "api_key",
    "model_name",
    "endpoint_path",
    "method",
    "request_content_type",
    "auth_type",
    "auth_header_name",
    "auth_header_prefix",
    "image_field_name",
    "prompt_field_name",
    "model_field_name",
    "count_field_name",
    "size_field_name",
    "quality_field_name",
    "extra_params_json",
    "response_image_type",
    "response_image_path",
    "response_text_path",
    "timeout_seconds",
    "enabled",
    "is_default",
)


def api_seed_from_database(database: Path) -> bytes:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"SELECT {', '.join(API_CONFIG_COLUMNS)} FROM api_configs "
            "WHERE enabled = 1 AND length(trim(coalesce(api_key, ''))) > 0 ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    configs = [{column: row[column] for column in API_CONFIG_COLUMNS} for row in rows]
    if not configs:
        raise ValueError("数据库中没有启用且已配置密钥的 API")
    return json.dumps(
        {"version": 1, "api_configs": configs},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def seed_entry(platform: str) -> str:
    if platform == "windows":
        return "SinoImageTool/_internal/desktop/private/api-configs.json"
    if platform == "macos":
        return "SinoImageTool.app/Contents/Resources/desktop/private/api-configs.json"
    raise ValueError(f"不支持的平台：{platform}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Inject private API settings into a local desktop ZIP")
    parser.add_argument("--platform", choices=("windows", "macos"), required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    database = args.database.expanduser().resolve()
    if not source.is_file() or not database.is_file():
        raise SystemExit("源压缩包或 API 数据库不存在")
    if output.exists():
        raise SystemExit(f"输出文件已经存在：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    seed = api_seed_from_database(database)
    entry = seed_entry(args.platform)
    with ZipFile(source, "r") as archive:
        if entry in archive.namelist():
            raise SystemExit("源压缩包已经包含 API 配置")
    shutil.copy2(source, output)
    info = ZipInfo(entry)
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    with ZipFile(output, "a") as archive:
        archive.writestr(info, seed)
    payload = json.loads(seed.decode("utf-8"))
    print(f"Private package created: {output} ({len(payload['api_configs'])} API configs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
