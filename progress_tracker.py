"""Progress of the Metashape operation that is running now, and cancellation from inside it.

Metashape calls the ``progress`` callback (``Callable[[float], None]`` in API 2.2.0) of
processing methods and of ``Task.apply`` with a percentage. The tracker keeps the latest value so
that the reentrant ``get_service_status`` can report it while the GUI thread is busy.

Probe 0.34: an exception raised from the callback is treated by the GUI as a processing error and
opens the modal "<message> Повторить обработку?" dialog, which blocks until a human answers. So the
callback never raises; a cancel request is only recorded (the job stops at the next step boundary,
the running operation can be stopped with "Отмена" in Metashape's progress window).
"""

import time

STALL_SECONDS = 300


def stage_progress(snapshot, index, count):
    """Attach zero-based stage position and monotonic overall percentage to a snapshot."""
    if count <= 0 or index < 0 or index >= count:
        raise ValueError("Invalid stage position.")
    if snapshot.get("running"):
        percent = snapshot.get("percent")
        share = 0.0 if percent is None else min(100.0, max(0.0, float(percent))) / 100.0
    else:
        # The Metashape call has finished while the server may still be recording its result.
        share = 1.0
    return {**snapshot, "stage_index": index, "stage_count": count,
            "overall_percent": round(100.0 * (index + share) / count, 1)}


class ProgressTracker:
    def __init__(self):
        self.job_id = None
        self.operation = None
        self.percent = None
        self.started = None
        self.updated = None
        self.calls = 0
        self.cancel_requested = False
        self.finished = None

    def begin(self, job_id, operation):
        self.__init__()
        self.job_id = job_id
        self.operation = operation
        self.started = self.updated = time.monotonic()

    def finish(self):
        if self.started is not None and self.finished is None:
            self.finished = time.monotonic()

    def request_cancel(self, job_id):
        if self.job_id is None or self.finished is not None or job_id != self.job_id:
            return False
        self.cancel_requested = True
        return True

    def __call__(self, value):
        self.calls += 1
        try:
            self.percent = float(value)
        except (TypeError, ValueError):
            pass
        self.updated = time.monotonic()

    def snapshot(self):
        if self.started is None:
            return None
        now = self.finished or time.monotonic()
        since_update = now - self.updated
        return {"job_id": self.job_id, "operation": self.operation,
                "percent": None if self.percent is None else round(self.percent, 1),
                "elapsed_seconds": round(now - self.started, 1),
                "seconds_since_update": round(since_update, 1),
                "callbacks": self.calls, "running": self.finished is None,
                "cancel_requested": self.cancel_requested,
                "stalled": self.finished is None and since_update > STALL_SECONDS}


TRACKER = ProgressTracker()
