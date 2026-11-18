"""The Folder panel gate reads work that is still writing.

The reported failure was a Folder drawer that always said "Download running"
and always greyed the change-folder button, with Pause and Cancel changing
nothing. Both symptoms come from one boolean, `ui_helpers.has_active_jobs`, so
the boolean has to be the tested shape. Its job clause used `ACTIVE_STATUSES`,
which contains `paused`: a download the user had already stopped is unfinished
but writes nothing, and the archives of such a job sit in the snapshot forever,
so the gate stayed on long after the session that wrote it died.

`IN_PROGRESS_STATUSES` is the set that claims a writer. The bridge already
reads its own version of that set, which omits `paused`, and `transfer_busy`
already excludes the paused transfer state; the job clause was the last
layer still disagreeing. The sets themselves are pinned so the fix cannot
pass by moving `paused` out of `ACTIVE_STATUSES`, which would break the rows
that must keep showing a paused download and offer Resume.
"""

from __future__ import annotations

import unittest

from sculptr.ui_helpers import (
    ACTIVE_STATUSES,
    IN_PROGRESS_STATUSES,
    has_active_jobs,
    transfer_busy,
)


class FolderGateTests(unittest.TestCase):
    def test_only_statuses_that_write_a_folder_are_busy(self) -> None:
        self.assertIn("paused", ACTIVE_STATUSES)
        self.assertNotIn("paused", IN_PROGRESS_STATUSES)

    def test_a_paused_download_leaves_the_folder_changeable(self) -> None:
        """Nothing is writing, so the drawer must not hold the folder hostage."""
        snapshot = {
            "jobs": [{"job_id": "resume-1", "action": "download_model", "status": "paused"}],
            "transfers": [],
        }
        self.assertFalse(has_active_jobs(snapshot))

    def test_work_the_user_has_not_stopped_still_holds_the_folder(self) -> None:
        for status in ("queued", "downloading", "retrying", "waiting", "verifying", "checking"):
            snapshot = {"jobs": [{"job_id": "job-1", "action": "download_model", "status": status}]}
            self.assertTrue(has_active_jobs(snapshot))

    def test_a_running_transfer_holds_the_folder_without_any_job(self) -> None:
        snapshot = {
            "jobs": [],
            "transfers": [{"transfer_id": "t-" + "0" * 32, "state": "active"}],
        }
        self.assertTrue(has_active_jobs(snapshot))
        self.assertTrue(transfer_busy(snapshot["transfers"][0]))

    def test_finished_and_abandoned_records_free_the_gate(self) -> None:
        snapshot = {
            "jobs": [
                {"job_id": "a", "action": "download_model", "status": "installed"},
                {"job_id": "b", "action": "download_model", "status": "cancelled"},
                {"job_id": "c", "action": "download_model", "status": "failed"},
                {"job_id": "d", "action": "download_model", "status": "paused"},
                {"job_id": "e", "action": "shutdown", "status": "completed"},
            ],
            "transfers": [
                {"transfer_id": "t-" + "1" * 32, "state": "installed"},
                {"transfer_id": "t-" + "2" * 32, "state": "paused"},
                {"transfer_id": "t-" + "3" * 32, "state": "discarded"},
                {"transfer_id": "t-" + "4" * 32, "state": "failed"},
            ],
        }
        self.assertFalse(has_active_jobs(snapshot))
