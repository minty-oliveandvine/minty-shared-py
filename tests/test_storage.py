"""minty_shared.storage: a file reaches the bucket byte for byte (owner, 2026-11-04)."""

import pytest

from minty_shared import storage


class FakeClient:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = []

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):  # noqa: N803 - boto3's name
        self.calls.append((fileobj.read(), bucket, key, ExtraArgs))
        if self.failures:
            self.failures -= 1
            raise ConnectionError("network down")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(storage.time, "sleep", slept.append)
    return slept


def test_the_bytes_and_the_content_type_are_stored_unchanged():
    data = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8192  # a 2 MB "PNG"
    client = FakeClient()
    assert storage.put_file(client, "b", "k.png", data, content_type="image/png") == len(data)
    assert client.calls == [(data, "b", "k.png", {"ContentType": "image/png"})]


def test_a_failure_is_retried_twice_with_backoff(no_sleep):
    client = FakeClient(failures=2)
    storage.put_file(client, "b", "k", b"x", content_type="application/pdf")
    assert len(client.calls) == 3
    assert no_sleep == [1, 2]


def test_a_third_failure_raises_with_the_cause_chained():
    client = FakeClient(failures=3)
    with pytest.raises(storage.StorageError) as caught:
        storage.put_file(client, "b", "k", b"x", content_type="image/jpeg")
    assert isinstance(caught.value.__cause__, ConnectionError)
    assert len(client.calls) == 3


def test_no_bucket_is_refused_before_any_call():
    client = FakeClient()
    with pytest.raises(storage.StorageError):
        storage.put_file(client, "", "k", b"x", content_type="image/jpeg")
    assert client.calls == []


def test_make_client_signs_s3v4_and_blank_endpoint_is_none():
    client = storage.make_client(key="K", secret="S", region="us-east-005", endpoint_url="")
    assert client.meta.config.signature_version == "s3v4"
    assert client.meta.region_name == "us-east-005"
    assert client.meta.endpoint_url == "https://s3.us-east-005.amazonaws.com"
