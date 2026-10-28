"""The S3 backend against the stores a deployment runs, Garage and SeaweedFS, each in a container of its own.

A Restore and an Undo write a person's file only where it is still the one
they read, by the ETag the store names it by: so what each store answers is
pinned here, not taken on trust.  A head of no object, a write made only
where the object is still the one seen, in one part and in many, and a
delete made only then.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from testcontainers.core.container import DockerContainer

from surogates.storage.backend import Changed, S3Backend

# Two real stores, each started in a container of its own: far over the 5 s a default test may take.
pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.slow]

GARAGE = "dxflrs/garage:v1.1.0"
SEAWEEDFS = "chrislusf/seaweedfs:4.48"
#: The key the tests' Garage is given: an id as Garage makes them, and a secret.
KEY_ID, SECRET = "GK0123456789abcdef01234567", "0123456789abcdef" * 4
BUCKET = "agent"
_GARAGE_CONFIG = """\
metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "sqlite"
replication_factor = 1
rpc_bind_addr = "[::]:3901"
rpc_public_addr = "127.0.0.1:3901"
rpc_secret = "{secret}"

[s3_api]
s3_region = "garage"
api_bind_addr = "[::]:3900"
root_domain = ".s3.garage.localhost"
"""


def _garage(container: DockerContainer, *command: str) -> str:
    """Run the Garage command *command* in its container; its output.  Raised on a failure."""
    code, out = container.exec(["/garage", *command])
    if code != 0:
        raise RuntimeError(f"garage {' '.join(command)}: {out.decode(errors='replace')}")
    return out.decode()


def _until(ready, seconds: float = 60.0) -> None:
    """Wait for *ready* to answer without raising, *seconds* at most."""
    until = time.monotonic() + seconds
    while True:
        try:
            ready()
            return
        except Exception:
            if time.monotonic() > until:
                raise
            time.sleep(0.5)


@pytest.fixture(scope="session")
def garage(tmp_path_factory):
    """A one-node Garage with its layout applied, a key and the tests' bucket: the endpoint, key and region."""
    config = tmp_path_factory.mktemp("garage") / "garage.toml"
    config.write_text(_GARAGE_CONFIG.format(secret=uuid.uuid4().hex * 2))
    with DockerContainer(GARAGE).with_volume_mapping(str(config), "/etc/garage.toml", "ro").with_exposed_ports(3900) as store:
        _until(lambda: _garage(store, "status"))
        node = _garage(store, "node", "id", "-q").strip().split("@")[0]
        _garage(store, "layout", "assign", "-z", "dc1", "-c", "1G", node)
        _garage(store, "layout", "apply", "--version", "1")
        _garage(store, "key", "import", "--yes", "-n", "tests", KEY_ID, SECRET)
        _garage(store, "bucket", "create", BUCKET)
        _garage(store, "bucket", "allow", "--read", "--write", "--owner", BUCKET, "--key", "tests")
        yield f"http://{store.get_container_host_ip()}:{store.get_exposed_port(3900)}", KEY_ID, SECRET, "garage"


@pytest.fixture(scope="session")
def seaweedfs():
    """A SeaweedFS of one process, its S3 open to any key, with the tests' bucket: the endpoint, key and region."""
    with DockerContainer(SEAWEEDFS).with_command("mini -dir=/data").with_exposed_ports(8333) as store:
        endpoint = f"http://{store.get_container_host_ip()}:{store.get_exposed_port(8333)}"
        s3 = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id="tests", aws_secret_access_key="tests", region_name="us-east-1")
        _until(lambda: s3.create_bucket(Bucket=BUCKET))
        yield endpoint, "tests", "tests", "us-east-1"


@pytest.fixture(params=["garage", "seaweedfs"])
def store(request) -> S3Backend:
    endpoint, key, secret, region = request.getfixturevalue(request.param)
    return S3Backend(endpoint, key, secret, region)


