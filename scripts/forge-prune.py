#!/usr/bin/env python3
"""Offline Forge retention. Stop the API and all workers before --apply.

Stdlib only; dry runs do not create locks, temporary files, or directories.
The API's in-process lock cannot coordinate with this maintenance program.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

SPIKE = "/home/alexk/debt-city-greybox-spike/apps/greybox/assets"
TERMINAL = {"ready", "failed"}
STATES = TERMINAL | {"uploaded", "matching", "review", "staged", "queued_bake", "baking"}
SUMMARY_KEYS = ("number", "asset", "variant", "origin", "job_id", "created_at",
                "accepted", "metrics", "notes", "lineage")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Store timestamps must include a timezone")
    return result


def inventory(root):
    """Fail before mutation on aliases or special files, including dangling links."""
    entries = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError(f"Refusing symlink or special file: {path}")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError(f"Refusing hardlink: {path}")
            entries[path.relative_to(root)] = (
                info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return entries


def atomic_write(path, data):
    # Unique O_EXCL temporary file; never open a fixed .tmp alias.
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix=".forge-prune-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def retained_log(path, max_bytes, max_lines):
    """Bounded memory tail; discard partial leading lines and UTF-8 characters."""
    with path.open("rb") as source:
        size = source.seek(0, os.SEEK_END)
        start = max(0, size - max_bytes)
        source.seek(start)
        data = source.read(max_bytes)
    if start:
        data = data.partition(b"\n")[2]
    return b"".join(data.splitlines(keepends=True)[-max_lines:])


def prune(root, *, keep_versions=10, max_age_days=30, apply=False,
          max_log_bytes=1024 * 1024, log_lines=200, now=None, report=print):
    if (type(keep_versions) is not int or keep_versions < 0
            or not math.isfinite(max_age_days) or max_age_days < 0
            or max_log_bytes < 1 or log_lines < 1):
        raise ValueError("Retention limits must be nonnegative; log limits must be positive")
    root = Path(root)
    if not root.is_absolute():
        raise ValueError("Store root must be absolute")
    root = root.resolve(strict=True)
    spike = Path(os.getenv("FORGE_SPIKE_ASSETS", SPIKE)).resolve()
    if root.is_relative_to(spike) or spike.is_relative_to(root):
        raise ValueError("Forge store and spike tree must be disjoint")
    if not root.is_dir():
        raise ValueError("Store root must be a directory")
    before = inventory(root)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    jobs, versions = {}, {}
    for path in sorted(root.glob("assets/*/variants/*/jobs/*/job.json")):
        job = read_json(path)
        asset, variant, job_id = path.parts[-6], path.parts[-4], path.parts[-2]
        if ((job["asset"], job["variant"], job["id"]) != (asset, variant, job_id)
                or not re.fullmatch(r"[a-f0-9]{32}", job_id) or job_id in jobs
                or job["state"] not in STATES or type(job["accepted"]) is not bool):
            raise ValueError(f"Invalid job record: {path}")
        timestamp(job["updated_at"])
        timestamp(job["created_at"])
        jobs[job_id] = (path.parent, job)
    for path in sorted(root.glob("assets/*/variants/*/versions/v*/version.json")):
        version = read_json(path)
        asset, variant, dirname = path.parts[-6], path.parts[-4], path.parts[-2]
        number = version["number"]
        if ((version["asset"], version["variant"]) != (asset, variant)
                or type(number) is not int or number < 1 or dirname != f"v{number}"
                or type(version["accepted"]) is not bool):
            raise ValueError(f"Invalid version record: {path}")
        # Validate summary fields before any deletion or index replacement.
        for key in SUMMARY_KEYS:
            version[key]
        timestamp(version["created_at"])
        versions[(asset, variant, number)] = (path.parent, version)

    protected = {}
    pairs = sorted({key[:2] for key in versions})
    for pair in pairs:
        keys = sorted((key for key in versions if key[:2] == pair), reverse=True)
        for rank, key in enumerate(keys):
            version = versions[key][1]
            reasons = []
            if version["accepted"]:
                reasons.append("ACCEPTED")
            if rank == 0:
                reasons.append("newest per variant")
            if rank < keep_versions:
                reasons.append(f"keep newest {keep_versions}")
            owner = jobs.get(version["job_id"])
            if owner and (owner[1]["state"] not in TERMINAL or owner[1].get("lease")):
                reasons.append("active owning job")
            if owner and owner[1]["accepted"]:
                reasons.append("ACCEPTED owning job")
            if version.get("critic_lease"):
                reasons.append("critic lease")
            if reasons:
                protected[key] = reasons
    # Keep input parents of in-progress iterations and lineage of retained versions.
    for _, job in jobs.values():
        if job["state"] not in TERMINAL and job.get("parent_version") is not None:
            key = (job["asset"], job["variant"], job["parent_version"])
            if key in versions:
                protected.setdefault(key, []).append("active iteration parent")
    pending = list(protected)
    while pending:
        key = pending.pop()
        lineage = versions[key][1]["lineage"]
        for number in (lineage.get("parent_version"), lineage.get("root_version")):
            parent = (*key[:2], number)
            if parent in versions and parent not in protected:
                protected[parent] = ["retained lineage"]
                pending.append(parent)

    deletions, trims = [], []
    removed_keys = set(versions) - set(protected)
    for key, (path, _) in versions.items():
        if key in protected:
            report(f"SKIP {path}: {', '.join(protected[key])}")
        else:
            deletions.append(path)
    retained_jobs = {versions[key][1]["job_id"] for key in protected}
    # Preserve parent staged inputs even when the child itself is old/eligible.
    retained_jobs.update(job["parent_job"] for _, job in jobs.values() if job.get("parent_job"))
    for job_id, (path, job) in jobs.items():
        reasons = []
        active = job["state"] not in TERMINAL or bool(job.get("lease"))
        if active:
            reasons.append("active state or lease")
        if job["accepted"]:
            reasons.append("ACCEPTED")
        if job_id in retained_jobs:
            reasons.append("retained version or child references job")
        if max(timestamp(job["updated_at"]), timestamp(job["created_at"])) >= cutoff:
            reasons.append("younger than age limit")
        snapshot = path / "views_backup/snapshot.json"
        recovery = snapshot.exists() and read_json(snapshot).get("restored") is not True
        if recovery:
            reasons.append("unrestored snapshot")
        if reasons:
            report(f"SKIP {path}: {', '.join(reasons)}")
            log = path / "worker.log"
            if not active and not recovery and log.exists():
                tail = retained_log(log, max_log_bytes, log_lines)
                if len(tail) < log.stat().st_size:
                    trims.append((log, tail))
        else:
            deletions.append(path)

    # Prepare all derived indexes before mutation; dry-run leaves even stale indexes alone.
    indexes = []
    for pair in pairs:
        if not any(key[:2] == pair for key in removed_keys):
            continue
        summaries = []
        for key in sorted(protected):
            if key[:2] == pair:
                version = versions[key][1]
                summaries.append({field: version[field] for field in SUMMARY_KEYS} | {
                    "state": "ready", "artifacts": version.get("artifacts", []),
                    "critic": version.get("critic", {"status": "pending"}),
                    "style": version.get("style", version.get("metrics", {}).get("style")),
                    "attention": version.get("attention"),
                    "policy": {field: version.get("policy", {})[field] for field in ("mode", "action")
                               if field in version.get("policy", {})}} |
                    ({"set_id": version["set_id"]} if version.get("set_id") is not None else {}))
        path = root / "assets" / pair[0] / "variants" / pair[1] / "versions.json"
        indexes.append((path, json.dumps(summaries, indent=2, allow_nan=False).encode()))
    verb = "DELETE" if apply else "WOULD DELETE"
    for path in deletions:
        report(f"{verb} {path}")
    for path, data in trims:
        report(f"{'TRIM' if apply else 'WOULD TRIM'} {path}: keep {len(data)} bytes")
    for path, _ in indexes:
        report(f"{'REINDEX' if apply else 'WOULD REINDEX'} {path}")
    if apply:
        # Detect changes while planning. This is not a substitute for stopped writers.
        if inventory(root) != before:
            raise ValueError("Store changed during planning; stop API and workers before --apply")
        for path in deletions:
            shutil.rmtree(path)
        for path, data in indexes + trims:
            atomic_write(path, data)
    report(f"{'APPLIED' if apply else 'DRY RUN'}: versions={len(removed_keys)} "
           f"jobs={len(deletions) - len(removed_keys)} logs={len(trims)}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-root", default=os.getenv("FORGE_STORE_ROOT"),
                        help="Absolute host Forge root (or FORGE_STORE_ROOT)")
    parser.add_argument("--keep-versions", type=int, default=10)
    parser.add_argument("--max-age-days", type=float, default=30)
    parser.add_argument("--max-log-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--log-lines", type=int, default=200)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false")
    mode.add_argument("--apply", action="store_true", help="Delete eligible data; requires stopped writers")
    parser.set_defaults(apply=False)
    args = parser.parse_args(argv)
    if not args.store_root:
        parser.error("--store-root or FORGE_STORE_ROOT is required")
    try:
        prune(args.store_root, keep_versions=args.keep_versions, max_age_days=args.max_age_days,
              apply=args.apply, max_log_bytes=args.max_log_bytes, log_lines=args.log_lines)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
