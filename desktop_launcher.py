from __future__ import annotations

import importlib
import hashlib
import json
import logging
import multiprocessing
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path
from typing import Sequence


APP_NAME = "SinoImageTool"
APP_TITLE = "箱包图片工作台"
HEALTH_TIMEOUT_SECONDS = 45


def user_data_dir() -> Path:
    override = os.environ.get("SINO_GPT_DESKTOP_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return Path(root) / APP_NAME
    root = os.environ.get("XDG_DATA_HOME", "").strip()
    return (Path(root).expanduser() if root else Path.home() / ".local" / "share") / APP_NAME


def _configure_runtime_environment(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    (data_dir / "results").mkdir(parents=True, exist_ok=True)
    os.environ["SINO_GPT_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")


def _bundled_api_seed_path() -> Path | None:
    relative = Path("desktop") / "private" / "api-configs.json"
    candidates = [
        Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / relative,
        Path(sys.executable).resolve().parent / "_internal" / relative,
        Path(sys.executable).resolve().parent.parent / "Resources" / relative,
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _install_bundled_api_seed(data_dir: Path) -> int:
    seed_path = _bundled_api_seed_path()
    if seed_path is None:
        return 0
    seed_bytes = seed_path.read_bytes()
    seed_digest = hashlib.sha256(seed_bytes).hexdigest()
    marker = data_dir / ".api-seed-sha256"
    if marker.is_file() and marker.read_text(encoding="ascii").strip() == seed_digest:
        return 0
    payload = json.loads(seed_bytes.decode("utf-8"))
    configs = payload.get("api_configs") if isinstance(payload, dict) else None
    if not isinstance(configs, list) or not configs:
        raise RuntimeError("内置 API 配置为空或格式错误")

    from backend.database import db_session, init_db, now_iso
    from backend.services.api_config_service import API_CONFIG_FIELDS, VALID_API_TYPES

    init_db()
    applied = 0
    with db_session() as conn:
        for item in configs:
            if not isinstance(item, dict):
                continue
            api_type = str(item.get("api_type") or "").strip()
            config_name = str(item.get("config_name") or "").strip()
            api_base_url = str(item.get("api_base_url") or "").strip()
            api_key = str(item.get("api_key") or "").strip()
            if api_type not in VALID_API_TYPES or not config_name or not api_base_url or not api_key:
                continue
            data = {field: item.get(field) for field in API_CONFIG_FIELDS if field in item}
            data.update({
                "config_name": config_name,
                "api_type": api_type,
                "api_base_url": api_base_url,
                "api_key": api_key,
                "enabled": 1 if item.get("enabled", True) else 0,
                "is_default": 1 if item.get("is_default") else 0,
            })
            if data["is_default"]:
                conn.execute("UPDATE api_configs SET is_default = 0 WHERE api_type = ?", (api_type,))
            existing = conn.execute(
                "SELECT id FROM api_configs WHERE config_name = ? AND api_type = ?",
                (config_name, api_type),
            ).fetchone()
            fields = list(data)
            timestamp = now_iso()
            if existing:
                assignments = ", ".join(f"{field} = ?" for field in fields)
                conn.execute(
                    f"UPDATE api_configs SET {assignments}, updated_at = ? WHERE id = ?",
                    [*(data[field] for field in fields), timestamp, existing["id"]],
                )
            else:
                placeholders = ", ".join("?" for _ in fields)
                conn.execute(
                    f"INSERT INTO api_configs ({', '.join(fields)}, created_at, updated_at) "
                    f"VALUES ({placeholders}, ?, ?)",
                    [*(data[field] for field in fields), timestamp, timestamp],
                )
            applied += 1
    if applied == 0:
        raise RuntimeError("内置 API 配置没有可用项目")
    marker_tmp = marker.with_suffix(".tmp")
    marker_tmp.write_text(seed_digest, encoding="ascii")
    os.replace(marker_tmp, marker)
    return applied


def _dispatch_worker(arguments: Sequence[str]) -> int | None:
    if not arguments:
        return None
    mode = arguments[0]
    if mode == "--sino-module-worker" and len(arguments) == 4:
        module = importlib.import_module(arguments[1])
        from backend.services.heavy_worker_io import run_worker

        sys.argv = [sys.argv[0], arguments[2], arguments[3]]
        run_worker(module._handle)
        return 0
    if mode == "--sino-prewarm-worker" and len(arguments) == 3:
        from backend.services.prewarm_worker import main

        sys.argv = [sys.argv[0], arguments[1], arguments[2]]
        main()
        return 0
    if mode == "--sino-cutout-worker" and len(arguments) == 4:
        from backend.services.cutout_model_worker import main

        sys.argv = [sys.argv[0], arguments[1], arguments[2], arguments[3]]
        main()
        return 0
    return None


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _wait_for_health(url: str, timeout: float = HEALTH_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status == 200 and payload.get("status") == "ok":
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(0.15)
    raise RuntimeError("本地图片服务启动超时") from last_error


def _chrome_candidates() -> list[Path]:
    candidates: list[Path] = []
    if os.name == "nt":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = os.environ.get(variable)
            if root:
                candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            root = os.environ.get(variable)
            if root:
                candidates.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    elif sys.platform == "darwin":
        candidates.extend([
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
        ])
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            executable = shutil.which(name)
            if executable:
                candidates.append(Path(executable))
    return candidates


def find_chromium_browser() -> Path | None:
    return next((path for path in _chrome_candidates() if path.is_file()), None)


def _launch_app_browser(
    url: str,
) -> tuple[subprocess.Popen[bytes] | None, tempfile.TemporaryDirectory[str] | None]:
    executable = find_chromium_browser()
    if executable is None:
        webbrowser.open(url, new=1)
        return None, None
    profile_owner = tempfile.TemporaryDirectory(
        prefix="sino-browser-",
        ignore_cleanup_errors=True,
    )
    profile = Path(profile_owner.name)
    command = [
        str(executable),
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--disable-background-mode",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    return subprocess.Popen(command), profile_owner


def _show_fallback_window(url: str) -> None:
    import tkinter as tk

    window = tk.Tk()
    window.title(APP_TITLE)
    window.resizable(False, False)
    tk.Label(
        window,
        text="本地服务正在运行\n关闭此窗口即可退出",
        padx=28,
        pady=18,
        font=("Arial", 11),
    ).pack()
    tk.Button(window, text="重新打开页面", command=lambda: webbrowser.open(url), width=18).pack(pady=(0, 18))
    window.mainloop()


def _show_error(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        window = tk.Tk()
        window.withdraw()
        messagebox.showerror(APP_TITLE, message)
        window.destroy()
    except Exception:
        logging.exception("Unable to show desktop error dialog")


def _run_self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="sino-desktop-test-") as directory:
        data_dir = Path(directory)
        _configure_runtime_environment(data_dir)
        _install_bundled_api_seed(data_dir)
        from backend.app import FRONTEND_DIST
        from backend.database import init_db

        init_db()
        if not (FRONTEND_DIST / "index.html").is_file():
            raise RuntimeError(f"前端资源不存在：{FRONTEND_DIST}")
    return 0


def _run_server_self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="sino-desktop-server-test-") as directory:
        data_dir = Path(directory)
        _configure_runtime_environment(data_dir)
        _install_bundled_api_seed(data_dir)
        import uvicorn
        from backend.app import app

        port = _find_free_port()
        url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            workers=1,
            log_level="error",
            access_log=False,
            log_config=None,
        ))
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        try:
            _wait_for_health(url)
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status != 200 or b'<div id="root">' not in response.read():
                    raise RuntimeError("本地前端资源检查失败")
        finally:
            server.should_exit = True
            server_thread.join(timeout=8)
        if server_thread.is_alive():
            raise RuntimeError("本地服务未能正常退出")
    return 0


def run_desktop() -> int:
    data_dir = user_data_dir()
    _configure_runtime_environment(data_dir)
    _install_bundled_api_seed(data_dir)
    logging.basicConfig(
        filename=data_dir / "desktop.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    import uvicorn
    from backend.app import app

    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        workers=1,
        log_level="warning",
        access_log=False,
        log_config=None,
    ))
    server_thread = threading.Thread(target=server.run, name="local-api", daemon=True)
    server_thread.start()
    browser: subprocess.Popen[bytes] | None = None
    browser_profile: tempfile.TemporaryDirectory[str] | None = None
    try:
        _wait_for_health(url)
        browser, browser_profile = _launch_app_browser(url)
        if browser is not None:
            browser.wait()
        else:
            _show_fallback_window(url)
    finally:
        server.should_exit = True
        server_thread.join(timeout=8)
        if browser is not None and browser.poll() is None:
            browser.terminate()
        if browser_profile is not None:
            browser_profile.cleanup()
    return 0


def main() -> int:
    multiprocessing.freeze_support()
    try:
        worker_result = _dispatch_worker(sys.argv[1:])
        if worker_result is not None:
            return worker_result
        if sys.argv[1:] == ["--self-test"]:
            return _run_self_test()
        if sys.argv[1:] == ["--server-self-test"]:
            return _run_server_self_test()
        return run_desktop()
    except Exception as exc:
        logging.exception("Desktop launcher failed")
        diagnostic_path = os.environ.get("SINO_GPT_SELF_TEST_LOG", "").strip()
        if diagnostic_path:
            Path(diagnostic_path).write_text(traceback.format_exc(), encoding="utf-8")
        elif not any(argument.endswith("worker") or "self-test" in argument for argument in sys.argv[1:]):
            _show_error(f"程序启动失败：{exc}\n\n详细信息已写入 desktop.log")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
