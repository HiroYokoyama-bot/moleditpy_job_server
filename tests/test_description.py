"""The PC description: an owner-set note shown with the server status.

It exists so that a person (or an AI agent) meeting this server can tell
what the PC is and what it is for before submitting anything to it.
"""

import base64
import json
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from job_server.api import create_app
from job_server.auth import signing_message
from job_server.settings import Settings, pc_description

DESCRIPTION = "GPU workstation in Lab 3 - free for ORCA jobs"


@pytest.fixture
def described(tmp_path):
    settings = Settings(
        data_dir=str(tmp_path), cpus=2, memory_mb=1024, description=DESCRIPTION
    )
    with TestClient(create_app(settings, dispatch=False)) as client:
        yield client


def _device_headers(client):
    """Enroll a device the way conftest.enroll does, for any test client."""
    app = client.app
    app.state.auth.create_user("alice", "example-password-123", "user")
    session = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "example-password-123"},
    )
    assert session.status_code == 200, session.text
    key = Ed25519PrivateKey.generate()
    device_id = str(uuid.uuid4())
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    response = client.post(
        "/api/v1/auth/devices",
        json={"device_id": device_id, "public_key": base64.b64encode(public).decode()},
        headers={"Authorization": "Bearer " + session.json()["token"]},
    )
    assert response.status_code == 201, response.text
    challenge = client.post("/api/v1/auth/challenge", json={"device_id": device_id}).json()
    signature = key.sign(signing_message(challenge["server_id"], device_id, challenge["nonce"]))
    response = client.post(
        "/api/v1/auth/token",
        json={
            "device_id": device_id,
            "nonce": challenge["nonce"],
            "signature": base64.b64encode(signature).decode(),
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["token"]}


def test_description_defaults_to_empty():
    assert Settings().description == ""


def test_description_survives_save_and_load(tmp_path):
    path = tmp_path / "settings.json"
    Settings(data_dir=str(tmp_path), description=DESCRIPTION).save(path)
    assert Settings.load(path).description == DESCRIPTION


def test_settings_written_before_the_field_load_with_no_description(tmp_path):
    path = tmp_path / "settings.json"
    Settings(data_dir=str(tmp_path)).save(path)
    saved = json.loads(path.read_text("utf-8"))
    saved.pop("description", None)
    path.write_text(json.dumps(saved), "utf-8")
    assert Settings.load(path).description == ""


@pytest.mark.parametrize(
    "value",
    [
        42,
        "x" * 501,
        "two\nlines",
        "tab\there",
        "bell\a",
    ],
)
def test_description_must_be_one_short_line_of_text(value):
    with pytest.raises(ValueError):
        Settings(description=value).validate()


def test_health_names_the_pc_and_carries_the_description(described):
    answer = described.get("/api/v1/health")
    assert answer.status_code == 200
    assert answer.json()["description"] == DESCRIPTION
    assert answer.json()["label"] == pc_description()


def test_status_answers_name_the_pc(described):
    """Several servers on one LAN must be tellable apart at a glance."""
    from socket import gethostname

    headers = _device_headers(described)
    for path, kwargs in (
        ("/api/v1/health", {}),
        ("/api/v1/capabilities", {"headers": headers}),
        ("/api/v1/system/resources", {"headers": headers}),
    ):
        answer = described.get(path, **kwargs)
        assert answer.status_code == 200, path
        assert answer.json()["hostname"] == gethostname(), path


def test_capabilities_do_not_carry_the_description(described):
    answer = described.get("/api/v1/capabilities", headers=_device_headers(described))
    assert answer.status_code == 200
    assert "description" not in answer.json()
    assert answer.json()["label"] == pc_description()


def test_system_resources_do_not_carry_the_description(described):
    answer = described.get("/api/v1/system/resources", headers=_device_headers(described))
    assert answer.status_code == 200
    assert "description" not in answer.json()
    assert answer.json()["label"] == pc_description()


def test_the_fixed_label_names_the_pc_and_its_os():
    from socket import gethostname

    assert gethostname() in pc_description()
    assert "job server running on" in pc_description()


def test_an_unset_description_still_leaves_the_label_naming_the_pc(client):
    answer = client.get("/api/v1/health").json()
    assert answer["description"] == ""
    assert answer["label"] == pc_description()
