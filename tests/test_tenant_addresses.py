"""Which stored addresses mail can reach, and which are the runtime's own stand-ins."""

from __future__ import annotations

from uuid import UUID

import pytest

from surogates.channels.identity import channel_user_email
from surogates.office.mentions import agent_address
from surogates.tenant.addresses import deliverable

ORG = UUID("6f1c2d3e-5a4b-4c3d-8e2f-1a2b3c4d5e6f")
PERSON = UUID("0b1c2d3e-4f5a-4b6c-9d7e-8f9a0b1c2d3e")


@pytest.mark.parametrize(
    "address",
    [
        channel_user_email("whatsapp", "40712000103", ORG),
        channel_user_email("slack", "@U0123ABC", ORG).upper(),
        f"person-{PERSON}@{ORG}.people.surogate.local",
        "x@surogate.local",
        "x@SUROGATE.LOCAL.",
        agent_address("Writer"),
        "system@surogate.invalid",
        "ana@INVALID",
    ],
)
def test_the_runtimes_stand_in_addresses_reach_no_mailbox(address: str) -> None:
    assert deliverable(address) is False


@pytest.mark.parametrize(
    "address",
    [
        "ana@bank.test",
        "Ana.Pop+Loans@Bank.Test",
        "ana@corp.local",
        "ana@notsurogate.local",
        "ana@surogate.local.bank.test",
        "ana@invalid.bank.test",
    ],
)
def test_a_real_address_is_deliverable_as_written(address: str) -> None:
    assert deliverable(address) is True


@pytest.mark.parametrize(
    "value",
    ["", "ana", "@bank.test", "ana@", "ana@.", "two@at@bank.test", "an a@bank.test", "ana@bank.test\n"],
)
def test_a_value_that_is_not_an_address_is_never_deliverable(value: str) -> None:
    assert deliverable(value) is False
