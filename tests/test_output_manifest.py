"""A job's result list: computed once with digests, then served from storage."""

import hashlib
import json
import sys
import time

import pytest

from job_server.db import Database
from job_server.files import parse_manifest
from job_server.jobs import Jobs
from job_server.maintenance import Maintenance
from job_server.settings import Settings

from .conftest import reserve

DIGEST = hashlib.sha256(b"abc").hexdigest()
PRINCIPAL = {"user_id": "u1", "role": "user"}


def _stack(tmp_path, state, ended_at=None):
    settings = Settings(data_dir=str(tmp_path / "data"))
    db = Database(settings.root)
    jobs = Jobs(db, settings)
    with db.transaction() as c:
        c.execute(
            "INSERT INTO users (id,username,password_hash,role,created_at) "
            "VALUES ('u1','alice','x','user',1.0)"
        )
        c.execute(
            "INSERT INTO jobs (id,owner_id,spec,state,created_at,started_at,ended_at,work_root) "
            "VALUES ('job1','u1',?,?,?,?,?,?)",
            (
                json.dumps({"name": "job", "after_job": None}), state, 1.0, 2.0,
                ended_at, str(settings.job_root),
            ),
        )
    folder = jobs.directory("job1")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.txt").write_bytes(b"abc")
    return settings, db, jobs


def test_a_running_jobs_first_list_is_recorded_with_digests(tmp_path):
    _settings, db, jobs = _stack(tmp_path, "RUNNING")
    expected = [{"path": "result.txt", "size": 3, "sha256": DIGEST}]
    assert jobs.outputs("job1", PRINCIPAL) == expected
    assert db.one("SELECT manifest FROM jobs WHERE id='job1'")["manifest"] is not None


def test_a_running_jobs_list_tracks_new_output(tmp_path):
    _settings, _db, jobs = _stack(tmp_path, "RUNNING")
    assert jobs.outputs("job1", PRINCIPAL) == [
        {"path": "result.txt", "size": 3, "sha256": DIGEST}
    ]
    folder = jobs.directory("job1")
    (folder / "result.txt").write_bytes(b"abc plus more")
    (folder / "second.txt").write_bytes(b"xy")
    entries = jobs.outputs("job1", PRINCIPAL)
    assert entries == [
        {
            "path": "result.txt", "size": 13,
            "sha256": hashlib.sha256(b"abc plus more").hexdigest(),
        },
        {
            "path": "second.txt", "size": 2,
            "sha256": hashlib.sha256(b"xy").hexdigest(),
        },
    ]


def test_maintenance_stores_a_finished_jobs_result_list_once(tmp_path):
    settings, db, jobs = _stack(tmp_path, "DONE", ended_at=2.0)
    Maintenance(db, jobs, settings).store_manifests()
    expected = [{"path": "result.txt", "size": 3, "sha256": DIGEST}]
    stored = json.loads(db.one("SELECT manifest FROM jobs WHERE id='job1'")["manifest"])
    assert stored["entries"] == expected
    assert jobs.outputs("job1", PRINCIPAL) == expected
    # A folder that no longer matches the stored stamp is listed afresh.
    (jobs.directory("job1") / "result.txt").write_bytes(b"changed!")
    refreshed = jobs.outputs("job1", PRINCIPAL)
    assert refreshed == [{
        "path": "result.txt", "size": 8,
        "sha256": hashlib.sha256(b"changed!").hexdigest(),
    }]


def test_a_queued_jobs_result_list_is_computed_once_and_stored(tmp_path):
    _settings, db, jobs = _stack(tmp_path, "PENDING")
    expected = [{"path": "result.txt", "size": 3, "sha256": DIGEST}]
    assert jobs.outputs("job1", PRINCIPAL) == expected
    assert db.one("SELECT manifest FROM jobs WHERE id='job1'")["manifest"] is not None
    assert jobs.outputs("job1", PRINCIPAL) == expected


