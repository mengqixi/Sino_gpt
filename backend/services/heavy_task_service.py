from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from threading import BoundedSemaphore, Lock, Thread
from typing import Any, Callable

from ..config import PROJECT_DIR
from ..runtime import module_worker_command, prewarm_worker_command


_MAX_PENDING_HEAVY_TASKS = 3
_HEAVY_TASK_SLOTS = BoundedSemaphore(_MAX_PENDING_HEAVY_TASKS)
_HEAVY_TASK_EXECUTION = Lock()
_PREWARM_GUARD = Lock()
_PREWARM_STATE: dict[str, Any] | None = None
_WORKER_FEATURES = {
    "backend.services.cutout_pipeline_worker": "cutout",
    "backend.services.organizer_render_worker": "organizer",
    "backend.services.recolor_worker": "recolor",
}


class HeavyTaskSuperseded(ValueError):
    """Raised when a newer request makes an image worker result obsolete."""


def _raise_if_superseded(is_superseded: Callable[[], bool] | None) -> None:
    if is_superseded is not None and is_superseded():
        raise HeavyTaskSuperseded("预览已被更新的调整替代")


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _communicate_worker(
    process: subprocess.Popen[str],
    timeout: int,
    is_superseded: Callable[[], bool] | None,
) -> tuple[str, str]:
    """Wait for a worker while allowing an obsolete preview to stop early."""
    deadline = time.monotonic() + timeout
    while True:
        if is_superseded is not None and is_superseded():
            _terminate_process(process)
            raise HeavyTaskSuperseded("预览已被更新的调整替代")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process(process)
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
            return stdout or "", stderr or ""
        except subprocess.TimeoutExpired:
            continue


def _worker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    })
    return environment


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    """Publish worker input only after the complete JSON document is durable.

    The prewarmed worker polls for ``input.json``. Writing that file directly
    creates a short window where the worker can observe an empty or partial
    document. A completed temporary file followed by ``os.replace`` keeps the
    publication atomic on the same filesystem.
    """
    serialized = json.dumps(payload, ensure_ascii=False)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _read_worker_result(output_path: Path) -> Any:
    try:
        response = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("图像处理结果读取失败") from exc
    if not response.get("ok"):
        raise ValueError(str(response.get("error") or "图像处理失败"))
    return response.get("result")


def _worker_failure(completed: subprocess.CompletedProcess[str], output_path: Path) -> None:
    if completed.returncode == 0 and output_path.is_file():
        return
    detail = completed.stderr.strip()[-800:]
    message = "图像处理进程异常退出，请稍后重试"
    if completed.returncode in {-9, 137}:
        message = "服务器图像处理内存不足，请稍后重试"
    if detail:
        message = f"{message}：{detail}"
    raise ValueError(message)


def _stop_prewarmer_locked() -> None:
    global _PREWARM_STATE
    state = _PREWARM_STATE
    _PREWARM_STATE = None
    if not state:
        return
    process: subprocess.Popen[str] = state["process"]
    _terminate_process(process)
    shutil.rmtree(state["directory"], ignore_errors=True)


def _clean_stale_prewarmer_locked() -> None:
    if _PREWARM_STATE and _PREWARM_STATE["process"].poll() is not None:
        _stop_prewarmer_locked()


def _reap_idle_prewarmer(process: subprocess.Popen[str], directory: Path) -> None:
    global _PREWARM_STATE
    process.wait()
    with _PREWARM_GUARD:
        if _PREWARM_STATE and _PREWARM_STATE["process"] is process:
            _PREWARM_STATE = None
            shutil.rmtree(directory, ignore_errors=True)


