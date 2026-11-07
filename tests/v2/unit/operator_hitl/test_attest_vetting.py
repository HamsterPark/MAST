"""What an attest prompt may say - the asker does not decide what the operator signs.

An ``attest`` prompt asks the operator to sign a value into registry slots. The value an option
records is its *id*; the operator reads its *label*. Whoever posts the prompt (an external agent
through ``/api/ext``, ``AskOperator``) writes both, so a prompt like

    attest laser_area_safe, options [{"id": "yes", "label": "NO - the laser area is NOT safe"}]

made a click on "NOT safe" record True. The server therefore takes what the
operator sees from the slot definitions: a yes/no slot takes no options at all, a choice slot
takes options from its own list under its own labels, a number slot its unit and range from its
definition, a text slot's options are labelled with the text they record. Every path that posts a
prompt goes through the same check (``prompts.vet``, called by ``prompts.post``).
"""
from __future__ import annotations

import pytest

from mast.operator_hitl import optical_state, prompts
from mast.skills.builtins.operator_skills import AskOperator

PromptOption = prompts.PromptOption


@pytest.fixture(autouse=True)
def _memory(memory_backends):
    yield


def attest(slots, **kw):
    kw.setdefault("title", "Please acknowledge")
    return prompts.OperatorPrompt(kind="attest", attest_slots=list(slots), **kw)


def labels(p):
    return [(o.id, o.label) for o in p.options]


# ── a yes/no slot takes no options of the asker's ────────────────────────────────


def test_the_audit_prompt_that_made_not_safe_sign_safe_is_refused():
    p = attest(["laser_area_safe"], title="Please acknowledge the shift summary",
               message="Pick one to close this card.",
               options=[PromptOption("yes", "NO - the laser area is NOT safe"), PromptOption("no", "Later")])
    with pytest.raises(prompts.PromptError, match="yes/no slot.*cannot carry its own options"):
        prompts.post(p)
    assert prompts.list_open() == [], "the operator was shown nothing"
    assert not optical_state.get_store().is_valid("laser_area_safe", expect=True)


@pytest.mark.parametrize("options", [
    [PromptOption("true", "Dismiss"), PromptOption("false", "Keep")],
    [PromptOption("yes", "Yes")],
    [PromptOption("1", "1")],
])
def test_no_option_of_any_shape_is_accepted_on_a_yes_no_slot(options):
    with pytest.raises(prompts.PromptError, match="cannot carry its own options"):
        prompts.post(attest(["laser_area_safe"], options=options))
    assert prompts.list_open() == []


def test_a_yes_no_slot_is_asked_without_options_and_the_answer_is_the_value():
    p = attest(["laser_area_safe"], severity="info")
    pid = prompts.post(p)
    assert p.severity == "safety" and p.options == []
    ans = prompts.answer(pid, by="alice", source="web", value=True)
    assert ans.value is True and optical_state.get_store().is_valid("laser_area_safe", expect=True)


def test_the_same_check_on_every_door():
    """prompts.post, the backend's own check path through vet, and AskOperator all refuse it."""
    p = attest(["laser_area_safe"], options=[PromptOption("yes", "NO - NOT safe")])
    with pytest.raises(prompts.PromptError):
        prompts.vet(p)
    with pytest.raises(prompts.PromptError):
        prompts.post(p)
    ctx = type("Ctx", (), {"check_abort": staticmethod(lambda: False)})()
    res = AskOperator().execute(ctx, {"title": "Acknowledge", "kind": "attest", "attest_slots": "laser_area_safe",
                                      "options": "yes=NO - the laser area is NOT safe,no=Later"})
    assert not res.success and res.error.startswith("invalid prompt") and "own options" in res.error
    assert prompts.list_open() == []


# ── a choice slot: its own options, under its own labels ──────────────────────────────


def test_a_choice_slot_shows_its_own_labels_whatever_the_asker_wrote():
    p = attest(["excitation_waveplate"], options=[PromptOption("hwp_1064", "the 532 nm plate (safe to pick)"),
                                                  PromptOption("hwp_532", "1064")])
    prompts.post(p)
    assert labels(p) == [("hwp_1064", "λ/2 @1064 nm"), ("hwp_532", "λ/2 @532 nm")], \
        "the asker's order is kept, the labels are the slot's"


