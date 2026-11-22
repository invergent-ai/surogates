"""A capped read against an S3-compatible store, where ``SUROGATES_TEST_S3_ENDPOINT`` names one."""

from __future__ import annotations

import os
import uuid

import pytest

from surogates.storage.backend import S3Backend

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def store() -> S3Backend:
    endpoint = os.environ.get("SUROGATES_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("no S3-compatible store named")
    return S3Backend(endpoint, os.environ["SUROGATES_TEST_S3_KEY"], os.environ["SUROGATES_TEST_S3_SECRET"], "garage")


async def test_a_capped_read_of_a_real_object(store) -> None:
    bucket = f"cap-{uuid.uuid4().hex[:20]}"
    await store.create_bucket(bucket)
    data = bytes(range(256)) * 16_384  # 4 MiB, more than one chunk
    await store.write(bucket, "large.bin", data)
    await store.write(bucket, "empty.bin", b"")

    assert await store.read_at_most(bucket, "large.bin", 10) == data[:10]
    assert await store.read_at_most(bucket, "large.bin", (1 << 20) + 5) == data[:(1 << 20) + 5]
    assert await store.read_at_most(bucket, "large.bin", len(data) + 1) == data
    assert await store.read_at_most(bucket, "large.bin", 0) == b""
    assert await store.read_at_most(bucket, "empty.bin", 10) == b""
    with pytest.raises(KeyError):
        await store.read_at_most(bucket, "missing.bin", 10)
