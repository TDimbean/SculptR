"""Download intent and progress journals: who writes them, what survives a crash.

The rule these tests protect is the one the whole design hangs on: the user's
intent and the engine's progress are separate files with separate writers, so an
engine that dies (or is replaced by a new session) cannot erase what the user
asked for, and the UI cannot pretend it owns byte offsets.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from sculptr import transfers
from sculptr.transfers import (
    ACTIVE,
    DISCARDED,
    DISCARDED_STATE,
    INSTALLED,
    PAUSED,
    PAUSED_STATE,
    QUEUED,
    REQUESTED,
    RETRYING,
    IntentJournal,
    JournalError,
    TransferStateJournal,
    download_root,
    has_trustworthy_digest,
    intent_directory,
    intent_path,
    merged_records,
    model_record,
    partial_path,
    payload_identity,
    reconcile_offset,
    staged_path,
    state_directory,
    transfer_id,
)

from tests.support import spec


class JournalLayoutTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sculptr-journal-"))

    def test_journals_are_separate_directories_under_the_models_root(self):
        self.assertEqual(download_root(self.root), self.root / ".sculptr" / "downloads")
        self.assertEqual(intent_directory(self.root), download_root(self.root) / "intent")
        self.assertEqual(state_directory(self.root), download_root(self.root) / "state")
        self.assertNotEqual(intent_directory(self.root), state_directory(self.root))

    def test_payload_store_keeps_the_manifest_layout(self):
        plain = partial_path(self.root, "model-a", "weights.bin")
        self.assertEqual(plain, self.root / "model-a.partial" / "weights.bin.part")
        nested = partial_path(self.root, "model-a", "sub/dir/config.yaml")
        self.assertEqual(nested, self.root / "model-a.partial" / "sub/dir" / "config.yaml.part")
        self.assertEqual(
            staged_path(self.root, "model-a", "sub/dir/config.yaml"),
            self.root / "model-a.partial" / "sub/dir" / "config.yaml.staged",
        )
        # Two repositories shipping the same file name must not share one payload.
        self.assertNotEqual(
            partial_path(self.root, "model-a", "config.yaml"),
            partial_path(self.root, "model-b", "config.yaml"),
        )

    def test_paths_that_escape_the_store_are_refused(self):
        for unsafe in ("../evil.bin", "/etc/passwd", "a/../../b.bin"):
            with self.assertRaises(ValueError, msg=unsafe):
                partial_path(self.root, "model-a", unsafe)

    def test_hostile_transfer_id_cannot_write_outside_the_journal(self):
        with self.assertRaises(ValueError):
            intent_path(self.root, "../../intent-evil")
        with self.assertRaises(ValueError):
            intent_path(self.root, "t-nope")


class IntentJournalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sculptr-intent-"))
        self.intents = IntentJournal(self.root)
        self.model = spec(b"x" * 10)
        self.intent = self.intents.record(
            "test-model", self.model, license_required=False, license_authorized=True
        )
        self.identifier = self.intent["transfer_id"]

    def test_transfer_id_is_stable_and_bound_to_the_pinned_revision(self):
        again = self.intents.record(
            "test-model", self.model, license_required=False, license_authorized=True
        )
        self.assertEqual(again["transfer_id"], self.identifier)
        self.assertTrue(again["transfer_id"].startswith("t-"))
        self.assertTrue(transfers._TRANSFER_ID_RE.fullmatch(again["transfer_id"]))
        pinned = again["pinned"]
        self.assertNotEqual(
            transfer_id("test-model", [{**pinned[0], "revision": "other-revision"}]),
            self.identifier,
            "a different pinned revision is a different download",
        )
        moved = spec(b"x" * 10, model_id="other-model")
        self.intents.record("other-model", moved, license_required=False, license_authorized=True)
        other = self.intents.record(
            "other-model", moved, license_required=False, license_authorized=True
        )
        self.assertNotEqual(other["transfer_id"], self.identifier)

    def test_intent_survives_a_reload_and_renames_nothing(self):
        reloaded = IntentJournal(self.root).load(self.identifier)
        self.assertEqual(reloaded, self.intent)
        self.assertEqual(reloaded["request"], REQUESTED)
        self.assertEqual(reloaded["model_id"], "test-model")
        self.assertEqual(reloaded["authorization"]["license_authorized"], True)

    def test_pause_resume_discard_are_user_decisions_on_the_intent_only(self):
        self.assertEqual(self.intents.set_request(self.identifier, PAUSED)["request"], PAUSED)
        self.assertEqual(IntentJournal(self.root).load(self.identifier)["request"], PAUSED)
        self.assertEqual(self.intents.set_request(self.identifier, REQUESTED)["request"], REQUESTED)
        self.assertEqual(self.intents.set_request(self.identifier, DISCARDED)["request"], DISCARDED)
        self.assertEqual(self.intents.authorize(self.identifier, False)["authorization"]["license_authorized"], False)

    def test_unknown_request_and_unknown_transfer_are_reported_not_guessed(self):
        with self.assertRaises(ValueError):
            self.intents.set_request(self.identifier, "halfway")
        with self.assertRaises(KeyError):
            self.intents.set_request("t-" + "0" * 32, PAUSED)

    def test_a_corrupt_intent_file_reads_as_absent(self):
        path = intent_path(self.root, self.identifier)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(IntentJournal(self.root).load(self.identifier), {})
        path.write_text(json.dumps({"schema": 99, "transfer_id": self.identifier}), encoding="utf-8")
        self.assertEqual(IntentJournal(self.root).load(self.identifier), {})

    def test_a_stray_file_does_not_break_the_iter(self):
        (intent_directory(self.root) / "notes.json").write_text('{"schema":1}', encoding="utf-8")
        (intent_directory(self.root) / "t-zzz.json").write_text('{"schema":1}', encoding="utf-8")
        identifiers = [record["transfer_id"] for record in IntentJournal(self.root).iter()]
        self.assertEqual(identifiers, [self.identifier])

    def test_a_failed_write_is_an_error_and_leaves_no_temp_file_behind(self):
        """Silently losing an intent means the user's download vanishes."""
        original = os.replace
        try:
            os.replace = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
            with self.assertRaises(JournalError):
                self.intents.set_request(self.identifier, PAUSED)
        finally:
            os.replace = original
        strays = [path.name for path in intent_directory(self.root).iterdir() if ".json." in path.name]
        self.assertEqual(strays, [])
        self.assertEqual(IntentJournal(self.root).load(self.identifier)["request"], REQUESTED)


class TransferStateJournalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sculptr-state-"))
        self.intents = IntentJournal(self.root)
        self.states = TransferStateJournal(self.root)
        self.intent = self.intents.record(
            "test-model", spec(b"x" * 10), license_required=False, license_authorized=True
        )
        self.identifier = self.intent["transfer_id"]
        self.manifest = [
            {"local_path": "weights.bin", "size": 1_000, "sha256": "a" * 64},
            {"local_path": "config.yaml", "size": 100, "sha256": None},
        ]

    def test_begin_records_the_manifest_and_totals(self):
        record = self.states.begin(self.intent, self.manifest, attempt_id="attempt-1", session_id="s-1")
        self.assertEqual(record["state"], QUEUED)
        self.assertEqual(record["attempt_id"], "attempt-1")
        self.assertEqual(record["total_bytes"], 1100)
        self.assertEqual(record["completed_bytes"], 0)
        self.assertEqual(set(record["files"]), {"weights.bin", "config.yaml"})
        # State lives in the engine's directory; the intent directory belongs to the UI.
        self.assertTrue((state_directory(self.root) / f"{self.identifier}.json").is_file())
        self.assertEqual(
            json.loads(
                (state_directory(self.root) / f"{self.identifier}.json").read_text("utf-8")
            )["state"],
            QUEUED,
        )
        self.assertNotIn("attempt_id", IntentJournal(self.root).load(self.identifier))

    def test_a_new_attempt_keeps_offsets_and_verified_flags_from_the_last_one(self):
        first = self.states.begin(self.intent, self.manifest, attempt_id="attempt-1", session_id="s-1")
        first = self.states.checkpoint(first, "weights.bin", 600, etag='"v1"')
        second = self.states.begin(
            self.intent, self.manifest, attempt_id="attempt-2", session_id="s-2"
        )
        self.assertEqual(second["attempt_id"], "attempt-2")
        self.assertNotEqual(second["session_id"], "s-1")
        self.assertEqual(second["files"]["weights.bin"]["offset"], 600)
        self.assertEqual(second["files"]["weights.bin"]["etag"], '"v1"')
        self.assertEqual(second["completed_bytes"], 600, "recovered bytes, not a fresh zero")

    def test_checkpoints_accumulate_across_files(self):
        record = self.states.begin(self.intent, self.manifest, attempt_id="a", session_id="s")
        record = self.states.checkpoint(record, "weights.bin", 400)
        record = self.states.checkpoint(record, "config.yaml", 100)
        self.assertEqual(record["completed_bytes"], 500)
        self.assertEqual(TransferStateJournal(self.root).load(self.identifier)["completed_bytes"], 500)
        record = self.states.checkpoint(record, "weights.bin", 1000)
        self.assertEqual(record["completed_bytes"], 1100)

    def test_a_state_write_never_rewinds_bytes_another_writer_committed(self):
        """Shutdown and pause writes are made from a record the caller copied
        earlier. A worker may have committed newer bytes since then, and the
        atomic replace would otherwise overwrite them, so a state-only write
        must merge the newest durable offsets instead of losing them."""
        record = self.states.begin(self.intent, self.manifest, attempt_id="a", session_id="s")
        stale = dict(record)
        self.states.checkpoint(record, "weights.bin", 600)
        self.states.update(stale, state=PAUSED_STATE, error_code="engine_stopped")
        durable = TransferStateJournal(self.root).load(self.identifier)
        self.assertEqual(durable["state"], PAUSED_STATE)
        self.assertEqual(durable["files"]["weights.bin"]["offset"], 600)
        self.assertEqual(durable["completed_bytes"], 600)

    def test_a_checkpoint_after_a_clean_restart_may_still_lower_the_offset(self):
        """The merge is not applied to checkpoints: they are the only writer that
        knows a payload was legitimately restarted, so a lower offset is real."""
        record = self.states.begin(self.intent, self.manifest, attempt_id="a", session_id="s")
        record = self.states.checkpoint(record, "weights.bin", 600)
        record = self.states.checkpoint(record, "weights.bin", 100)
        self.assertEqual(record["completed_bytes"], 100)
        self.assertEqual(
            TransferStateJournal(self.root).load(self.identifier)["files"]["weights.bin"]["offset"],
            100,
        )

    def test_only_a_terminal_state_stamps_completion(self):
        record = self.states.begin(self.intent, self.manifest, attempt_id="a", session_id="s")
        self.assertIsNone(record["finished_at"])
        retrying = self.states.finish(record, RETRYING, error_code="timeout", message="retry soon")
        self.assertEqual(retrying["state"], RETRYING)
        self.assertIsNone(retrying["finished_at"], "a retry is not the end of the transfer")
        done = self.states.finish(retrying, INSTALLED, message="installed", phase="installed")
        self.assertEqual(done["state"], INSTALLED)
        self.assertIsNotNone(done["finished_at"])
        paused = self.states.finish(done, PAUSED_STATE, error_code="no_progress")
        self.assertIn(paused["state"], transfers.TERMINAL_STATES)
        discarded = self.states.finish(paused, DISCARDED_STATE)
        self.assertEqual(discarded["state"], DISCARDED_STATE)
        # A new attempt for the same transfer re-opens the record cleanly.
        reopened = self.states.begin(
            self.intent, self.manifest, attempt_id="attempt-2", session_id="s-2"
        )
        self.assertIsNone(reopened["finished_at"])
        self.assertEqual(reopened["state"], QUEUED)

    def test_a_bad_state_record_is_skipped_by_the_iter(self):
        record = self.states.begin(self.intent, self.manifest, attempt_id="a", session_id="s")
        state_directory(self.root).joinpath(f"{record['transfer_id']}.json").write_text(
            '{"schema": 2, "transfer_id": "t-x"}', encoding="utf-8"
        )
        self.assertEqual(TransferStateJournal(self.root).iter(), [])


class MergedRecordTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sculptr-merged-"))
        self.intents = IntentJournal(self.root)
        self.states = TransferStateJournal(self.root)

    def test_intent_without_state_shows_as_queued(self):
        intent = self.intents.record(
            "test-model", spec(b"x"), license_required=False, license_authorized=True
        )
        records = merged_records(self.root)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["transfer_id"], intent["transfer_id"])
        self.assertEqual(records[0]["state"], QUEUED)
        self.assertEqual(records[0]["intent_request"], REQUESTED)
        self.assertEqual(records[0]["completed_bytes"], 0)

    def test_paused_intent_without_state_is_not_shown_as_active(self):
        intent = self.intents.record(
            "test-model", spec(b"x"), license_required=False, license_authorized=True
        )
        self.intents.set_request(intent["transfer_id"], PAUSED)
        self.assertEqual(merged_records(self.root)[0]["state"], PAUSED)

    def test_state_is_joined_onto_the_intent(self):
        intent = self.intents.record(
            "test-model", spec(b"x"), license_required=False, license_authorized=True
        )
        record = self.states.begin(intent, [{"local_path": "w.bin", "size": 10}], attempt_id="a", session_id="s")
        self.states.checkpoint(record, "w.bin", 7)
        merged = merged_records(self.root)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["state"], QUEUED)
        self.assertEqual(merged[0]["completed_bytes"], 7)
        self.assertEqual(merged[0]["total_bytes"], 10)
        self.assertEqual(merged[0]["attempt_id"], "a")
        self.assertEqual(model_record(self.root, "test-model")["transfer_id"], intent["transfer_id"])

    def test_an_orphan_state_is_still_visible(self):
        intent = self.intents.record(
            "test-model", spec(b"x"), license_required=False, license_authorized=True
        )
        record = self.states.begin(intent, [{"local_path": "w.bin", "size": 10}], attempt_id="a", session_id="s")
        intent_path(self.root, intent["transfer_id"]).unlink()
        merged = merged_records(self.root)
        self.assertEqual(len(merged), 1)
        self.assertIsNone(merged[0]["intent_request"])
        self.assertEqual(merged[0]["state"], QUEUED)

    def test_unnamed_journal_files_are_skipped(self):
        intent_directory(self.root).mkdir(parents=True, exist_ok=True)
        (intent_directory(self.root) / "t-dead.json").write_text(
            json.dumps({"schema": 1, "transfer_id": "t-dead", "model_id": "x", "request": REQUESTED}),
            encoding="utf-8",
        )
        self.assertEqual(merged_records(self.root), [])