def prewarm_heavy_task(feature: str) -> dict[str, Any]:
    """Start one disposable worker before the user's first heavy action."""
    global _PREWARM_STATE
    if feature not in set(_WORKER_FEATURES.values()):
        raise ValueError("不支持的预热功能")
    if not _HEAVY_TASK_EXECUTION.acquire(blocking=False):
        return {"feature": feature, "status": "busy"}
    try:
        with _PREWARM_GUARD:
            _clean_stale_prewarmer_locked()
            if _PREWARM_STATE and _PREWARM_STATE["feature"] == feature:
                return {"feature": feature, "status": "ready"}
            _stop_prewarmer_locked()
            directory = Path(tempfile.mkdtemp(prefix="sino-prewarm-"))
            process = subprocess.Popen(
                prewarm_worker_command(feature, directory),
                cwd=str(PROJECT_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                env=_worker_environment(),
            )
            _PREWARM_STATE = {
                "feature": feature,
                "directory": directory,
                "process": process,
            }
            Thread(
                target=_reap_idle_prewarmer,
                args=(process, directory),
                daemon=True,
            ).start()
            deadline = time.monotonic() + 20
            ready_path = directory / "ready"
            while time.monotonic() < deadline:
                if ready_path.is_file():
                    return {"feature": feature, "status": "ready"}
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            _stop_prewarmer_locked()
            return {"feature": feature, "status": "unavailable"}
    finally:
        _HEAVY_TASK_EXECUTION.release()


def stop_prewarmer() -> None:
    with _HEAVY_TASK_EXECUTION:
        with _PREWARM_GUARD:
            _stop_prewarmer_locked()


def _consume_prewarmer(
    worker_module: str,
    payload: dict[str, Any],
    timeout: int,
    is_superseded: Callable[[], bool] | None = None,
) -> tuple[bool, Any]:
    global _PREWARM_STATE
    feature = _WORKER_FEATURES.get(worker_module)
    with _PREWARM_GUARD:
        _clean_stale_prewarmer_locked()
        state = _PREWARM_STATE
        if not state:
            return False, None
        if state["feature"] != feature:
            _stop_prewarmer_locked()
            return False, None
        _PREWARM_STATE = None

    directory: Path = state["directory"]
    process: subprocess.Popen[str] = state["process"]
    input_path = directory / "input.json"
    output_path = directory / "output.json"
    try:
        try:
            _raise_if_superseded(is_superseded)
            _write_json_atomically(input_path, payload)
            _, stderr = _communicate_worker(process, timeout, is_superseded)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("图像处理超时，请稍后重试") from exc
        completed = subprocess.CompletedProcess(
            process.args,
            process.returncode,
            stdout="",
            stderr=stderr or "",
        )
        _worker_failure(completed, output_path)
        return True, _read_worker_result(output_path)
    finally:
        _terminate_process(process)
        shutil.rmtree(directory, ignore_errors=True)


def _run_direct_worker(
    worker_module: str,
    payload: dict[str, Any],
    timeout: int,
    is_superseded: Callable[[], bool] | None = None,
) -> Any:
    with tempfile.TemporaryDirectory(prefix="sino-heavy-task-") as directory:
        task_dir = Path(directory)
        input_path = task_dir / "input.json"
        output_path = task_dir / "output.json"
        _raise_if_superseded(is_superseded)
        _write_json_atomically(input_path, payload)
        try:
            process = subprocess.Popen(
                module_worker_command(worker_module, input_path, output_path),
                cwd=str(PROJECT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_worker_environment(),
            )
            stdout, stderr = _communicate_worker(process, timeout, is_superseded)
            completed = subprocess.CompletedProcess(
                process.args,
                process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("图像处理超时，请稍后重试") from exc
        except OSError as exc:
            raise ValueError("图像处理进程启动失败") from exc
        _worker_failure(completed, output_path)
        return _read_worker_result(output_path)


def run_heavy_task(
    worker_module: str,
    payload: dict[str, Any],
    *,
    timeout: int = 600,
    is_superseded: Callable[[], bool] | None = None,
) -> Any:
    """Run one heavy task outside the resident API process.

    Up to three requests may enter the shared queue, only one worker executes
    at once, and a matching page-open prewarmer is consumed by the first task.
    """
    _raise_if_superseded(is_superseded)
    with _HEAVY_TASK_SLOTS:
        _raise_if_superseded(is_superseded)
        with _HEAVY_TASK_EXECUTION:
            _raise_if_superseded(is_superseded)
            consumed, result = _consume_prewarmer(
                worker_module,
                payload,
                timeout,
                is_superseded,
            )
            if consumed:
                return result
            return _run_direct_worker(
                worker_module,
                payload,
                timeout,
                is_superseded,
            )