def test_the_listing_can_be_read_without_digests(client, alice):
    job_id = reserve(client, alice).json()["id"]
    client.post(f"/api/v1/jobs/{job_id}/commit", headers=alice["headers"])
    work = client.app.state.jobs.directory(job_id)
    (work / "result.txt").write_bytes(b"abc")
    entries = client.get(
        f"/api/v1/jobs/{job_id}/files?digest=0", headers=alice["headers"]
    ).json()
    assert entries == [{"path": "result.txt", "size": 3}]
    row = client.app.state.db.one("SELECT manifest FROM jobs WHERE id=?", (job_id,))
    assert row["manifest"] is None, "Reading without digests records nothing"
    entries = client.get(f"/api/v1/jobs/{job_id}/files", headers=alice["headers"]).json()
    assert entries == [{"path": "result.txt", "size": 3, "sha256": DIGEST}]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows stat has no change time: its ctime is the creation time",
)
def test_a_rewrite_that_restores_the_mtime_still_gets_a_new_digest(client, alice):
    import os

    job_id = reserve(client, alice).json()["id"]
    client.post(f"/api/v1/jobs/{job_id}/commit", headers=alice["headers"])
    work = client.app.state.jobs.directory(job_id)
    target = work / "result.txt"
    target.write_bytes(b"abc")
    entries = client.get(f"/api/v1/jobs/{job_id}/files", headers=alice["headers"]).json()
    assert entries[0]["sha256"] == DIGEST
    before = target.stat()
    target.write_bytes(b"xyz")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    entries = client.get(f"/api/v1/jobs/{job_id}/files", headers=alice["headers"]).json()
    assert entries[0]["sha256"] == hashlib.sha256(b"xyz").hexdigest()


