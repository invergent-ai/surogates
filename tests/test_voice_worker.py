"""Which of our numbers was called, and by whom, from the SIP participant LiveKit puts in the room."""
from surogates.voice.worker import CallInfo, call_info


def test_call_info_from_sip_attributes():
    attrs = {"sip.trunkPhoneNumber": "+40300000001", "sip.phoneNumber": "+40722000111", "sip.callID": "SCL_abc"}
    assert call_info("call-_+40722000111_x", attrs) == CallInfo(call_id="SCL_abc", called="+40300000001",
                                                                 caller="+40722000111")


def test_call_info_normalizes_the_called_number_and_tolerates_a_withheld_caller():
    assert call_info("room-1", {"sip.trunkPhoneNumber": "40300000001"}) == \
        CallInfo(call_id="room-1", called="+40300000001", caller=None)


def test_call_info_not_a_phone_call():
    assert call_info("room-1", {}) is None


class _Session:
    def __init__(self):
        self.handlers = {}

    def on(self, name):
        def register(fn):
            self.handlers[name] = fn
            return fn
        return register

    def emit(self, name, state):
        from types import SimpleNamespace
        self.handlers[name](SimpleNamespace(new_state=state))


class _Scape:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, *a))


def test_the_soundscape_follows_who_speaks_and_the_agent_thinking():
    from types import SimpleNamespace
    from surogates.voice.worker import follow_call
    session, scape, agent = _Session(), _Scape(), SimpleNamespace(last_said="")
    follow_call(session, agent, scape)
    session.emit("agent_state_changed", "thinking")
    assert scape.calls[-2:] == [("agent_speaking", False), ("agent_thinking", True)]
    session.emit("agent_state_changed", "speaking")
    assert ("agent_speaking", True) in scape.calls[-2:]
    session.emit("agent_state_changed", "listening")
    assert scape.calls[-2:] == [("agent_speaking", False), ("agent_thinking", False)]
    session.emit("user_state_changed", "speaking")
    assert scape.calls[-1] == ("caller_speaking", True) and ("writing",) not in scape.calls


def test_the_pen_writes_once_when_the_caller_answers_a_question_for_details():
    from types import SimpleNamespace
    from surogates.voice.worker import follow_call
    session, scape = _Session(), _Scape()
    agent = SimpleNamespace(last_said="Pe ce nume fac programarea?")
    follow_call(session, agent, scape)
    session.emit("user_state_changed", "speaking")
    session.emit("user_state_changed", "listening")
    session.emit("user_state_changed", "speaking")  # a second breath of the same answer
    assert scape.calls.count(("writing",)) == 1
    assert scape.calls[-1] == ("caller_speaking", True)


async def test_call_cleanup_runs_every_step_even_when_one_fails(caplog):
    from surogates.voice.worker import run_all
    done = []

    async def ok(name):
        done.append(name)

    async def boom():
        raise ValueError("background crashed")

    await run_all(("end the session", ok("end")), ("background", boom()), ("release the line", ok("release")))
    assert done == ["end", "release"]
    assert "background" in caplog.text
