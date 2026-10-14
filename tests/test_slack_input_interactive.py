import json
import pytest
from types import SimpleNamespace

from surogates.channels.platforms.slack import SlackPlatform


class _Client:
    def __init__(self, views_open_raises=False):
        self.views_open_raises = views_open_raises
        self.views = []
        self.messages = []
        self.updates = []

    async def views_open(self, **kwargs):
        if self.views_open_raises:
            raise RuntimeError("expired_trigger")
        self.views.append(kwargs)
        return {"ok": True}

    async def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        return {"ts": "901.0"}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}


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


def _view_submission_form(
    session_id="s1", tool_call_id="tc1", value="blue", channel="", message_ts=""
):
    view = {
        "callback_id": "surogates_input_modal",
        "private_metadata": json.dumps({
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "channel": channel,
            "message_ts": message_ts,
        }),
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
    meta = json.loads(client.views[0]["view"]["private_metadata"])
    assert meta["channel"] == "C1"
    assert meta["message_ts"] == "100.0"


async def test_view_submission_updates_message_on_success(monkeypatch):
    from surogates.session import interactive_input

    async def fake_pending(store, *, session_id, tool_call_id=None):
        return {"tool_call_id": "tc1", "questions": [{"prompt": "Anything?"}], "context": ""}

    async def fake_resolve(store, *, session_id, tool_call_id, responses):
        return True

    monkeypatch.setattr(interactive_input, "pending_input_for_session", fake_pending)
    monkeypatch.setattr(interactive_input, "resolve_input_response", fake_resolve)
    client = _Client()

    result = await _platform_with(client).handle_interactive(
        "/slack/{app_id}/interact",
        _view_submission_form(channel="C1", message_ts="100.0", value="use a tunnel"),
        request=SimpleNamespace(),
        creds={"bot_token": "x"},
        routing=None,
        deps=_Deps(),
    )

    assert result.status_code == 200
    assert len(client.updates) == 1
    update = client.updates[0]
    assert update["channel"] == "C1"
    assert update["ts"] == "100.0"
    assert all(b.get("type") != "actions" for b in update["blocks"])
    assert "use a tunnel" in json.dumps(update["blocks"])


async def test_view_submission_no_update_when_resolve_is_stale(monkeypatch):
    from surogates.session import interactive_input

    async def fake_pending(store, *, session_id, tool_call_id=None):
        return {"tool_call_id": "tc1", "questions": [{"prompt": "Anything?"}], "context": ""}

    async def fake_resolve(store, *, session_id, tool_call_id, responses):
        return False  # already answered by a concurrent submit

    monkeypatch.setattr(interactive_input, "pending_input_for_session", fake_pending)
    monkeypatch.setattr(interactive_input, "resolve_input_response", fake_resolve)
    client = _Client()

    result = await _platform_with(client).handle_interactive(
        "/slack/{app_id}/interact",
        _view_submission_form(channel="C1", message_ts="100.0"),
        request=SimpleNamespace(),
        creds={"bot_token": "x"},
        routing=None,
        deps=_Deps(),
    )

    assert result.status_code == 200
    assert client.updates == []


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
        "responses": [{"question": "Anything?", "answer": "blue", "is_other": False}],
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


# ---------------------------------------------------------------------------
# A few short choices: buttons in the message, one tap answers
# ---------------------------------------------------------------------------

CONFIRM = [{"prompt": "Trimit mesajul?", "choices": [{"label": "Trimit"}, {"label": "Nu trimit"}]}]


def test_a_few_short_choices_are_buttons_in_the_message():
    from surogates.channels.platforms.slack_interactive import build_input_prompt_blocks

    text, blocks = build_input_prompt_blocks(session_id="s1", tool_call_id="tc1", questions=CONFIRM,
                                             context="Mesaj pentru Ion: «Vă așteptăm mâine la 10.»")
    assert text == "Trimit mesajul?"
    assert "Vă așteptăm" in blocks[0]["text"]["text"] and "Trimit mesajul?" in blocks[0]["text"]["text"]
    buttons = blocks[1]["elements"]
    assert [b["text"]["text"] for b in buttons] == ["Trimit", "Nu trimit"]
    assert [b["action_id"] for b in buttons] == ["surogates_input_choice_0", "surogates_input_choice_1"]
    assert json.loads(buttons[1]["value"]) == {"session_id": "s1", "tool_call_id": "tc1", "choice": 1}
    assert "I need your input" not in json.dumps(blocks)


@pytest.mark.parametrize("questions", [
    [{"prompt": "Pick", "choices": [{"label": f"o{i}"} for i in range(6)]}],  # too many
    [{"prompt": "Pick", "choices": [{"label": "x" * 41}, {"label": "y"}]}],  # too long
    [{"prompt": "Name?"}],  # free text
    CONFIRM + CONFIRM,  # several questions
])
def test_anything_else_keeps_the_form(questions):
    from surogates.channels.platforms.slack_interactive import ANSWER_ACTION_ID, build_input_prompt_blocks

    _, blocks = build_input_prompt_blocks(session_id="s1", tool_call_id="tc1", questions=questions)
    assert [e["action_id"] for e in blocks[1]["elements"]] == [ANSWER_ACTION_ID]


def _tap_form(choice, session_id="s1", tool_call_id="tc1"):
    payload = {
        "type": "block_actions", "trigger_id": "trig1", "channel": {"id": "C1"},
        "container": {"message_ts": "100.0"}, "message": {"ts": "100.0"},
        "actions": [{"action_id": f"surogates_input_choice_{choice}",
                     "value": json.dumps({"session_id": session_id, "tool_call_id": tool_call_id,
                                          "choice": choice})}],
    }
    return {"payload": json.dumps(payload)}


async def test_one_tap_answers_and_the_message_shows_the_choice(monkeypatch):
    from surogates.session import interactive_input

    resolved = []

    async def pending(store, *, session_id, tool_call_id):
        return {"tool_call_id": tool_call_id, "questions": CONFIRM}

    async def resolve(store, **kw):
        resolved.append(kw)
        return True

    monkeypatch.setattr(interactive_input, "pending_input_for_session", pending)
    monkeypatch.setattr(interactive_input, "resolve_input_response", resolve)
    client = _Client()
    response = await _platform_with(client)._handle_interact(_tap_form(1), creds={"bot_token": "x"}, deps=_Deps())
    assert response.status_code == 200 and client.views == []  # no form
    assert resolved == [{"session_id": "s1", "tool_call_id": "tc1",
                         "responses": [{"question": "Trimit mesajul?", "answer": "Nu trimit", "is_other": False}]}]
    [update] = client.updates
    assert update["ts"] == "100.0" and "Nu trimit" in update["text"]


async def test_a_stale_or_forged_tap_changes_nothing(monkeypatch):
    from surogates.session import interactive_input

    async def nothing(store, **kw):
        return None

    async def resolve(store, **kw):
        raise AssertionError("must not resolve")

    monkeypatch.setattr(interactive_input, "pending_input_for_session", nothing)
    monkeypatch.setattr(interactive_input, "resolve_input_response", resolve)
    client = _Client()
    await _platform_with(client)._handle_interact(_tap_form(0), creds={"bot_token": "x"}, deps=_Deps())

    async def pending(store, *, session_id, tool_call_id):
        return {"tool_call_id": tool_call_id, "questions": CONFIRM}

    monkeypatch.setattr(interactive_input, "pending_input_for_session", pending)
    await _platform_with(client)._handle_interact(_tap_form(7), creds={"bot_token": "x"}, deps=_Deps())
    assert client.updates == []
