import unittest
from unittest import mock

import progress_tracker
from progress_tracker import ProgressTracker, stage_progress


class ProgressTrackerTest(unittest.TestCase):
    def test_reports_percent_and_stall(self):
        clock = [100.0]
        with mock.patch.object(progress_tracker.time, "monotonic", lambda: clock[0]):
            tracker = ProgressTracker()
            self.assertIsNone(tracker.snapshot())
            tracker.begin("job", "autopilot_step2")
            clock[0] = 110.0
            tracker(42.25)
            clock[0] = 110.0 + progress_tracker.STALL_SECONDS + 1
            state = tracker.snapshot()
        self.assertEqual(state["percent"], 42.2)
        self.assertEqual(state["callbacks"], 1)
        self.assertTrue(state["stalled"])
        self.assertTrue(state["running"])

    def test_cancel_is_recorded_for_the_same_job_only_and_never_raises(self):
        tracker = ProgressTracker()
        tracker.begin("job", "build")
        self.assertFalse(tracker.request_cancel("other"))
        tracker(10)
        self.assertTrue(tracker.request_cancel("job"))
        tracker(11)  # an exception here would open Metashape's modal retry dialog
        self.assertTrue(tracker.snapshot()["cancel_requested"])
        tracker.finish()
        self.assertFalse(tracker.request_cancel("job"))
        self.assertFalse(tracker.snapshot()["running"])

    def test_stage_progress_does_not_drop_after_operation_finishes(self):
        running = stage_progress({"running": True, "percent": 50}, 1, 4)
        finished = stage_progress({"running": False, "percent": 99}, 1, 4)
        self.assertEqual(running["overall_percent"], 37.5)
        self.assertEqual(finished["overall_percent"], 50.0)


if __name__ == "__main__":
    unittest.main()
