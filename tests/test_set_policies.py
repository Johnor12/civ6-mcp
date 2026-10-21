"""set_policies must let a card move between slots within one batch.

The engine's CanSlotPolicy rejects any card that is already active, whatever
the target slot, so a plain pre-check refused every swap. The builder now
accepts an active card when the slot it occupies is also being reassigned.
"""

from civ_mcp import lua as lq


def test_batch_slots_are_declared_as_touched():
    lua = lq.build_set_policies({0: "POLICY_DISCIPLINE", 2: "POLICY_SURVEY"})
    assert "local touched = {[0]=true, [2]=true}" in lua


def test_active_card_is_rescued_only_when_its_slot_is_in_the_batch():
    lua = lq.build_set_policies({1: "POLICY_DISCIPLINE"})
    assert "activeSlot_1 >= 0 and not touched[activeSlot_1]" in lua
    assert "include slot" in lua
    # a card that is not active anywhere still gets the plain CANNOT_SLOT path
    assert "activeSlot_1 < 0" in lua


def test_explicit_clear_touches_the_slot_without_adding():
    lua = lq.build_set_policies({2: "NONE"})
    assert "table.insert(clearList, 2)" in lua
    assert "addList[2]" not in lua
    assert "local touched = {[2]=true}" in lua
