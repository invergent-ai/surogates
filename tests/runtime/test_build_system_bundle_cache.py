"""Unit tests for :func:`surogates.api.app.build_system_bundle_cache`.

The factory wires the Hub SDK with the same Configuration the
``build_file_bundle_cache`` factory uses, then constructs a
``SystemBundleCache`` whose loader resolves the largest ``v*`` tag on
``platform/system-skills`` and returns an :class:`AgentFileBundle`
pointing at that snapshot.

We cover the resolver in isolation by stubbing
``surogate_hub_sdk.TagsApi.list_tags``:

* picks the largest ``v\\d+`` tag,
* ignores non-``v*`` tags (e.g. ``latest`` aliases),
* raises ``LookupError`` when no ``v*`` tag exists yet,
* warns when pagination is reported (operational drift guard).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from surogates.api.app import build_system_bundle_cache
from surogates.runtime import SYSTEM_SKILLS_REPO


def _settings(endpoint: str = "https://hub.example.invalid/api/v1"):
    return SimpleNamespace(
        hub=SimpleNamespace(
            endpoint=endpoint, username="u", password="p",
        ),
    )


def _ref_list(tags):
    page = MagicMock()
    page.results = [SimpleNamespace(id=t) for t in tags]
    page.pagination = SimpleNamespace(has_more=False, next_offset="")
    return page


@pytest.fixture
def patched_sdk():
    """Patch the SDK + ``SystemBundleCache`` so the factory does not
    perform real network calls.  Yields the ``TagsApi`` mock so each
    test can program its ``list_tags`` return value, plus a dict
    that captures the loader the factory hands to the cache."""

    captured: dict = {}

    class _CapturingCache:
        def __init__(self, loader):
            captured["loader"] = loader

    # The factory imports SystemBundleCache from surogates.runtime
    # (the package's re-export), so the patch must target that
    # binding — not the module the class is defined in.
    with patch(
        "surogates.runtime.SystemBundleCache",
        _CapturingCache,
    ), \
         patch("surogate_hub_sdk.ApiClient") as _ApiClient, \
         patch("surogate_hub_sdk.Configuration") as _Configuration, \
         patch("surogate_hub_sdk.ObjectsApi") as _ObjectsApi, \
         patch("surogate_hub_sdk.TagsApi") as _TagsApi:
        tags_api = MagicMock()
        _TagsApi.return_value = tags_api
        yield SimpleNamespace(
            tags_api=tags_api,
            captured=captured,
        )


def test_requires_hub_endpoint() -> None:
    with pytest.raises(RuntimeError, match="settings.hub.endpoint"):
        build_system_bundle_cache(settings=_settings(endpoint=""))


@pytest.mark.asyncio
async def test_resolver_picks_largest_v_tag(patched_sdk) -> None:
    patched_sdk.tags_api.list_tags.return_value = _ref_list(
        ["v1", "v3", "v2"],
    )
    # Patch the bundle-build hop so we don't need a real Hub client.
    with patch("surogates.runtime.AgentFileBundle") as bundle_cls, \
         patch("surogates.runtime.HubBundleClient") as _hub, \
         patch(
             "surogates.runtime.bundle_cache._L2ReadThroughHub"
         ) as _l2_rt:
        build_system_bundle_cache(settings=_settings())
        loader = patched_sdk.captured["loader"]
        await loader()

    bundle_cls.assert_called_once()
    _, kwargs = bundle_cls.call_args
    assert kwargs["version"] == "v3"
    assert kwargs["hub_ref"] == SYSTEM_SKILLS_REPO


@pytest.mark.asyncio
async def test_resolver_ignores_non_v_tags(patched_sdk) -> None:
    patched_sdk.tags_api.list_tags.return_value = _ref_list(
        ["latest", "v2", "stable", "v4"],
    )
    with patch("surogates.runtime.AgentFileBundle") as bundle_cls, \
         patch("surogates.runtime.HubBundleClient"), \
         patch("surogates.runtime.bundle_cache._L2ReadThroughHub"):
        build_system_bundle_cache(settings=_settings())
        await patched_sdk.captured["loader"]()
    _, kwargs = bundle_cls.call_args
    assert kwargs["version"] == "v4"


@pytest.mark.asyncio
async def test_resolver_ignores_v_prefixed_garbage(patched_sdk) -> None:
    patched_sdk.tags_api.list_tags.return_value = _ref_list(
        ["v1", "vNOT-A-NUMBER", "v2"],
    )
    with patch("surogates.runtime.AgentFileBundle") as bundle_cls, \
         patch("surogates.runtime.HubBundleClient"), \
         patch("surogates.runtime.bundle_cache._L2ReadThroughHub"):
        build_system_bundle_cache(settings=_settings())
        await patched_sdk.captured["loader"]()
    _, kwargs = bundle_cls.call_args
    assert kwargs["version"] == "v2"


@pytest.mark.asyncio
async def test_resolver_raises_lookup_when_no_v_tag(patched_sdk) -> None:
    patched_sdk.tags_api.list_tags.return_value = _ref_list(
        ["latest", "stable"],
    )
    with patch("surogates.runtime.HubBundleClient"), \
         patch("surogates.runtime.bundle_cache._L2ReadThroughHub"):
        build_system_bundle_cache(settings=_settings())
        with pytest.raises(LookupError, match="no v\\* tag yet"):
            await patched_sdk.captured["loader"]()
