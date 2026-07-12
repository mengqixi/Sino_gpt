import json
import re
from typing import Any

import requests

from .file_service import (
    decode_base64_image,
    download_image,
    encode_image_as_data_url,
    save_result_bytes,
    suffix_from_url,
)
from .json_path_service import json_path_get


class RelayGatewayTimeoutError(ValueError):
    """The relay accepted the request but its gateway did not return a result."""


def _auth_headers(config: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    auth_type = (config.get("auth_type") or "bearer").lower()
    api_key = config.get("api_key") or ""
    header_name = config.get("auth_header_name") or "Authorization"
    prefix = config.get("auth_header_prefix") or "Bearer"
    if auth_type == "none":
        return headers
    if not api_key:
        raise ValueError("未配置 API Key，请先在 API 设置中保存密钥")
    if auth_type == "bearer":
        headers[header_name] = f"{prefix} {api_key}".strip()
    elif auth_type == "raw":
        headers[header_name] = api_key
    else:
        raise ValueError("不支持的认证方式")
    return headers


def _load_extra_params(config: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(config.get("extra_params_json") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("额外参数 JSON 格式错误") from exc
    if not isinstance(value, dict):
        raise ValueError("额外参数 JSON 必须是对象")
    return value


def _request_params(
    config: dict[str, Any],
    final_prompt: str,
    output_count: int | None,
    image_size: str,
    quality: str | None,
) -> dict[str, Any]:
    params = _load_extra_params(config)
    form_params = {
        config.get("prompt_field_name") or "prompt": final_prompt,
        config.get("model_field_name") or "model": config.get("model_name") or "",
        config.get("size_field_name") or "size": image_size,
    }
    if output_count and config.get("count_field_name"):
        form_params[config["count_field_name"]] = output_count
    if quality and config.get("quality_field_name"):
        form_params[config["quality_field_name"]] = quality
    params.update({k: v for k, v in form_params.items() if k and v is not None})
    return params


def _summarize_error_body(text: str) -> str:
    body = (text or "").strip()
    if not body:
        return "中转站没有返回错误详情"
    title_match = re.search(r"<title>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        title = re.sub(r"\s+", " ", title_match.group(1)).strip()
        return f"中转站返回 HTML 错误页：{title}"
    compact = re.sub(r"\s+", " ", body)
    return compact[:500]


def _raise_for_status(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        summary = _summarize_error_body(response.text)
        if response.status_code == 524:
            raise RelayGatewayTimeoutError(
                "中转站返回错误：HTTP 524，网关超时。请求已发到中转站，但中转站或上游生图服务长时间没有返回结果。"
                "本次结果无法确认且可能已经扣费；系统不会自动重试。请先检查中转站记录，再决定是否改用快速 API。"
            ) from exc
        raise ValueError(f"中转站返回错误：HTTP {response.status_code}。{summary}") from exc


def call_relay_image_api(
    config: dict[str, Any],
    image_path: str | list[str],
    final_prompt: str,
    output_count: int | None,
    image_size: str,
    quality: str | None,
) -> dict[str, Any]:
    base_url = (config.get("api_base_url") or "").strip()
    endpoint_path = (config.get("endpoint_path") or "").strip()
    if not base_url:
        raise ValueError("API Base URL 为空")
    if not endpoint_path:
        raise ValueError("接口路径为空")
    url = base_url.rstrip("/") + "/" + endpoint_path.lstrip("/")
    headers = _auth_headers(config)
    params = _request_params(config, final_prompt, output_count, image_size, quality)
    image_paths = image_path if isinstance(image_path, list) else [image_path]
    if not image_paths:
        raise ValueError("上传图片不存在")
    timeout = int(config.get("timeout_seconds") or 300)
    method = (config.get("method") or "POST").upper()
    if method != "POST":
        raise ValueError("第一版仅支持 POST 请求")
    content_type = (config.get("request_content_type") or "multipart/form-data").lower()
    try:
        if content_type == "multipart/form-data":
            image_field_name = config.get("image_field_name") or "image"
            opened_files = []
            try:
                files = []
                for path in image_paths:
                    image_file = open(path, "rb")
                    opened_files.append(image_file)
                    files.append((image_field_name, image_file))
                response = requests.post(url, headers=headers, data=params, files=files, timeout=timeout)
            finally:
                for image_file in opened_files:
                    image_file.close()
        elif content_type == "application/json":
            image_values = [encode_image_as_data_url(path) for path in image_paths]
            params[config.get("image_field_name") or "image"] = image_values if len(image_values) > 1 else image_values[0]
            response = requests.post(
                url,
                headers={**headers, "Content-Type": "application/json"},
                json=params,
                timeout=timeout,
            )
        else:
            raise ValueError("Unsupported request_content_type")
    except requests.Timeout as exc:
        raise RelayGatewayTimeoutError(
            "中转站连接超时，结果无法确认且可能已经扣费；系统不会自动重试。"
        ) from exc
    except requests.RequestException as exc:
        raise ValueError(f"中转站请求失败：{exc}") from exc

    _raise_for_status(response)
    try:
        return response.json()
    except ValueError as exc:
        summary = _summarize_error_body(response.text)
        raise ValueError(f"中转站返回非 JSON：{summary}") from exc


def save_images_from_response(response_json: dict[str, Any], config: dict[str, Any], job_id: int) -> list[dict[str, str]]:
    path = config.get("response_image_path") or ""
    try:
        values = json_path_get(response_json, path)
    except ValueError as exc:
        raise ValueError(f"中转站返回结果中没有找到 {path}，请检查“返回图片字段路径”配置。{exc}") from exc
    values = [value for value in values if value]
    if not values:
        raise ValueError("生成结果为空")

    image_type = (config.get("response_image_type") or "base64").lower()
    timeout = int(config.get("timeout_seconds") or 300)
    saved: list[dict[str, str]] = []
    for index, value in enumerate(values):
        if image_type == "base64":
            image_bytes = decode_base64_image(str(value))
            path_saved = save_result_bytes(job_id, index, image_bytes)
            saved.append({"path": path_saved, "source_type": "base64"})
        elif image_type == "url":
            url = str(value)
            image_bytes = download_image(url, timeout)
            path_saved = save_result_bytes(job_id, index, image_bytes, suffix_from_url(url))
            saved.append({"path": path_saved, "source_type": "url"})
        else:
            raise ValueError("Unsupported response_image_type")
    return saved