def test_a_subset_of_the_slots_options_is_fine():
    p = attest(["objective"], options=[PromptOption("10x", "x"), PromptOption("100x", "y")])
    prompts.post(p)
    assert [o.id for o in p.options] == ["10x", "100x"]


def test_an_option_the_slot_does_not_have_is_refused():
    with pytest.raises(prompts.PromptError, match="'200x' is not one of the options of slot"):
        prompts.post(attest(["objective"], options=[PromptOption("10x", "10x"), PromptOption("200x", "200x")]))
    assert prompts.list_open() == []


def test_a_choice_slot_without_options_is_left_to_be_drawn_from_its_definition():
    p = attest(["objective"])
    prompts.post(p)
    assert p.options == []


def test_the_answer_records_the_option_that_was_labelled_for_it():
    p = attest(["excitation_waveplate"], options=[PromptOption("hwp_532", "wrong label"),
                                                  PromptOption("hwp_1064", "wrong label 2")])
    pid = prompts.post(p)
    shown = dict(labels(prompts.get(pid)))
    ans = prompts.answer(pid, by="alice", source="web", choice="hwp_1064")
    assert shown["hwp_1064"] == "λ/2 @1064 nm"
    assert optical_state.get_store().get("excitation_waveplate").value == ans.value == "hwp_1064"


def test_several_choice_slots_share_only_the_options_they_all_have():
    # excitation_waveplate has hwp_532, hwp_1064, none; analyzer_optic adds polarizer
    ok = attest(["excitation_waveplate", "analyzer_optic"], options=[PromptOption("hwp_532", "")])
    prompts.post(ok)
    with pytest.raises(prompts.PromptError, match="'polarizer' is not one of the options"):
        prompts.post(attest(["excitation_waveplate", "analyzer_optic"], options=[PromptOption("polarizer", "")]))


# ── a number slot: its unit and range, numbers as options ─────────────────────────────────


def test_a_number_slot_takes_its_unit_and_range_from_its_definition():
    p = attest(["incident_polarizer_deg"], unit="mW", number_min=-5, number_max=1e9)
    prompts.post(p)
    assert (p.unit, p.number_min, p.number_max) == ("deg", 0.0, 360.0)


def test_a_number_answer_outside_the_slots_range_is_refused_at_the_prompt():
    pid = prompts.post(attest(["incident_polarizer_deg"], number_max=1e9))
    with pytest.raises(prompts.PromptError, match="above the maximum 360"):
        prompts.answer(pid, by="alice", source="web", value=720)
    assert prompts.get(pid).status == "open"
    ans = prompts.answer(pid, by="alice", source="web", value="45")
    assert ans.value == 45.0 and optical_state.get_store().get("incident_polarizer_deg").value == 45.0


def test_number_options_are_the_numbers_to_sign_under_labels_made_from_them():
    p = attest(["incident_polarizer_deg"], options=[PromptOption("45", "Set to 0 degrees, nothing to worry about")])
    pid = prompts.post(p)
    assert labels(p) == [("45", "45 deg")]
    ans = prompts.answer(pid, by="alice", source="web", choice="45")
    assert ans.value == 45.0 == optical_state.get_store().get("incident_polarizer_deg").value


@pytest.mark.parametrize("option_id", ["forty-five", "nan", "inf", "-1", "361", "1e999"])
def test_a_number_option_that_is_not_a_number_in_range_is_refused(option_id):
    with pytest.raises(prompts.PromptError, match="not a number|outside the range"):
        prompts.post(attest(["incident_polarizer_deg"], options=[PromptOption(option_id, "x")]))
    assert prompts.list_open() == []


def test_number_slots_with_different_units_are_not_asked_together():
    with pytest.raises(prompts.PromptError, match="different units"):
        prompts.post(attest(["incident_polarizer_deg", "npi_power_pct"]))


def test_number_slots_with_the_same_unit_share_the_narrower_range():
    store = optical_state.get_store()
    store.set_slots([optical_state.OpticalSlot("angle_a", "A", kind="number", unit="deg", min=0.0, max=360.0),
                     optical_state.OpticalSlot("angle_b", "B", kind="number", unit="deg", min=10.0, max=90.0)])
    p = attest(["angle_a", "angle_b"])
    prompts.post(p)
    assert (p.unit, p.number_min, p.number_max) == ("deg", 10.0, 90.0)