def test_a_replace_that_restores_the_mtime_still_gets_a_new_digest(client, alice):
    import os

    job_id = reserve(client, alice).json()["id"]
    client.post(f"/api/v1/jobs/{job_id}/commit", headers=alice["headers"])
    work = client.app.state.jobs.directory(job_id)
    target = work / "result.txt"
    target.write_bytes(b"abc")
    entries = client.get(f"/api/v1/jobs/{job_id}/files", headers=alice["headers"]).json()
    assert entries[0]["sha256"] == DIGEST
    before = target.stat()
    staged = work / "staged.txt"
    staged.write_bytes(b"xyz")
    os.utime(staged, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(staged, target)
    entries = client.get(f"/api/v1/jobs/{job_id}/files", headers=alice["headers"]).json()
    assert entries[0]["sha256"] == hashlib.sha256(b"xyz").hexdigest()


def test_a_stored_manifest_is_not_part_of_the_public_job(client, alice):
    job_id = reserve(client, alice).json()["id"]
    client.post(f"/api/v1/jobs/{job_id}/commit", headers=alice["headers"])
    work = client.app.state.jobs.directory(job_id)
    (work / "result.txt").write_bytes(b"abc")
    client.get(f"/api/v1/jobs/{job_id}/files", headers=alice["headers"])
    shown = client.get(f"/api/v1/jobs/{job_id}", headers=alice["headers"]).json()
    assert "manifest" not in shown
    listed = client.get("/api/v1/jobs", headers=alice["headers"]).json()
    assert all("manifest" not in job for job in listed)


def test_an_unreadable_stored_manifest_is_recomputed(client, alice):
    job_id = reserve(client, alice).json()["id"]
    client.post(f"/api/v1/jobs/{job_id}/commit", headers=alice["headers"])
    work = client.app.state.jobs.directory(job_id)
    (work / "result.txt").write_bytes(b"abc")
    db = client.app.state.db
    with db.transaction() as c:
        c.execute("UPDATE jobs SET manifest='not json' WHERE id=?", (job_id,))
    entries = client.get(f"/api/v1/jobs/{job_id}/files", headers=alice["headers"]).json()
    assert entries == [{"path": "result.txt", "size": 3, "sha256": DIGEST}]
    row = db.one("SELECT manifest FROM jobs WHERE id=?", (job_id,))
    assert parse_manifest(row["manifest"]) is not None


def test_a_second_listing_reads_no_file_again(client, alice, monkeypatch):
    from job_server import files

    job_id = reserve(client, alice).json()["id"]
    client.post(f"/api/v1/jobs/{job_id}/commit", headers=alice["headers"])
    work = client.app.state.jobs.directory(job_id)
    (work / "a.txt").write_bytes(b"abc")
    reads = []
    real = files.sha256

    def counting(path, stop=None):
        reads.append(path)
        return real(path, stop)

    monkeypatch.setattr(files, "sha256", counting)
    client.get(f"/api/v1/jobs/{job_id}/files", headers=alice["headers"])
    assert len(reads) == 1
    client.get(f"/api/v1/jobs/{job_id}/files", headers=alice["headers"])
    assert len(reads) == 1, "The stored record is served without reading again"
    (work / "b.txt").write_bytes(b"abc")
    client.get(f"/api/v1/jobs/{job_id}/files", headers=alice["headers"])
    assert len(reads) == 2, "Only the added file is read"


def test_a_recorded_end_hashes_nothing_in_the_dispatcher(client, alice, monkeypatch):
    job_id = reserve(client, alice).json()["id"]
    client.post(f"/api/v1/jobs/{job_id}/commit", headers=alice["headers"])
    db = client.app.state.db
    with db.transaction() as c:
        c.execute(
            "UPDATE jobs SET state='RUNNING',started_at=?,pid=4242,process_started=1.0 WHERE id=?",
            (time.time(), job_id),
        )
    work = client.app.state.jobs.directory(job_id)
    (work / "result.txt").write_bytes(b"abc")
    # A list recorded while the job ran is not the final one.
    client.get(f"/api/v1/jobs/{job_id}/files", headers=alice["headers"])
    assert db.one("SELECT manifest FROM jobs WHERE id=?", (job_id,))["manifest"]
    (work / "result.txt").write_bytes(b"abc, final")
    control = client.app.state.settings.root / "control" / job_id
    control.mkdir(parents=True, exist_ok=True)
    (control / "done.json").write_text(json.dumps({
        "state": "DONE", "reason": "", "exit_code": 0, "ended_at": time.time(),
    }))
    import job_server.files as files

    def no_hashing(*_args, **_kwargs):
        raise AssertionError("The dispatcher must not hash job output")

    monkeypatch.setattr(files, "sha256", no_hashing)
    client.app.state.scheduler.reconcile()
    row = db.one("SELECT state,manifest FROM jobs WHERE id=?", (job_id,))
    assert row["state"] == "DONE"
    assert row["manifest"] is None, "The running list is cleared for maintenance"
    monkeypatch.undo()
    scheduler = client.app.state.scheduler
    scheduler.maintenance.store_manifests()
    entries = client.get(f"/api/v1/jobs/{job_id}/files", headers=alice["headers"]).json()
    assert entries == [{
        "path": "result.txt", "size": 10,
        "sha256": hashlib.sha256(b"abc, final").hexdigest(),
    }]


def test_a_stopping_server_abandons_a_result_list_mid_file(tmp_path):
    settings, db, jobs = _stack(tmp_path, "DONE", ended_at=2.0)
    (jobs.directory("job1") / "big.bin").write_bytes(b"x" * (3 * 1024 * 1024))
    maintenance = Maintenance(db, jobs, settings)
    reads = []

    def stop():
        reads.append(1)
        return len(reads) > 1  # The stop arrives after the first 1 MiB read.

    maintenance.stop_event.is_set = stop
    maintenance.store_manifests()
    assert db.one("SELECT manifest FROM jobs WHERE id='job1'")["manifest"] is None


def test_result_lists_are_stored_after_moves_and_backups(tmp_path):
    settings, db, jobs = _stack(tmp_path, "DONE", ended_at=2.0)
    maintenance = Maintenance(db, jobs, settings)
    order = []
    for name in ("measure_running", "measure_finished", "relocate_finished",
                 "backup_if_due", "archive_events", "store_manifests"):
        setattr(maintenance, name, lambda name=name: order.append(name))
    maintenance.run_once()
    assert order[-1] == "store_manifests"
