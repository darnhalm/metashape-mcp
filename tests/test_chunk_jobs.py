import asyncio
import tempfile
import unittest
from pathlib import Path

from processing_jobs import ProcessingJobs


class FakeBridge:
    def __init__(self, fail=False, hold=False):
        self.calls = []
        self.fail = fail
        self.hold = hold
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, method, **parameters):
        self.calls.append((method, parameters))
        if method in ("chunk_alignment_prepare", "chunk_merge_prepare"):
            return {"project": "p.psx", "chunk_label": "Reference", "frame_index": 0, "total": 3,
                    "fingerprint": "fp", "summary": {"method": parameters["parameters"].get("method")}}
        if method == "chunk_alignment_apply":
            if self.fail:
                raise RuntimeError("Метод недоступен")
            if self.hold:
                self.started.set()
                await self.release.wait()
            return {"moved_chunks": 2, "restore_point": {
                "restore_point_id": "rp-1", "reason": "before:chunk_alignment_apply", "kind": "psx_metadata"}}
        if method == "chunk_merge_apply":
            return {"chunk_key_created": 7, "restore_point": {
                "restore_point_id": "rp-2", "reason": "before:chunk_merge_apply", "kind": "psx_metadata"}}
        raise AssertionError(method)


class ChunkJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.events = []
        self.created = []

    async def asyncTearDown(self):
        for jobs in self.created:  # close SQLite connections (ResourceWarning on Python 3.13)
            await jobs.close()
        self.temporary.cleanup()

    def jobs(self, bridge):
        jobs = ProcessingJobs(Path(self.temporary.name), bridge,
                              lambda event, **fields: self.events.append(event))
        self.created.append(jobs)
        return jobs

    async def test_alignment_runs_in_place_with_restore_point(self):
        bridge = FakeBridge()
        jobs = self.jobs(bridge)
        job = await jobs.prepare_chunk_alignment(0, [1, 2], {"method": "markers", "fit_scale": True})
        self.assertEqual((job["kind"], job["state"]), ("chunk_alignment", "prepared"))
        self.assertEqual(job["parameters"]["chunk_keys"], [1, 2])
        jobs.start(job["job_id"], "chunk_alignment")
        await jobs.task
        info = jobs.info(job["job_id"])
        self.assertEqual(info["state"], "completed")
        self.assertEqual(info["restore_point_id"], "rp-1")
        self.assertEqual(info["assets"]["moved_chunks"], 2)
        method, request = bridge.calls[-1]
        self.assertEqual((method, request["fingerprint"]), ("chunk_alignment_apply", "fp"))
        self.assertIn("restore_point_created", self.events)
        self.assertEqual(jobs.status(job["job_id"], 0, 10)["kind"], "chunk_alignment")

    async def test_merge_records_new_chunk(self):
        jobs = self.jobs(FakeBridge())
        job = await jobs.prepare_chunk_merge([1, 2], {"copy_masks": True})
        jobs.start(job["job_id"], "chunk_merge")
        await jobs.task
        info = jobs.info(job["job_id"])
        self.assertEqual((info["state"], info["copy_key"]), ("completed", 7))

    async def test_failure_and_cancel_before_start(self):
        jobs = self.jobs(FakeBridge(fail=True))
        job = await jobs.prepare_chunk_alignment(0, [1], {"method": "points"})
        jobs.start(job["job_id"], "chunk_alignment")
        await jobs.task
        self.assertEqual(jobs.info(job["job_id"])["state"], "failed")
        other = await jobs.prepare_chunk_alignment(0, [1], {"method": "points"})
        self.assertEqual(jobs.cancel(other["job_id"])["state"], "cancelled")
        with self.assertRaises(ValueError):
            jobs.start(other["job_id"], "chunk_merge")

    async def test_cancel_during_inplace_call_records_cancelled_and_restore_point(self):
        bridge = FakeBridge(hold=True)
        jobs = self.jobs(bridge)
        job = await jobs.prepare_chunk_alignment(0, [1], {"method": "markers"})
        jobs.start(job["job_id"], "chunk_alignment")
        await asyncio.wait_for(bridge.started.wait(), 5)
        self.assertEqual(jobs.cancel(job["job_id"])["state"], "cancelling")
        bridge.release.set()
        await jobs.task
        result = jobs.info(job["job_id"])
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["restore_point_id"], "rp-1")


if __name__ == "__main__":
    unittest.main()