class OffsetReconciliationTests(unittest.TestCase):
    def test_uncommitted_tail_is_the_only_data_ever_discarded(self):
        offset, reason = reconcile_offset(5_000, 4_000)
        self.assertEqual((offset, reason), (4_000, "truncated_uncommitted_tail"))

    def test_a_checkpoint_ahead_of_the_volume_resumes_at_what_exists(self):
        offset, reason = reconcile_offset(3_000, 4_000)
        self.assertEqual((offset, reason), (3_000, "checkpoint_ahead_of_disk"))

    def test_agreement_needs_no_note(self):
        self.assertEqual(reconcile_offset(4_000, 4_000), (4_000, None))


class PayloadIdentityTests(unittest.TestCase):
    def test_size_alone_never_proves_a_prefix(self):
        entry = {"local_path": "w.bin", "size": 10, "sha256": None, "git_sha1": None}
        self.assertFalse(has_trustworthy_digest(entry))
        self.assertTrue(has_trustworthy_digest({**entry, "sha256": "a" * 64}))
        self.assertTrue(has_trustworthy_digest({**entry, "git_sha1": "b" * 40}))

    def test_identity_names_the_revision_not_just_the_file(self):
        identity = payload_identity(
            {"repo_id": "r", "revision": "rev", "remote_path": "w.bin", "local_path": "w.bin", "size": 10}
        )
        self.assertEqual(identity["revision"], "rev")
        self.assertEqual(identity["remote_path"], "w.bin")
        self.assertEqual(identity["size"], 10)


class ModelIdSafetyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sculptr-modelid-"))

    def test_a_model_id_cannot_move_the_payload_store(self):
        with self.assertRaises(ValueError):
            partial_path(self.root, "../victim", "w.bin")
        with self.assertRaises(ValueError):
            partial_path(self.root, "", "w.bin")


if __name__ == "__main__":
    unittest.main()
