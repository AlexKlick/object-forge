from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_sprite_pipeline.forge_store import ForgeStore

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/forge-prune.py"
spec = importlib.util.spec_from_file_location("forge_prune", SCRIPT)
pruner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pruner)


class ForgePruneTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="forge-p6-prune-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "store"
        self.store = ForgeStore(self.root)
        self.now = datetime(2026, 9, 4, tzinfo=timezone.utc)
        self.old = self.now - timedelta(days=40)
        self.messages = []

    def job(self, state="failed", *, old=True, asset="a", variant="v"):
        with patch.object(self.store, "_now", return_value=self.old if old else self.now):
            job = self.store.create_job(asset, variant)
            job["state"] = state
            return self.store._save_job(job)

    def version(self, *, accepted=False, asset="a", variant="v"):
        job = self.job("baking", asset=asset, variant=variant)
        with patch.object(self.store, "_now", return_value=self.old):
            version = self.store.complete(job["id"], artifacts={"atlas.png": "artifact"})
            if accepted:
                self.store.accept_version(asset, variant, version["number"])
        return self.store.get_version(asset, variant, version["number"])

    def run_prune(self, **kwargs):
        pruner.prune(self.root, now=self.now, report=self.messages.append, **kwargs)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix():
                (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns)
                for p in self.root.rglob("*")}

    def test_accepted_and_newest_per_variant_never_pruned_even_keep_zero(self):
        self.version(accepted=True)
        self.version()
        self.version()
        self.version(asset="other")
        self.run_prune(keep_versions=0, apply=True)
        self.assertEqual([v["number"] for v in self.store.list_versions("a", "v")], [1, 3])
        self.assertEqual(len(self.store.list_versions("other", "v")), 1)
        self.assertTrue(any("ACCEPTED" in line for line in self.messages))
        self.assertTrue(any("newest per variant" in line for line in self.messages))

    def test_default_dry_run_touches_no_bytes_paths_or_mtimes(self):
        for _ in range(12):
            self.version()
        job = self.job("ready", old=False)
        (self.store._job_path(job["id"]).parent / "worker.log").write_bytes(b"marker\n" * 400)
        before = self.snapshot()
        self.run_prune()
        self.assertEqual(before, self.snapshot())
        self.assertTrue(any("WOULD DELETE" in line for line in self.messages))
        self.assertTrue(any("WOULD TRIM" in line for line in self.messages))
        self.assertIn("versions=2", self.messages[-1])

    def test_apply_removes_only_eligible_versions_jobs_and_rebuilds_index(self):
        versions = [self.version() for _ in range(4)]
        failed = self.job()
        recent = self.job(old=False)
        self.run_prune(keep_versions=2, apply=True)
        index = json.loads((self.root / "assets/a/variants/v/versions.json").read_bytes())
        self.assertEqual([v["number"] for v in index], [3, 4])
        self.assertEqual(self.store.list_versions("a", "v"), index)
        ids = {job["id"] for job in self.store.list_jobs()}
        self.assertEqual(ids, {versions[2]["job_id"], versions[3]["job_id"], recent["id"]})
        self.assertNotIn(failed["id"], ids)
        # Keeping newest prevents the allocator from reusing a pruned version number.
        self.assertEqual(self.version()["number"], 5)

    def test_all_active_states_preserved_with_logs_even_if_old_or_lease_expired(self):
        for state in sorted(pruner.STATES - pruner.TERMINAL):
            job = self.job(state)
            (self.store._job_path(job["id"]).parent / "worker.log").write_bytes(b"active\n" * 500)
        before = self.snapshot()
        self.run_prune(apply=True, max_age_days=0, max_log_bytes=30)
        self.assertEqual(before, self.snapshot())

    def test_accepted_jobs_and_jobs_with_leases_preserved(self):
        for field, value in (("accepted", True), ("lease", {"lease_id": "still-held"})):
            job = self.job()
            job[field] = value
            self.store._save_job(job)
        self.run_prune(apply=True, max_age_days=0)
        self.assertEqual(len(self.store.list_jobs()), 2)

    def test_age_uses_update_and_creation_and_boundary_is_kept(self):
        recent = self.job(old=False)
        boundary = self.job()
        path = self.store._job_path(boundary["id"])
        boundary["updated_at"] = (self.now - timedelta(days=30)).isoformat()
        path.write_text(json.dumps(boundary))
        self.job()
        self.run_prune(apply=True)
        self.assertEqual({j["id"] for j in self.store.list_jobs()}, {recent["id"], boundary["id"]})

    def test_log_tail_bounds_bytes_and_lines_and_keeps_newest_utf8(self):
        job = self.job("ready", old=False)
        path = self.store._job_path(job["id"]).parent / "worker.log"
        path.write_text("".join(f"marker {i} café\n" for i in range(100)))
        self.run_prune(apply=True, max_log_bytes=100, log_lines=3)
        self.assertLessEqual(path.stat().st_size, 100)
        self.assertEqual(path.read_text().splitlines(), [f"marker {i} café" for i in (97, 98, 99)])

    def test_single_oversized_line_is_dropped(self):
        job = self.job("ready", old=False)
        path = self.store._job_path(job["id"]).parent / "worker.log"
        path.write_bytes(b"x" * 1000)
        self.run_prune(apply=True, max_log_bytes=10)
        self.assertEqual(path.read_bytes(), b"")

    def test_unrestored_snapshot_keeps_failed_job_and_logs(self):
        job = self.job()
        path = self.store._job_path(job["id"]).parent
        (path / "views_backup").mkdir()
        (path / "views_backup/snapshot.json").write_text('{"restored": false}')
        (path / "worker.log").write_bytes(b"recovery\n" * 500)
        before = self.snapshot()
        self.run_prune(apply=True, max_log_bytes=10)
        self.assertEqual(before, self.snapshot())

    def test_symlink_and_hardlink_refused_before_any_deletion(self):
        job = self.job()
        outside = self.root.parent / "outside"
        outside.write_bytes(b"protected")
        link = self.root / "alias"
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    link.symlink_to(outside)
                else:
                    os.link(outside, link)
                with self.assertRaisesRegex(ValueError, "link"):
                    self.run_prune(apply=True)
                self.assertEqual(outside.read_bytes(), b"protected")
                self.assertTrue(self.store._job_path(job["id"]).exists())
                link.unlink()

    def test_invalid_record_refused_before_eligible_data_deleted(self):
        job = self.job()
        bad = self.job()
        path = self.store._job_path(bad["id"])
        path.write_text('{"state": "failed"}')
        before = self.snapshot()
        with self.assertRaises(KeyError):
            self.run_prune(apply=True)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(self.store._job_path(job["id"]).exists())

    def test_keep_lineage_active_iteration_inputs_and_critic_leases(self):
        versions = [self.version() for _ in range(5)]
        latest_path = self.store._version_dir("a", "v", 5) / "version.json"
        latest = versions[-1]
        latest["lineage"] = {"parent_version": 1, "root_version": 1}
        latest_path.write_text(json.dumps(latest))
        critic_path = self.store._version_dir("a", "v", 2) / "version.json"
        critic = versions[1]
        critic["critic_lease"] = {"lease_id": "pending"}
        critic_path.write_text(json.dumps(critic))
        job = self.job("review")
        job.update(parent_version=3, parent_job=versions[2]["job_id"])
        self.store._save_job(job)
        self.run_prune(apply=True, keep_versions=0)
        self.assertEqual([v["number"] for v in self.store.list_versions()], [1, 2, 3, 5])
        for number in (1, 2, 3, 5):
            self.assertTrue(self.store.get_job(versions[number - 1]["job_id"]))

    def test_limits_and_spike_overlap_refused(self):
        for options in ({"keep_versions": -1}, {"max_age_days": -1},
                        {"max_age_days": float("nan")}, {"max_log_bytes": 0}, {"log_lines": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_prune(**options)
        with patch.dict(os.environ, {"FORGE_SPIKE_ASSETS": str(self.root)}):
            with self.assertRaisesRegex(ValueError, "disjoint"):
                self.run_prune(apply=True)

    def test_cli_defaults_dry_run_then_apply(self):
        job = self.job()
        before = self.snapshot()
        environment = {**os.environ, "FORGE_STORE_ROOT": str(self.root)}
        result = subprocess.run([sys.executable, str(SCRIPT)], env=environment,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY RUN:", result.stdout)
        self.assertEqual(before, self.snapshot())
        result = subprocess.run([sys.executable, str(SCRIPT), "--apply"], env=environment,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("APPLIED:", result.stdout)
        with self.assertRaises(FileNotFoundError):
            self.store.get_job(job["id"])


if __name__ == "__main__":
    unittest.main()
