import hashlib
import json
import os

import pytest

from job_server.db import Database
from job_server.files import disk_usage
from job_server.jobs import Jobs
from job_server.maintenance import Maintenance
from job_server.settings import Settings

from .conftest import reserve


def test_disk_usage_counts_regular_files_only(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"a" * 100)
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "b.bin").write_bytes(b"b" * 23)
    assert disk_usage(tmp_path) == 123
    assert disk_usage(tmp_path / "missing") == 0


@pytest.mark.skipif(os.name == "nt", reason="Symlinks need privileges on Windows")
def test_disk_usage_does_not_follow_links(tmp_path):
    outside = tmp_path.parent / "outside.bin"
    outside.write_bytes(b"x" * 1000)
    (tmp_path / "a.bin").write_bytes(b"a" * 10)
    (tmp_path / "link").symlink_to(outside)
    assert disk_usage(tmp_path) == 10


def test_pending_job_reports_its_uploaded_inputs(client, alice):
    data = b"example input data"
    files = [{"path": "input.inp", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]
    job_id = reserve(client, alice, files=files).json()["id"]
    base = f"/api/v1/jobs/{job_id}"
    client.put(base + "/files/input.inp", content=data, headers=alice["headers"])
    assert client.post(base + "/commit", headers=alice["headers"]).status_code == 200
    job = client.get(base, headers=alice["headers"]).json()
    assert job["state"] == "PENDING"
    assert job["disk_bytes"] == len(data)
    listed = client.get("/api/v1/jobs", headers=alice["headers"]).json()
    assert listed[0]["disk_bytes"] == len(data)


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
    return settings, db, jobs


def test_maintenance_measures_a_finished_job_once(tmp_path):
    settings, db, jobs = _stack(tmp_path, "DONE", ended_at=2.0)
    folder = jobs.directory("job1")
    (folder / "result.out").write_bytes(b"r" * 40)
    (folder / "scratch.tmp").write_bytes(b"s" * 2)
    row = db.one("SELECT * FROM jobs WHERE id='job1'")
    # Not measured yet: the job list falls back to the uploaded inputs (none).
    assert jobs.public(row)["disk_bytes"] == 0
    Maintenance(db, jobs, settings).measure_finished()
    row = db.one("SELECT * FROM jobs WHERE id='job1'")
    assert row["disk_bytes"] == 42
    assert jobs.public(row)["disk_bytes"] == 42
    # The stored total is kept even though the folder keeps changing hands:
    # measuring again does not replace it.
    (jobs.directory("job1") / "late.bin").write_bytes(b"l" * 100)
    Maintenance(db, jobs, settings).measure_finished()
    assert db.one("SELECT * FROM jobs WHERE id='job1'")["disk_bytes"] == 42


def test_running_job_disk_usage_tracks_growth_at_most_once_a_minute(tmp_path):
    settings, db, jobs = _stack(tmp_path, "RUNNING")
    (jobs.directory("job1") / "input.inp").write_bytes(b"i" * 10)
    maintenance = Maintenance(db, jobs, settings)
    maintenance.measure_running()
    row = db.one("SELECT * FROM jobs WHERE id='job1'")
    assert row["disk_bytes"] == 10
    assert jobs.public(row)["disk_bytes"] == 10
    # Output grows, but a pass inside the minute does not re-measure.
    (jobs.directory("job1") / "output.out").write_bytes(b"o" * 50)
    maintenance.measure_running()
    assert db.one("SELECT * FROM jobs WHERE id='job1'")["disk_bytes"] == 10
    # Once the minute has passed, the next pass picks the growth up.
    with db.transaction() as c:
        c.execute("UPDATE jobs SET disk_measured_at=0 WHERE id='job1'")
    maintenance.measure_running()
    row = db.one("SELECT * FROM jobs WHERE id='job1'")
    assert row["disk_bytes"] == 60
    assert jobs.public(row)["disk_bytes"] == 60


def test_a_job_measured_while_running_is_measured_once_more_after_it_ends(tmp_path):
    settings, db, jobs = _stack(tmp_path, "RUNNING")
    (jobs.directory("job1") / "input.inp").write_bytes(b"i" * 10)
    maintenance = Maintenance(db, jobs, settings)
    maintenance.measure_running()
    assert db.one("SELECT * FROM jobs WHERE id='job1'")["disk_bytes"] == 10
    # The job ends and its last output lands after the running measurement.
    (jobs.directory("job1") / "final.out").write_bytes(b"f" * 5)
    measured_at = db.one("SELECT disk_measured_at FROM jobs WHERE id='job1'")["disk_measured_at"]
    with db.transaction() as c:
        c.execute(
            "UPDATE jobs SET state='DONE',ended_at=? WHERE id='job1'",
            (measured_at + 1,),
        )
    maintenance.measure_finished()
    row = db.one("SELECT * FROM jobs WHERE id='job1'")
    assert row["disk_bytes"] == 15
    # And from then on the total never changes again.
    (jobs.directory("job1") / "late.bin").write_bytes(b"l" * 100)
    maintenance.measure_finished()
    assert db.one("SELECT * FROM jobs WHERE id='job1'")["disk_bytes"] == 15
