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
