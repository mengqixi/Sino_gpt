import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from unittest.mock import patch

from backend.services import heavy_task_service as service
from backend.services import prewarm_worker


class HeavyTaskServiceTests(unittest.TestCase):
    def tearDown(self):
        service.stop_prewarmer()

    def test_concurrent_heavy_tasks_execute_one_at_a_time(self):
        state_lock = Lock()
        active = 0
        max_active = 0

        def fake_run(_module, payload, _timeout, _is_superseded):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with state_lock:
                active -= 1
            return payload["value"]

        with (
            patch.object(service, "_consume_prewarmer", return_value=(False, None)),
            patch.object(service, "_run_direct_worker", side_effect=fake_run),
            ThreadPoolExecutor(max_workers=3) as executor,
        ):
            results = list(executor.map(
                lambda value: service.run_heavy_task("test.worker", {"value": value}),
                range(3),
            ))

        self.assertEqual(results, [0, 1, 2])
        self.assertEqual(max_active, 1)

    def test_superseded_worker_process_is_terminated(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        started = time.monotonic()
        with self.assertRaises(service.HeavyTaskSuperseded):
            service._communicate_worker(
                process,
                timeout=10,
                is_superseded=lambda: time.monotonic() - started > 0.15,
            )
        self.assertIsNotNone(process.poll())

    def test_queued_superseded_task_never_starts_a_worker(self):
        first_started = Event()
        release_first = Event()
        second_is_superseded = Event()
        worker_values: list[int] = []

        def fake_run(_module, payload, _timeout, _is_superseded):
            worker_values.append(payload["value"])
            if payload["value"] == 1:
                first_started.set()
                release_first.wait(timeout=2)
            return payload["value"]

        with (
            patch.object(service, "_consume_prewarmer", return_value=(False, None)),
            patch.object(service, "_run_direct_worker", side_effect=fake_run),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(
                service.run_heavy_task,
                "test.worker",
                {"value": 1},
            )
            self.assertTrue(first_started.wait(timeout=1))
            second = executor.submit(
                service.run_heavy_task,
                "test.worker",
                {"value": 2},
                is_superseded=second_is_superseded.is_set,
            )
            time.sleep(0.05)
            second_is_superseded.set()
            release_first.set()
            self.assertEqual(first.result(timeout=2), 1)
            with self.assertRaises(service.HeavyTaskSuperseded):
                second.result(timeout=2)

        self.assertEqual(worker_values, [1])

    def test_unknown_prewarm_feature_is_rejected(self):
        with self.assertRaises(ValueError):
            service.prewarm_heavy_task("unknown")

    def test_prewarm_waits_three_minutes_before_idle_exit(self):
        self.assertEqual(prewarm_worker.PREWARM_IDLE_SECONDS, 180)

    def test_worker_input_is_published_only_after_complete_json_is_written(self):
        with TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.json"
            real_replace = service.os.replace
            observed_payloads = []

            def inspect_then_replace(source, destination):
                self.assertFalse(input_path.exists())
                observed_payloads.append(service.json.loads(Path(source).read_text(encoding="utf-8")))
                real_replace(source, destination)

            payload = {"text": "中文" * 1000, "values": list(range(200))}
            with patch.object(service.os, "replace", side_effect=inspect_then_replace):
                service._write_json_atomically(input_path, payload)

            self.assertEqual(observed_payloads, [payload])
            self.assertEqual(service.json.loads(input_path.read_text(encoding="utf-8")), payload)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
