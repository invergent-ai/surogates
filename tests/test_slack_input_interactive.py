import json
from types import SimpleNamespace

from surogates.channels.platforms.slack import SlackPlatform


class _Client:
    def __init__(self, views_open_raises=False):
        self.views_open_raises = views_open_raises
        self.views = []
        self.messages = []

    async def views_open(self, **kwargs):
        if self.views_open_raises:
            raise RuntimeError("expired_trigger")
        self.views.append(kwargs)
        return {"ok": True}

    async def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        return {"ts": "901.0"}


class _Store:
    pass


class _Deps:
    def __init__(self):
        self.session_store = _Store()


def _platform_with(client):
    platform = SlackPlatform()
    platform._get_client = lambda token: client
    return platform


def _block_actions_form(session_id="s1", tool_call_id="tc1"):
    payload = {
        "type": "block_actions",
        "trigger_id": "trig1",
        "channel": {"id": "C1"},
        "message": {"thread_ts": "100.0", "ts": "100.0"},
        "actions": [
            {
                "action_id": "surogates_input_answer",
                "value": json.dumps({"session_id": session_id, "tool_call_id": tool_call_id}),
            },
        ],
    }
    return {"payload": json.dumps(payload)}


def _view_submission_form(session_id="s1", tool_call_id="tc1", value="blue"):
    view = {
        "callback_id": "surogates_input_modal",
        "private_metadata": json.dumps({"session_id": session_id, "tool_call_id": tool_call_id}),
        "state": {"values": {"q0_other": {"q0_other": {
            "type": "plain_text_input",
            "value": value,
        }}}},
    }
    return {"payload": json.dumps({"type": "view_submission", "view": view})}


async def test_answer_click_opens_modal(monkeypatch):
    from surogates.session import interactive_input

    async def fake_pending(store, *, session_id, tool_call_id=None):
        assert session_id == "s1"
        assert tool_call_id == "tc1"
        return {"tool_call_id": "tc1", "questions": [{"prompt": "Anything?"}], "context": ""}

    monkeypatch.setattr(interactive_input, "pending_input_for_session", fake_pending)
    client = _Client()

    result = await _platform_with(client).handle_interactive(
        "/slack/{app_id}/interact",
        _block_actions_form(),
        request=SimpleNamespace(),
        creds={"bot_token": "x"},
        routing=None,
        deps=_Deps(),
    )

    assert result.status_code == 200
    assert client.views[0]["trigger_id"] == "trig1"
    assert client.views[0]["view"]["callback_id"] == "surogates_input_modal"


async def test_view_submission_resolves(monkeypatch):
    from surogates.session import interactive_input

    async def fake_pending(store, *, session_id, tool_call_id=None):
        return {"tool_call_id": "tc1", "questions": [{"prompt": "Anything?"}], "context": ""}

    captured = {}

    async def fake_resolve(store, *, session_id, tool_call_id, responses):
        captured.update(session_id=session_id, tool_call_id=tool_call_id, responses=responses)
        return True

    monkeypatch.setattr(interactive_input, "pending_input_for_session", fake_pending)
    monkeypatch.setattr(interactive_input, "resolve_input_response", fake_resolve)

    result = await _platform_with(_Client()).handle_interactive(
        "/slack/{app_id}/interact",
        _view_submission_form(),
        request=SimpleNamespace(),
        creds={"bot_token": "x"},
        routing=None,
        deps=_Deps(),
    )

    assert result.status_code == 200
    assert captured == {
        "session_id": "s1",
        "tool_call_id": "tc1",
        "responses": [{"question": "Anything?", "answer": "blue", "is_other": True}],
    }


async def test_view_submission_validation_errors_return_slack_error_response(monkeypatch):
    from surogates.session import interactive_input

    async def fake_pending(store, *, session_id, tool_call_id=None):
        return {
            "tool_call_id": "tc1",
            "questions": [{"prompt": "Anything?", "choices": [{"label": "blue"}], "allow_other": True}],
            "context": "",
        }

    monkeypatch.setattr(interactive_input, "pending_input_for_session", fake_pending)

    form = _view_submission_form(value="")
    view = json.loads(form["payload"])["view"]
    view["state"]["values"] = {
        "q0_choice": {"q0_choice": {
            "selected_option": {"text": {"text": "Other"}, "value": "__other__"},
        }},
        "q0_other": {"q0_other": {"value": ""}},
    }
    form = {"payload": json.dumps({"type": "view_submission", "view": view})}

    result = await _platform_with(_Client()).handle_interactive(
        "/slack/{app_id}/interact",
        form,
        request=SimpleNamespace(),
        creds={"bot_token": "x"},
        routing=None,
        deps=_Deps(),
    )

    assert result.status_code == 200
    assert b"response_action" in result.body
    assert b"q0_other" in result.body
