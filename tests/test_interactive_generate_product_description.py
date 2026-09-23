"""
Tier 2 -- Generate Product Description (Op 11), driven through a real pty.

Unlike every other Tier 2 file, this operation makes real Anthropic API
calls -- there's no scripted/mocked model response anywhere here, matching
this suite's own philosophy of testing against real dependencies rather
than mocks (the same reason every other file hits the real test sheet
instead of a fake one). Real model output varies call to call, so
assertions check for structural markers this operation's system prompt
guarantees (a labeled Details block, a locked fact staying locked, an
injected conflicting caption actually getting replaced) rather than exact
wording. Every test here spends at least one real, billed API call --
fixtures that just need existing content on file use
fd.create_unit_with_description() instead of a real generation, to keep
setup free.

Covers the subset of the full 57-item manual UAT checklist that's actually
worth guarding automatically: behaviors fragile enough that a future edit
could silently break them without a live run catching it. Static facts
(menu numbering, exact prompt wording, shared styling helpers) aren't
retested here -- those are code-review guarantees, not runtime ones.

Run: pytest tests/test_interactive_generate_product_description.py -v -s
"""
from datetime import date

import pytest

import inventory as inv
from tests import fixtures_data as fd
from tests.pexpect_helpers import (
    DOWN, ENTER, close, expect_clean, require_test_mode, retry_on_quota,
    spawn_app, strip_ansi,
)

pytestmark = pytest.mark.flaky(reruns=2, reruns_delay=5)

# Real generation/refinement calls run far longer than any other
# operation's own prompts -- this suite's normal DEFAULT_TIMEOUT (35s) is
# routinely too tight for a real Claude round-trip plus a completeness
# follow-up.
GEN_TIMEOUT = 90


def _today():
    return date.today().strftime("%m-%d-%Y")


@pytest.fixture(scope="module", autouse=True)
def _check_mode():
    require_test_mode()


def _enter_op11(child, sku):
    child.sendline("11")
    child.expect("SKU:")
    child.sendline(sku)
    child.expect("Is this the correct unit\\?")
    child.sendline("yes")


def _pick_channel(child, index):
    """index: 0=Instagram, 1=Shopify, 2=WhatsApp."""
    child.expect("Which channel is this for")
    for _ in range(index):
        child.send(DOWN)
    child.send(ENTER)


def _skip_photos_and_hangtag(child):
    child.expect("Attach photos")
    child.sendline("no")
    child.expect("Upload Hangtag")
    child.sendline("")


def _answer_notes(child, notes, name_collection="", tech_specs="", regenerating=False):
    child.expect("What would you like to change or add" if regenerating else "Describe this piece")
    child.sendline(notes)
    child.expect("Name/Collection")
    child.sendline(name_collection)
    child.expect("Technical Specs")
    child.sendline(tech_specs)


def _wait_for_version(child, timeout=GEN_TIMEOUT):
    """Real completeness judgment can't be scripted -- handles either a
    straight-to-version response or a genuine 'insufficient' verdict by
    choosing 'Generate anyway, as-is', so a test whose actual purpose is
    something other than the completeness loop itself isn't derailed by
    ordinary model variance."""
    idx = child.expect(["What next\\?", "How would you like to proceed"], timeout=timeout)
    if idx == 1:
        child.send(DOWN)
        child.send(ENTER)  # Generate anyway, as-is
        child.expect("What next\\?", timeout=timeout)
    return strip_ansi(child.before + child.after)


def _lock_this_in(child):
    child.send(DOWN)
    child.send(ENTER)


class TestFirstTimeGenerationHappyPath:
    def test_full_flow_writes_a_new_instagram_description(self):
        unit = fd.create_fresh_available_unit("OP11-HAPPY")
        child = spawn_app(timeout=GEN_TIMEOUT)
        try:
            _enter_op11(child, unit["SKU"])
            _pick_channel(child, 0)  # Instagram
            _skip_photos_and_hangtag(child)
            _answer_notes(child, "Deep blue silk saree with a silver zari border, temple wear, handloom weave.")
            text = _wait_for_version(child)
            assert "VERSION 1" in text
            assert "INSTAGRAM" in text
            _lock_this_in(child)
            # Both fields were left blank -- Step 7.5 must be skipped entirely.
            text = expect_clean(child, "Write this description to the master sheet")
            assert "GENERATED DESCRIPTION SUMMARY" in text
            child.sendline("yes")
            text = expect_clean(child, "description saved")
            assert unit["SKU"] in text
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert "[INSTAGRAM" in updated["Generated Descriptions"]
        assert "Please DM to buy" in updated["Generated Descriptions"]


