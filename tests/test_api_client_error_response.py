"""``_error_response`` always yields a string ``error`` -- the chat UI renders it as text."""

import json

import httpx

from surogates.harness.api_client import _error_response


def _status_error(status: int, body: object) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://api/v1/memory")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


def test_422_detail_list_is_flattened():
    exc = _status_error(422, {"detail": [{
        "type": "literal_error",
        "loc": ["body", "action"],
        "msg": "Input should be 'add', 'replace' or 'remove'",
        "input": "browse",
        "ctx": {"expected": "'add', 'replace' or 'remove'"},
    }]})
    assert json.loads(_error_response(exc)) == {
        "success": False,
        "error": "action: Input should be 'add', 'replace' or 'remove'",
    }


def test_string_and_object_details():
    assert json.loads(_error_response(_status_error(404, {"detail": "nope"})))["error"] == "nope"
    error = json.loads(_error_response(_status_error(409, {"detail": {"code": "x"}})))["error"]
    assert error == '{"code": "x"}'
