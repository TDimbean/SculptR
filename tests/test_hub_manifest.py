"""Manifest identity: what a hash in the revision metadata is allowed to prove.

The pinned-revision API answers with two hashes per file, and they are not the
same kind of hash.

* A file stored without a pointer has a content blob, and `blobId` is the SHA-1
  of `blob <size>\0` followed by the served bytes. That verifies the payload.
* A file stored in LFS or XET has no content blob. Its git blob is the ~135-byte
  pointer text, so `blobId` is the SHA-1 of that pointer and describes metadata,
  never the payload. `lfs.sha256` (`lfs.oid` in the tree form of the same API)
  is the SHA-256 of the content the CDN serves, and it is the only digest that
  can verify such a file.

The invariant under test is which of the two may be carried as a content digest.
Carrying a pointer hash was the defect seen in the wild on the Hunyuan
checkpoints: comparing a 7,366,389,768-byte payload against the hash of a
135-byte pointer fails always, and an always-failing check turns every
completed transfer into a quarantined payload and a clean restart from byte
zero, so the download reaches completion, begins again at zero, and never
finishes. A digest that cannot be trusted is dropped, not weakened; a
disagreement between the catalog pin and the pinned revision is refused before
a single byte is requested.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sculptr.catalog import CATALOG, FileRule, RepositorySpec
from sculptr.engine import hub
from sculptr.engine.hub import HubClient, HubError, IntegrityError, TransferPaused
from sculptr.transfers import staged_path

from tests.support import EventLog, REMOTE_PATH, REPO, REVISION, ServerBehaviour, controller, hooks, serve, spec


class ManifestIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="sculptr-manifest-"))
        # ~1 MiB of deterministic bytes, the same payload shape the transfer
        # tests move around.
        self.data = bytes(range(251)) * 4099
        self.model_id = "test-model"

    def test_pointer_stored_file_is_verified_with_its_own_content_digest(self) -> None:
        """The pinned revision's SHA-256 installs the file on the first pass."""
        behaviour = ServerBehaviour(data=self.data, stored_as_pointer=True)
        log = EventLog()
        with serve(behaviour) as endpoint:
            client = HubClient(endpoint, socket_timeout=10.0, log=log)
            remote = client.resolve_model(spec(self.data, pinned=False))[0]
            # The content digest of the pointer is trusted; the pointer's own
            # blob hash is not carried at all.
            self.assertEqual(remote.sha256, behaviour.pointer_digest)
            self.assertIsNone(remote.git_sha1)
            self.assertNotEqual(remote.sha256, behaviour.pointer_blob)
            self.assertEqual(remote.size, len(self.data))
            client.download(self.root, self.model_id, remote, hooks(), controller())
            # One pass, one request, verified: the pointer's content digest
            # matches, so nothing quarantines and nothing restarts.
            self.assertEqual(behaviour.requests, [{"range": None, "if_range": None}])
            self.assertNotIn("integrity_retry", log.names())
            complete = log.fields("download_complete")
            self.assertEqual(len(complete), 1)
            self.assertTrue(complete[0]["verified"])
        staged = staged_path(self.root, self.model_id, remote.local_path)
        self.assertEqual(staged.read_bytes(), self.data)

    def test_a_payload_contradicting_the_pointer_is_quarantined_once(self) -> None:
        """What the defect used to do: completion, then a restart from byte zero.

        The pointer claims a digest the served bytes contradict, so these are
        genuinely wrong bytes. A completed payload quarantines and asks for one
        clean pass; with no allowance left the same bytes surface a failure
        instead of starting a third pass, so nothing can loop.
        """
        behaviour = ServerBehaviour(data=self.data, stored_as_pointer=True, pointer_sha256="f" * 64)
        log = EventLog()
        with serve(behaviour) as endpoint:
            client = HubClient(endpoint, socket_timeout=10.0, log=log)
            remote = client.resolve_model(spec(self.data, pinned=False))[0]
            self.assertEqual(remote.sha256, "f" * 64)
            self.assertIsNone(remote.git_sha1)
            with self.assertRaises(TransferPaused) as restarted:
                client.download(self.root, self.model_id, remote, hooks(), controller())
            # The restart names itself: a quarantined payload, and the whole
            # file again from byte zero, which the caller is allowed to do once.
            self.assertIn("quarantined, re-downloading from zero", str(restarted.exception))
            self.assertEqual(log.names().count("integrity_retry"), 1)
            self.assertNotIn("clean_restart", log.names())
            self.assertFalse(staged_path(self.root, self.model_id, remote.local_path).is_file())
            # The same bytes, with no allowance left: the failure is told, and
            # no further pass is started. `download_model` makes exactly this
            # second call, which is what keeps the loop from closing.
            with self.assertRaises(IntegrityError):
                client.download(
                    self.root,
                    self.model_id,
                    remote,
                    hooks(),
                    controller(),
                    retry_allowed=False,
                )
            self.assertEqual(log.names().count("integrity_retry"), 1)
        self.assertEqual(len(behaviour.requests), 2)

    def test_a_pin_the_revision_contradicts_asks_for_no_bytes(self) -> None:
        """A disagreeing digest is settled from metadata, not from bandwidth."""
        behaviour = ServerBehaviour(data=self.data, stored_as_pointer=True)
        log = EventLog()
        with serve(behaviour) as endpoint:
            client = HubClient(endpoint, socket_timeout=10.0, log=log)
            with self.assertRaises(HubError) as refused:
                client.resolve_model(spec(self.data, pinned="a" * 64))
        self.assertIn(REMOTE_PATH, str(refused.exception))
        self.assertEqual(behaviour.requests, [])

    def test_sibling_shapes_resolve_by_the_rules_they_are_documented_by(self) -> None:
        """Resolution rules driven straight at a sibling entry, no transfer."""
        repository = RepositorySpec(
            repo_id=REPO,
            revision=REVISION,
            target_prefix="",
            include=(REMOTE_PATH,),
            files=(FileRule(REMOTE_PATH),),
        )
        digest = "b" * 64

        # The tree form of the same API names the content hash `oid`.
        tree_form = {"rfilename": REMOTE_PATH, "size": 10, "blobId": "c" * 40,
                      "lfs": {"oid": digest, "size": 10, "pointerSize": 132}}
        self.assertEqual(hub.HubClient._remote_file(repository, tree_form, None).sha256, digest)

        # A pointer whose content hash is missing or malformed verifies nothing;
        # its blob hash is never promoted to stand in for a content digest.
        shapeless = {"rfilename": REMOTE_PATH, "size": 10, "blobId": "c" * 40,
                     "lfs": {"sha256": "not-a-digest", "size": 10, "pointerSize": 132}}
        unverified = hub.HubClient._remote_file(repository, shapeless, None)
        self.assertIsNone(unverified.sha256)
        self.assertIsNone(unverified.git_sha1)

        # A pointer that disagrees with the manifest size is a bad manifest.
        disagreeing = {"rfilename": REMOTE_PATH, "size": 10, "blobId": "c" * 40,
                       "lfs": {"sha256": digest, "size": 11, "pointerSize": 132}}
        with self.assertRaises(HubError):
            hub.HubClient._remote_file(repository, disagreeing, None)

        # A plain file keeps its git blob hash, which does describe its content.
        plain = {"rfilename": REMOTE_PATH, "size": 10, "blobId": "c" * 40}
        self.assertEqual(hub.HubClient._remote_file(repository, plain, None).git_sha1, "c" * 40)

    def test_multi_gigabyte_checkpoints_carry_offline_pins(self) -> None:
        """The Hunyuan checkpoints are pinned in the catalog, so a digest
        disagreement is settled before a gigabyte is moved."""
        rules = {rule.path: rule for rule in CATALOG["hunyuan"].repositories[0].files}
        for path in (
            "hunyuan3d-dit-v2-1/model.fp16.ckpt",
            "hunyuan3d-vae-v2-1/model.fp16.ckpt",
        ):
            pinned = rules[path].sha256
            self.assertIsNotNone(pinned)
            self.assertEqual(len(pinned), 64)
            self.assertTrue(all(character in "0123456789abcdef" for character in pinned))
            self.assertNotEqual(pinned, rules[path].path)