class TestStatusRejection:
    @pytest.mark.parametrize("status", ["Sold", "Sold - Partial Payment", "Unassigned", "Reserved"])
    def test_blocked_status_is_rejected_by_name(self, status):
        unit = fd.create_fresh_available_unit(f"OP11-BLOCKED-{status[:4]}", status=status)
        child = spawn_app()
        try:
            child.sendline("11")
            child.expect("SKU:")
            child.sendline(unit["SKU"])
            text = expect_clean(child, "SKU:")
            assert f"has status '{status}'" in text
        finally:
            close(child)


class TestPoint1ConflictResolution:
    """Pins this session's rebuild: Point 1 resolves a conflict but never
    offers a sibling-sync itself -- that only ever happens from Step 9,
    after this channel's own write succeeds."""

    def test_mismatch_shown_and_sibling_sync_never_offered_at_point1(self):
        unit = fd.create_unit_with_description(
            "OP11-POINT1", "instagram", "Existing Instagram caption body.",
            name_collection="Neelambari Jamuni -- Aalayam Collection",
        )
        child = spawn_app(timeout=GEN_TIMEOUT)
        try:
            _enter_op11(child, unit["SKU"])
            _pick_channel(child, 1)  # Shopify -- a different channel than the existing Instagram
            _skip_photos_and_hangtag(child)
            child.expect("Describe this piece")
            child.sendline("Deep blue silk saree with a silver zari border, handloom weave.")
            child.expect("Name/Collection")
            child.sendline("Kesari Padma -- Vasantha Collection")  # conflicts with on-file
            child.expect("Technical Specs")
            child.sendline("")
            # Point 1 only runs after BOTH prompts are answered.
            text = expect_clean(child, "actually correct for this unit")
            assert "POSSIBLE MISMATCH" in text
            child.send(ENTER)  # "Keep this session's answer" (default)
            # The critical assertion: goes straight to generation, never
            # offers a sibling sync here.
            idx = child.expect(["Update it now", "What next\\?", "How would you like to proceed"],
                                timeout=GEN_TIMEOUT)
            assert idx != 0, "Point 1 must never offer a sibling-sync -- that regressed (see D-385)"
        finally:
            close(child)

    def test_blank_field_never_reads_as_a_change(self):
        """Pins the most severe bug caught this session: leaving a field
        blank must never be misread as "changed to blank", which would
        risk instructing Claude to blank out a sibling's correct fact."""
        unit = fd.create_unit_with_description(
            "OP11-BLANK-GUARD", "instagram", "Existing Instagram caption body.",
            name_collection="Neelambari Jamuni -- Aalayam Collection",
        )
        child = spawn_app(timeout=GEN_TIMEOUT)
        try:
            _enter_op11(child, unit["SKU"])
            _pick_channel(child, 1)  # Shopify
            _skip_photos_and_hangtag(child)
            child.expect("Describe this piece")
            child.sendline("Deep blue silk saree with a silver zari border, handloom weave.")
            child.expect("Name/Collection")
            child.sendline("")  # leave blank -- must NOT read as "changed to blank"
            child.expect("Technical Specs")
            child.sendline("")
            _wait_for_version(child)
            _lock_this_in(child)
            idx = child.expect(["Did the Name/Collection or Technical Specs change",
                                 "Write this description to the master sheet"], timeout=20)
            assert idx == 1, "Step 7.5 should be skipped -- neither field was touched this session"
            child.sendline("yes")
            idx2 = child.expect(["Update it now", "Select an option"], timeout=20)
            assert idx2 == 1, "leaving a field blank must never trigger a sibling-sync offer"
        finally:
            close(child)


