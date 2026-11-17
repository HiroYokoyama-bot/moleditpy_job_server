"""Downloads are verified against the X-SHA256 header of the download itself."""

import hashlib
import io
import uuid

import pytest

from job_server.client import JobServerClient

JOB = str(uuid.uuid4())


class Response(io.BytesIO):
    def __init__(self, data, headers, status=200):
        super().__init__(data)
        self.headers, self.status = headers, status

    def __exit__(self, *exc):
        self.close()
        return False


def client_for(content, *, digest=None, listed_size=None, status=200, content_range=None):
    client = JobServerClient("http://127.0.0.1:8766")
    client.token = "token"
    client._ensure_auth = lambda route: None
    listings, ranges = [], []

    def request(method, path, *args, **kwargs):
        listings.append(path)
        size = len(content) if listed_size is None else listed_size
        return [{"path": "result.out", "size": size}]

    def open_(request):
        wanted = request.get_header("Range")
        ranges.append(wanted)
        start = int(wanted.split("=")[1].rstrip("-")) if wanted else 0
        headers = {"X-SHA256": hashlib.sha256(content).hexdigest() if digest is None else digest}
        if wanted:
            headers["Content-Range"] = content_range or f"bytes {start}-{len(content) - 1}/{len(content)}"
        return Response(content[start:], headers, 206 if wanted else status)

    client.request, client._open = request, open_
    return client, listings, ranges


def test_the_listing_is_asked_for_without_digests(tmp_path):
    client, listings, _ = client_for(b"result data")
    target = client.download(JOB, "result.out", tmp_path / "out" / "result.out")
    assert target.read_bytes() == b"result data"
    assert listings == [f"/jobs/{JOB}/files?digest=0"]
    assert not (tmp_path / "out" / "result.out.partial").exists()


def test_a_body_that_does_not_match_its_header_is_discarded(tmp_path):
    client, _, _ = client_for(b"result data", digest="0" * 64)
    with pytest.raises(ValueError, match="checksum differs"):
        client.download(JOB, "result.out", tmp_path / "result.out")
    assert not (tmp_path / "result.out").exists()
    assert not (tmp_path / "result.out.partial").exists()


@pytest.mark.parametrize("digest", ["", "not-a-digest"])
def test_a_download_without_a_checksum_is_refused(tmp_path, digest):
    client, _, _ = client_for(b"result data", digest=digest)
    with pytest.raises(ValueError, match="no file checksum"):
        client.download(JOB, "result.out", tmp_path / "result.out")
    assert not (tmp_path / "result.out").exists()


def test_a_partial_download_resumes_and_is_verified_whole(tmp_path):
    content = b"0123456789" * 10
    (tmp_path / "result.out.partial").write_bytes(content[:40])
    client, _, ranges = client_for(content)
    client.download(JOB, "result.out", tmp_path / "result.out")
    assert ranges == ["bytes=40-"]
    assert (tmp_path / "result.out").read_bytes() == content


def test_a_full_size_leftover_is_fetched_again(tmp_path):
    # A leftover of the right size has nothing to be verified against
    # until a response brings the checksum, so it is not trusted as is.
    (tmp_path / "result.out.partial").write_bytes(b"stale data!")
    client, _, ranges = client_for(b"fresh data!")
    client.download(JOB, "result.out", tmp_path / "result.out")
    assert ranges == [None]
    assert (tmp_path / "result.out").read_bytes() == b"fresh data!"


def test_a_file_that_grew_after_listing_is_refused(tmp_path):
    client, _, _ = client_for(b"longer than listed", listed_size=4)
    with pytest.raises(ValueError, match="exceeded expected file size"):
        client.download(JOB, "result.out", tmp_path / "result.out")
    assert not (tmp_path / "result.out").exists()
