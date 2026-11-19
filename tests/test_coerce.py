from types import SimpleNamespace

from pydantic import BaseModel

from surogates.tools.coerce import coerce_tool_args


class _Addr(BaseModel):
    city: str


class _Args(BaseModel):
    addr: _Addr
    maybe: _Addr | None = None
    note: str


def test_double_encoded_ref_objects_are_decoded():
    schema = _Args.model_json_schema()
    registry = SimpleNamespace(get=lambda _: SimpleNamespace(schema=SimpleNamespace(parameters=schema)))
    encoded = '{"city": "X"}'

    args = coerce_tool_args("t", {"addr": encoded, "maybe": encoded, "note": encoded}, registry)

    assert args == {"addr": {"city": "X"}, "maybe": {"city": "X"}, "note": encoded}
