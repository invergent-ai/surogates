"""Simulation ids never collide with real ones, and the boundary nests the conversation's."""

from __future__ import annotations

from surogates.channels.memory_boundary import SIMULATION_BOUNDARY_PREFIX, simulation_boundary
from surogates.simulation.ids import (
    SIMULATION_ID_PREFIX,
    is_simulation_id,
    new_phone_number_id,
    new_wa_id,
)


def test_ids_carry_the_reserved_prefix_and_are_digits():
    pnid, wa_id = new_phone_number_id(), new_wa_id()
    assert pnid.startswith(SIMULATION_ID_PREFIX) and pnid.isdigit() and len(pnid) == 16
    assert wa_id.startswith(SIMULATION_ID_PREFIX) and wa_id.isdigit() and len(wa_id) == 12


def test_ids_are_unique():
    assert len({new_phone_number_id() for _ in range(200)}) == 200


def test_real_ids_are_not_simulation_ids():
    assert not is_simulation_id("100000000000001")
    assert not is_simulation_id("40712000001")
    assert not is_simulation_id("")
    assert is_simulation_id(new_wa_id())


def test_boundary_nests_the_conversation_boundary():
    assert SIMULATION_BOUNDARY_PREFIX == "simulation:"
    assert simulation_boundary("r1", "whatsapp:dm:999") == "simulation:r1:whatsapp:dm:999"