def test_the_incident_angle_prompt_the_protocols_post_still_works_end_to_end():
    """optics_protocols.py: a number slot, one option whose id is the angle, labelled by the protocol."""
    p = attest(["incident_polarizer_deg"], options=[PromptOption("22.5", "Set to 22.5°")], severity="action")
    pid = prompts.post(p)
    ans = prompts.answer(pid, by="alice", source="web", choice="22.5")
    assert float(ans.value) == 22.5 and optical_state.get_store().get("incident_polarizer_deg").value == 22.5


def test_the_waveplate_prompt_the_protocols_post_still_works_and_loses_only_its_hint():
    """optics_protocols.py annotates the labels of the plates that cover the wavelength. The slot's
    labels replace them (the prompt's context still names the covering plates); what is signed
    is unchanged."""
    slot = optical_state.get_store().slot("excitation_waveplate")
    opts = [PromptOption(str(o["id"]), str(o["label"]) + ("  ✓ covers 532 nm" if o["id"] == "hwp_532" else ""))
            for o in slot.options]
    p = attest(["excitation_waveplate"], options=opts, severity="action", context={"covering": ["hwp_532"]})
    pid = prompts.post(p)
    assert [lab for _, lab in labels(p)] == [str(o["label"]) for o in slot.options]
    assert p.context["covering"] == ["hwp_532"]
    prompts.answer(pid, by="alice", source="web", choice="hwp_532")
    assert optical_state.get_store().is_valid("excitation_waveplate", expect="hwp_532")


# ── a text slot ────────────────────────────────────────────────────────────────────────────


def test_a_text_slots_options_are_labelled_with_the_text_they_record():
    p = attest(["edge_filter"], options=[PromptOption("LP532", "Remove all filters"), PromptOption("LP633", "none")])
    pid = prompts.post(p)
    assert labels(p) == [("LP532", "LP532"), ("LP633", "LP633")]
    prompts.answer(pid, by="alice", source="web", choice="LP633")
    assert optical_state.get_store().get("edge_filter").value == "LP633"


# ── slots of one kind; units only on number slots ────────────────────────────────────────────


@pytest.mark.parametrize("slots", [["laser_area_safe", "edge_filter"], ["objective", "incident_polarizer_deg"],
                                   ["laser_area_safe", "objective"]])
def test_one_answer_is_not_written_to_slots_of_different_kinds(slots):
    """A prompt that names a laser-area flag next to an innocent-looking slot would sign both with one click."""
    with pytest.raises(prompts.PromptError, match="slots of one kind"):
        prompts.post(attest(slots))
    assert prompts.list_open() == []


@pytest.mark.parametrize("kw", [{"unit": "deg"}, {"number_min": 0}, {"number_max": 5}])
def test_unit_and_range_belong_to_number_slots(kw):
    with pytest.raises(prompts.PromptError, match="belong to number slots"):
        prompts.post(attest(["laser_area_safe"], **kw))
    with pytest.raises(prompts.PromptError, match="belong to number slots"):
        prompts.post(attest(["objective"], **kw))


def test_the_check_is_idempotent_and_leaves_other_prompts_alone():
    p = attest(["objective"], options=[PromptOption("10x", "x")])
    prompts.vet(p)
    once = (labels(p), p.severity, p.unit)
    prompts.vet(p)
    prompts.post(p)
    assert (labels(p), p.severity, p.unit) == once
    q = prompts.OperatorPrompt(kind="choice", title="Which sample?",
                               options=[PromptOption("a", "Sample A"), PromptOption("b", "Sample B")])
    prompts.vet(q)
    assert labels(q) == [("a", "Sample A"), ("b", "Sample B")], "only attest prompts are about a registry value"
    c = prompts.OperatorPrompt(kind="confirm", title="Fine?", unit="x")
    prompts.vet(c)


def test_a_slot_that_vanished_is_refused_when_posting():
    with pytest.raises(prompts.PromptError, match="unknown optical-state slot"):
        prompts.post(attest(["no_such_slot"]))


def test_the_registry_consulted_is_the_active_one(installed):
    """After service.install() that is the persistent store, with the slot definitions it loaded."""
    assert optical_state.get_store() is installed.store
    p = attest(["objective"], options=[PromptOption("10x", "x")])
    prompts.post(p)
    assert labels(p) == [("10x", installed.store.slot("objective").options[0]["label"])]