class TestChannelTone:
    def test_shopify_gets_an_explicit_details_block(self):
        unit = fd.create_fresh_available_unit("OP11-TONE-SHOPIFY")
        child = spawn_app(timeout=GEN_TIMEOUT)
        try:
            _enter_op11(child, unit["SKU"])
            _pick_channel(child, 1)  # Shopify
            _skip_photos_and_hangtag(child)
            child.expect("Describe this piece")
            child.sendline("Deep blue silk saree with a silver zari border, handloom weave, "
                            "structured drape suited for a boutique product page -- list specs clearly.")
            child.expect("Name/Collection")
            child.sendline("")
            child.expect("Technical Specs")
            child.sendline("24-karat zari, pure silk, 6 meters length")
            text = _wait_for_version(child)
            assert any(marker in text for marker in ("Zari:", "Length:", "Material:", "Details:")), (
                "Shopify output should include an explicit factual details block"
            )
        finally:
            close(child)

    def test_whatsapp_stays_conversational_no_raw_spec_block(self):
        unit = fd.create_fresh_available_unit("OP11-TONE-WHATSAPP")
        child = spawn_app(timeout=GEN_TIMEOUT)
        try:
            _enter_op11(child, unit["SKU"])
            _pick_channel(child, 2)  # WhatsApp
            _skip_photos_and_hangtag(child)
            child.expect("Describe this piece")
            child.sendline("Deep blue silk saree with a silver zari border, handloom weave.")
            child.expect("Name/Collection")
            child.sendline("")
            child.expect("Technical Specs")
            child.sendline("24-karat zari, pure silk, 6 meters length")
            text = _wait_for_version(child)
            assert not any(marker in text for marker in ("Zari:", "Material:", "Weave:", "Details:")), (
                "WhatsApp should translate specs into natural phrasing, never a raw label: value line"
            )
        finally:
            close(child)


class TestCompletenessLoop:
    def test_thin_notes_never_ask_more_than_two_rounds(self):
        unit = fd.create_fresh_available_unit("OP11-ROUNDCAP")
        child = spawn_app(timeout=GEN_TIMEOUT)
        try:
            _enter_op11(child, unit["SKU"])
            _pick_channel(child, 0)
            _skip_photos_and_hangtag(child)
            child.expect("Describe this piece")
            child.sendline("Red silk saree.")  # deliberately thin -- real model likely judges it insufficient
            child.expect("Name/Collection")
            child.sendline("")
            child.expect("Technical Specs")
            child.sendline("")

            menu_appearances = 0
            for _ in range(4):
                idx = child.expect(["What next\\?", "How would you like to proceed"], timeout=GEN_TIMEOUT)
                if idx == 0:
                    break
                menu_appearances += 1
                child.send(ENTER)  # "Answer these now" (first choice)
                child.expect("Your answer")
                child.sendline("Still just a plain red silk saree, nothing further to add.")
            assert menu_appearances <= 2, (
                "the completeness menu should never appear more than twice -- round 3 must "
                "auto-generate instead of asking again"
            )
        finally:
            close(child)

    def test_regenerate_skips_completeness_entirely(self):
        unit = fd.create_unit_with_description("OP11-REGEN-SKIP", "instagram", "Original caption body.")
        child = spawn_app(timeout=GEN_TIMEOUT)
        try:
            _enter_op11(child, unit["SKU"])
            child.expect("Which channel is this for")
            child.send(ENTER)  # Instagram -- already has content
            text = expect_clean(child, "Regenerate and replace this")
            assert "CURRENT INSTAGRAM DESCRIPTION" in text
            child.sendline("yes")
            _skip_photos_and_hangtag(child)
            _answer_notes(child, "Mention it's part of a small collection.", regenerating=True)
            # The critical assertion: goes straight to a version -- the
            # completeness menu must never appear at all for a regenerate.
            idx = child.expect(["What next\\?", "How would you like to proceed"], timeout=GEN_TIMEOUT)
            assert idx == 0, "regenerate must skip the completeness loop entirely"
            text = strip_ansi(child.before + child.after)
            assert "REGENERATION" in text
        finally:
            close(child)


