import io
import json
from types import SimpleNamespace

import pytest

import s3_uploader


class _FakeS3:
    def __init__(self, bodies):
        self.bodies = bodies
        self.listings = 0

    def get_paginator(self, _name):
        fake = self

        class _Paginator:
            def paginate(self, **_kwargs):
                fake.listings += 1
                return [{"Contents": [
                    {"Key": f"videos/{index}/metadata.json", "LastModified": index}
                    for index in range(len(fake.bodies))
                ]}]

        return _Paginator()

    def get_object(self, Bucket, Key):
        body = self.bodies[int(Key.split("/")[1])]
        if body is None:
            raise RuntimeError("synthetic read failure")
        return {"Body": io.BytesIO(json.dumps(body).encode("utf-8"))}


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(s3_uploader, "time_module", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(s3_uploader, "_video_gallery_cache",
                        {"data": None, "timestamp": 0, "complete": False, "failed_reads": False})
    return now


def _ids(videos):
    return [video["video_id"] for video in videos]


def test_failed_metadata_read_is_cached_only_briefly(monkeypatch, clock):
    # A skipped read used to be cached as the full gallery for 5 min, so
    # /video/{id} answered 404 for a video that exists. Never caching it would
    # instead hit S3 on every public page view while one file stays broken.
    client = _FakeS3([{"video_id": "a"}, None, {"video_id": "c"}])
    monkeypatch.setattr(s3_uploader, "get_s3_client", lambda: client)

    assert _ids(s3_uploader.list_video_gallery(limit=50)) == ["c", "a"]
    clock[0] += 10
    assert _ids(s3_uploader.list_video_gallery(limit=200)) == ["c", "a"]
    assert client.listings == 1  # served from the short cache

    client.bodies[1] = {"video_id": "b"}  # the read works again
    clock[0] += s3_uploader.FAILED_READ_CACHE_TTL_SECONDS
    assert _ids(s3_uploader.list_video_gallery(limit=200)) == ["c", "b", "a"]
    assert client.listings == 2

    clock[0] += s3_uploader.FAILED_READ_CACHE_TTL_SECONDS + 1
    s3_uploader.list_video_gallery(limit=200)
    assert client.listings == 2  # a clean listing keeps the normal 5 min


def test_listing_stopped_by_the_limit_is_not_served_to_a_larger_request(monkeypatch, clock):
    client = _FakeS3([{"video_id": str(index)} for index in range(3)])
    monkeypatch.setattr(s3_uploader, "get_s3_client", lambda: client)

    assert len(s3_uploader.list_video_gallery(limit=2)) == 2
    assert len(s3_uploader.list_video_gallery(limit=3)) == 3
    assert client.listings == 2
