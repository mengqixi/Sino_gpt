import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
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

        def fake_run(_module, payload, _timeout):
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

    def test_unknown_prewarm_feature_is_rejected(self):
        with self.assertRaises(ValueError):
            service.prewarm_heavy_task("unknown")

    def test_prewarm_waits_three_minutes_before_idle_exit(self):
        self.assertEqual(prewarm_worker.PREWARM_IDLE_SECONDS, 180)


if __name__ == "__main__":
    unittest.main()