@pytest.fixture()
def key() -> str:
    """A key of the test's own in the bucket, in a folder, as a project's files are."""
    return f"boundaries/{uuid.uuid4()}/workspace/Report.docx"


def a_file(tmp_path: Path, name: str, size: int, fill: bytes = b"x") -> Path:
    path = tmp_path / name
    path.write_bytes(fill * size)
    return path


async def test_a_head_names_the_objects_contents_by_an_etag_that_changes_with_each_write_and_tells_none_absent(store, key, tmp_path):
    with pytest.raises(KeyError):
        await store.stat(BUCKET, key)
    with pytest.raises(KeyError):
        await store.read(BUCKET, key)
    with pytest.raises(KeyError):
        await store.download(BUCKET, key, tmp_path / "out.bin")
    await store.write(BUCKET, key, b"PK\x03\x04 report v1")
    first = await store.stat(BUCKET, key)
    # Written again within the same second: its date may not change, its ETag does.
    await store.write(BUCKET, key, b"PK\x03\x04 report v2")
    second = await store.stat(BUCKET, key)
    assert first["size"] == second["size"] == 14 and first["etag"] and second["etag"] and first["etag"] != second["etag"]
    assert await store.read(BUCKET, key) == b"PK\x03\x04 report v2"
    # A bucket there is none of is the store's answer too, and no answer that the object is absent.
    with pytest.raises(ClientError):
        await store.read(f"none-{uuid.uuid4().hex[:8]}", key)


@pytest.mark.parametrize("size", [12, 20 * 2**20], ids=["one part", "in parts"])
async def test_a_write_made_where_the_object_is_still_the_one_its_writer_saw_lands_and_one_saved_since_is_kept(store, key, tmp_path, size):
    await store.write(BUCKET, key, b"PK\x03\x04 report v1")
    seen = (await store.stat(BUCKET, key))["etag"]
    version = a_file(tmp_path, "version.bin", size, b"v")
    await store.upload(BUCKET, key, version, if_tag=seen)
    written = await store.stat(BUCKET, key)
    assert written["size"] == size and written["etag"] != seen
    # Saved since it was seen: the write is refused, and the save is kept.
    await store.write(BUCKET, key, b"PK\x03\x04 saved by you")
    with pytest.raises(Changed):
        await store.upload(BUCKET, key, version, if_tag=written["etag"])
    assert await store.read(BUCKET, key) == b"PK\x03\x04 saved by you"


@pytest.mark.parametrize("size", [12, 20 * 2**20], ids=["one part", "in parts"])
async def test_a_write_made_where_there_is_still_no_object_lands_once_and_never_over_one_made_since(store, key, tmp_path, size):
    version = a_file(tmp_path, "version.bin", size, b"v")
    await store.upload(BUCKET, key, version, if_absent=True)
    assert (await store.stat(BUCKET, key))["size"] == size
    with pytest.raises(Changed):
        await store.upload(BUCKET, key, a_file(tmp_path, "other.bin", 5), if_absent=True)
    assert (await store.stat(BUCKET, key))["size"] == size


async def test_a_delete_made_where_the_object_is_still_the_one_its_deleter_saw_takes_it_away_and_one_saved_since_is_kept(store, key):
    await store.write(BUCKET, key, b"PK\x03\x04 report v1")
    seen = (await store.stat(BUCKET, key))["etag"]
    await store.write(BUCKET, key, b"PK\x03\x04 saved by you")
    with pytest.raises(Changed):
        await store.delete(BUCKET, key, if_tag=seen)
    assert await store.read(BUCKET, key) == b"PK\x03\x04 saved by you"
    await store.delete(BUCKET, key, if_tag=(await store.stat(BUCKET, key))["etag"])
    assert not await store.exists(BUCKET, key)
    # Gone since it was seen: a change too.
    with pytest.raises(Changed):
        await store.delete(BUCKET, key, if_tag=seen)