class TestStep8ConflictCheck:
    """Pins this session's rebuild of the write-time concurrency check:
    scoped to just the channel being written, fresh-read merge, and a
    show-and-decide resolution instead of an automatic discard."""

    def test_different_channel_change_does_not_interrupt_the_write(self):
        unit = fd.create_fresh_available_unit("OP11-DIFFCHAN")
        child = spawn_app(timeout=GEN_TIMEOUT)
        try:
            _enter_op11(child, unit["SKU"])
            _pick_channel(child, 0)  # Instagram
            _skip_photos_and_hangtag(child)
            _answer_notes(child, "Deep blue silk saree with a silver zari border, handloom weave.")
            _wait_for_version(child)

            # Simulate a concurrent edit to a DIFFERENT channel, landing
            # after Step 1's snapshot but before this write -- must not
            # interrupt an Instagram write at all.
            row_index = inv.find_row_index_by_sku(unit["SKU"])
            retry_on_quota(inv.update_row, row_index, {
                "Generated Descriptions": f"[SHOPIFY - updated {_today()}]\nConcurrent Shopify caption.\n\n{unit['SKU']}"
            })

            _lock_this_in(child)
            child.expect("Write this description to the master sheet")
            child.sendline("yes")
            idx = child.expect(["landed while you were", "description saved"], timeout=30)
            assert idx == 1, "a different channel's concurrent change must never trigger the conflict prompt"
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert "[SHOPIFY" in updated["Generated Descriptions"], "the concurrent Shopify write must survive"
        assert "[INSTAGRAM" in updated["Generated Descriptions"]

    def test_same_channel_collision_shows_conflict_and_write_mine_anyway_wins(self):
        unit = fd.create_fresh_available_unit("OP11-SAMECHAN")
        child = spawn_app(timeout=GEN_TIMEOUT)
        try:
            _enter_op11(child, unit["SKU"])
            _pick_channel(child, 2)  # WhatsApp
            _skip_photos_and_hangtag(child)
            _answer_notes(child, "Deep blue silk saree with a silver zari border, handloom weave.")
            _wait_for_version(child)

            row_index = inv.find_row_index_by_sku(unit["SKU"])
            retry_on_quota(inv.update_row, row_index, {
                "Generated Descriptions": (
                    f"[WHATSAPP - updated {_today()}]\n"
                    f"Conflicting WhatsApp caption from another session.\n\n{unit['SKU']}"
                )
            })

            _lock_this_in(child)
            child.expect("Write this description to the master sheet")
            child.sendline("yes")
            text = expect_clean(child, "landed while you were", timeout=20)
            assert "CURRENT WHATSAPP DESCRIPTION" in text
            assert "Conflicting WhatsApp caption from another session" in text
            child.sendline("yes")  # write mine anyway
            expect_clean(child, "description saved", timeout=30)
        finally:
            close(child)

        updated = inv.get_row_by_sheet_index(inv.find_row_index_by_sku(unit["SKU"]))
        assert "Conflicting WhatsApp caption" not in updated["Generated Descriptions"], (
            "choosing 'write mine anyway' must actually replace the conflicting version"
        )


class TestSiblingSync:
    def test_offered_after_primary_write_and_cancel_abandons_only_that_sibling(self):
        unit = fd.create_unit_with_description(
            "OP11-SIBSYNC", "instagram", "Existing Instagram caption.",
            name_collection="Neelambari Jamuni -- Aalayam Collection",
        )
        row_index = inv.find_row_index_by_sku(unit["SKU"])
        current = inv.get_row_by_sheet_index(row_index)
        retry_on_quota(inv.update_row, row_index, {
            "Generated Descriptions": (
                current["Generated Descriptions"] +
                f"\n\n[WHATSAPP - updated {_today()}]\nExisting WhatsApp caption.\n\n{unit['SKU']}"
            )
        })

        child = spawn_app(timeout=GEN_TIMEOUT)
        try:
            _enter_op11(child, unit["SKU"])
            _pick_channel(child, 1)  # Shopify -- fresh channel
            _skip_photos_and_hangtag(child)
            child.expect("Describe this piece")
            child.sendline("Deep blue silk saree with a silver zari border, structured drape for a product page.")
            child.expect("Name/Collection")
            child.sendline("Kesari Padma -- Vasantha Collection")  # conflicts with on-file -> Point 1 fires
            child.expect("Technical Specs")
            child.sendline("")
            # Point 1 only runs after BOTH prompts are answered.
            expect_clean(child, "actually correct for this unit")
            child.send(ENTER)  # Keep this session's answer
            _wait_for_version(child)
            _lock_this_in(child)
            child.expect("Did the Name/Collection or Technical Specs change")
            child.sendline("no")
            child.expect("Write this description to the master sheet")
            child.sendline("yes")
            expect_clean(child, "Update it now", timeout=30)
            child.sendline("yes")  # accept the first sibling offered
            _wait_for_version(child)
            child.send(DOWN)
            child.send(DOWN)
            child.send(ENTER)  # Return to Main Menu -- cancel this sibling's patch
            # Matches partway through "{sibling} sync cancelled. Nothing changed
            # for that channel." -- the rest of that same line isn't necessarily
            # in the buffer yet the instant this matches, so only assert on the
            # part guaranteed captured at the match point.
            text = expect_clean(child, "sync cancelled|Update it now", timeout=15)
            assert "sync cancelled" in text
        finally:
            close(child)
