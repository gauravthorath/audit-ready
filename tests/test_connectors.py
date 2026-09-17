"""Offline tests for the evidence connector layer (CR-3).

The live path needs a network and a token, so what is pinned here is
everything *around* it: the contract gate every source must pass, the
GitHub→contract mapping (fed recorded payloads, no client), the snapshot
writer, and the stubs. If these hold, a broken connector fails loudly at its
own boundary instead of writing a malformed corpus the judge then reasons over.
"""

from __future__ import annotations

import json

import pytest

from audit import mcp_evidence as me
from audit.connectors import (
    STUBS,
    ConnectorError,
    CsvFileSource,
    EvidenceRecords,
    JsonFileSource,
    blank_ticket,
    build_source,
    fill_ticket,
    find_linked_ids,
    snapshot,
    validate_records,
    validate_ticket,
)


# --------------------------------------------------------------------------
# The contract gate
# --------------------------------------------------------------------------


def test_a_blank_ticket_is_already_contract_valid():
    # The floor: every source starts from this, so it must pass on its own.
    validate_ticket(blank_ticket(ticket_id="T-1", created="2026-01-01T00:00:00Z"))


def test_a_missing_field_is_named_not_defaulted():
    ticket = blank_ticket(ticket_id="T-1", created="2026-01-01T00:00:00Z")
    del ticket["signoff"]
    with pytest.raises(ConnectorError, match="signoff"):
        validate_ticket(ticket)


@pytest.mark.parametrize(
    "field, value, needle",
    [
        ("type", "bug", "type"),               # GitHub's word, not the contract's
        ("priority", "P1", "priority"),        # ditto
        ("comments", "a string", "comments"),  # judge iterates this
        ("description", None, "description"),  # prompt formats this
        ("created", "", "created"),            # every record is dated
        ("ticket_id", "  ", "ticket_id"),
    ],
)
def test_wrong_shapes_are_refused_at_the_boundary(field, value, needle):
    ticket = blank_ticket(ticket_id="T-1", created="2026-01-01T00:00:00Z")
    ticket[field] = value
    with pytest.raises(ConnectorError, match=needle):
        validate_ticket(ticket)


def test_open_tickets_are_legal_closed_may_be_none():
    validate_ticket(
        blank_ticket(
            ticket_id="T-1", created="2026-01-01T00:00:00Z", status="open", closed=None
        )
    )


def test_a_comment_missing_its_author_is_refused():
    ticket = blank_ticket(ticket_id="T-1", created="2026-01-01T00:00:00Z")
    ticket["comments"] = [{"timestamp": "2026-01-01T00:00:00Z", "body": "hi"}]
    with pytest.raises(ConnectorError, match="comments\\[0\\]"):
        validate_ticket(ticket)


def test_duplicate_ticket_ids_are_refused():
    # Two records with one id means one silently wins in every downstream dict.
    t = blank_ticket(ticket_id="T-1", created="2026-01-01T00:00:00Z")
    with pytest.raises(ConnectorError, match="duplicate"):
        validate_records(EvidenceRecords(tickets=[t, dict(t)], source="test"))


def test_fill_ticket_keeps_fields_the_contract_never_named():
    # One synthetic ticket carries satisfaction_survey; dropping it would lose
    # evidence a control might ask for.
    filled = fill_ticket({"ticket_id": "T-1", "satisfaction_survey": {"score": 4}})
    assert filled["satisfaction_survey"] == {"score": 4}
    assert filled["comments"] == []


# --------------------------------------------------------------------------
# Cross-system links
# --------------------------------------------------------------------------


def test_linked_ids_are_read_out_of_prose():
    changes, emails = find_linked_ids(
        "fixed under CHG-3001", None, "confirmed to client in EML-2002 and EML-2002"
    )
    assert changes == ["CHG-3001"]
    assert emails == ["EML-2002"]  # deduped, order kept


def test_a_near_miss_is_not_a_link():
    assert find_linked_ids("CHG-", "XCHG-3001", "chg-3001") == ([], [])


# --------------------------------------------------------------------------
# GitHub → contract mapping (recorded payloads, no network)
# --------------------------------------------------------------------------


def test_mcp_text_blocks_are_unwrapped_to_json():
    assert me.unwrap([{"type": "text", "text": '{"a": 1}'}]) == {"a": 1}
    assert me.unwrap('{"a": 1}') == {"a": 1}
    assert me.unwrap({"a": 1}) == {"a": 1}


def test_labels_drive_type_and_priority_and_guesses_are_admitted():
    assert me.map_type(["bug"]) == ("incident", False)
    assert me.map_priority(["p1"]) == ("high", False)
    # No label the mapping knows: a default is used, and flagged as inferred.
    assert me.map_type(["needs-triage"]) == (me.DEFAULT_TYPE, True)
    assert me.map_priority([]) == (me.DEFAULT_PRIORITY, True)


def test_actor_fields_survive_both_shapes_github_sends():
    assert me.login_of({"login": "ada"}) == "ada"
    assert me.login_of("ada") == "ada"
    assert me.login_of(None) == ""


def test_issue_text_is_unescaped_at_the_boundary():
    assert me.text_of("I&#39;d like &amp; more") == "I'd like & more"


def _issue_detail():
    return {
        "number": 8616,
        "title": "Store.put() rejects TypedDict &amp; friends",
        "body": "Root cause traced to CHG-3001. Client told in EML-2002.",
        "state": "closed",
        "state_reason": "completed",
        "created_at": "2026-08-13T20:11:15Z",
        "closed_at": "2026-08-28T13:05:06Z",
        "closed_by": "maintainer-1",
        "user": {"login": "reporter-1"},
        "assignees": [{"login": "dev-1"}],
        "labels": [{"name": "bug"}, {"name": "p1"}],
    }


def test_a_recorded_issue_maps_to_a_contract_valid_ticket():
    comments = [
        {"user": {"login": "dev-1"}, "created_at": "2026-08-16T20:36:09Z", "body": "on it"}
    ]
    ticket, notes = me.map_issue(_issue_detail(), comments, owner="acme", repo="widgets")

    validate_ticket(ticket)  # the point of the whole mapping
    assert ticket["ticket_id"] == "GH-8616"
    assert (ticket["type"], ticket["priority"]) == ("incident", "high")
    assert ticket["title"] == "Store.put() rejects TypedDict & friends"
    assert ticket["reporter"] == "reporter-1"
    assert ticket["assignee"] == "dev-1"
    assert ticket["closed"] == "2026-08-28T13:05:06Z"
    assert ticket["comments"][0]["author"] == "dev-1"
    # Links are recovered from prose, since GitHub has no field for them.
    assert ticket["linked_changes"] == ["CHG-3001"]
    assert ticket["linked_emails"] == ["EML-2002"]
    assert notes == []  # nothing was guessed for this issue


def test_what_github_cannot_supply_is_reported_not_invented():
    detail = _issue_detail() | {"labels": [], "state": "open", "closed_at": None,
                                "state_reason": None, "assignees": []}
    ticket, notes = me.map_issue(detail, [], owner="acme", repo="widgets")

    validate_ticket(ticket)
    assert ticket["signoff"] is None          # no approval record exists there
    assert ticket["closed"] is None
    assert ticket["assignee"] is None
    assert "not resolved" in ticket["resolution"].lower()
    # And the guesses are on the record, one note per inferred field.
    assert len(notes) == 2
    assert any(n.startswith("type defaulted") for n in notes)
    assert any(n.startswith("priority defaulted") for n in notes)


# --------------------------------------------------------------------------
# File import (CR-3: "+ file import")
# --------------------------------------------------------------------------


def test_json_import_reads_a_corpus_directory(tmp_path):
    t = blank_ticket(ticket_id="T-1", created="2026-01-01T00:00:00Z")
    (tmp_path / "tickets.json").write_text(json.dumps([t]))
    (tmp_path / "changes.json").write_text(json.dumps([{"change_id": "CHG-1"}]))

    records = JsonFileSource(tmp_path).fetch()
    assert [x["ticket_id"] for x in records.tickets] == ["T-1"]
    assert records.changes == [{"change_id": "CHG-1"}]
    assert records.emails == []  # absent file is not an error


def test_csv_import_parses_list_cells_and_recovers_links(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text(
        "ticket_id,title,type,priority,status,created,closed,reporter,assignee,"
        "description,resolution,comments\n"
        'T-9,Late delivery,complaint,high,closed,2026-01-01T00:00:00Z,,client-a,,'
        '"Escalated, see CHG-3002",Credit issued,'
        '"[{""author"": ""a"", ""timestamp"": ""2026-01-02T00:00:00Z"", ""body"": ""noted""}]"\n'
    )
    records = CsvFileSource(path).fetch()
    ticket = records.tickets[0]
    validate_ticket(ticket)
    assert ticket["closed"] is None
    assert ticket["comments"][0]["author"] == "a"
    assert ticket["linked_changes"] == ["CHG-3002"]


def test_a_bad_csv_row_names_the_ticket_it_broke_on(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text(
        "ticket_id,title,type,priority,status,created,reporter,description,resolution\n"
        "T-9,Late,defect,high,closed,2026-01-01T00:00:00Z,client-a,x,y\n"
    )
    with pytest.raises(ConnectorError, match="T-9"):
        CsvFileSource(path).fetch()


# --------------------------------------------------------------------------
# Snapshot + provenance
# --------------------------------------------------------------------------


def test_snapshot_writes_the_layout_the_engine_already_reads(tmp_path):
    records = EvidenceRecords(
        tickets=[blank_ticket(ticket_id="T-1", created="2026-01-01T00:00:00Z",
                              description="under CHG-3001")],
        source="test:acme",
    )
    provenance = snapshot(records, tmp_path / "live")

    out = tmp_path / "live"
    assert json.loads((out / "tickets.json").read_text())[0]["ticket_id"] == "T-1"
    assert json.loads((out / "changes.json").read_text()) == []
    assert json.loads((out / "emails.json").read_text()) == []
    assert json.loads((out / "provenance.json").read_text())["source"] == "test:acme"
    assert provenance["counts"]["tickets"] == 1


def test_a_referenced_but_absent_record_is_a_reportable_gap(tmp_path):
    # The planted-deficiency case: the ticket cites an approval nobody filed.
    records = EvidenceRecords(
        tickets=[blank_ticket(ticket_id="T-1", created="2026-01-01T00:00:00Z",
                              linked_changes=["CHG-3099"], linked_emails=["EML-2099"])],
        source="test",
    )
    provenance = snapshot(records, tmp_path / "live")
    assert provenance["dangling_references"] == {
        "changes": ["CHG-3099"],
        "emails": ["EML-2099"],
    }


def test_borrowed_artifacts_are_stamped_as_borrowed(tmp_path):
    donor = tmp_path / "donor"
    donor.mkdir()
    (donor / "changes.json").write_text(json.dumps([{"change_id": "CHG-3001"}]))
    (donor / "emails.json").write_text(json.dumps([{"email_id": "EML-2002"}]))

    records = EvidenceRecords(
        tickets=[blank_ticket(ticket_id="T-1", created="2026-01-01T00:00:00Z")],
        source="github:acme/widgets",
    )
    provenance = snapshot(records, tmp_path / "live", artifacts_from=donor)

    # Recorded per file, so a reader can see which half of the corpus was live.
    assert provenance["artifacts_borrowed_from"] == {
        "changes.json": str(donor),
        "emails.json": str(donor),
    }
    assert provenance["counts"]["changes"] == 1
    # A reader of the report can tell live evidence from file-imported evidence.
    assert json.loads((tmp_path / "live" / "changes.json").read_text())[0][
        "change_id"
    ] == "CHG-3001"


def test_snapshot_refuses_to_write_a_malformed_corpus(tmp_path):
    bad = blank_ticket(ticket_id="T-1", created="2026-01-01T00:00:00Z")
    bad["type"] = "bug"
    with pytest.raises(ConnectorError):
        snapshot(EvidenceRecords(tickets=[bad], source="test"), tmp_path / "live")
    assert not (tmp_path / "live" / "tickets.json").exists()


# --------------------------------------------------------------------------
# The stubs (CR-3: "others stubbed")
# --------------------------------------------------------------------------


CR3_SOURCES = ("github", "jira", "linear", "json", "csv", "eml")


def test_every_source_named_in_cr3_is_built_not_stubbed():
    """CR-3: connector interface, live connectors, file import (CSV/JSON/.eml)."""
    stubbed = [name for name in CR3_SOURCES if name in STUBS]
    assert stubbed == [], f"a CR-3 source is still stubbed: {stubbed}"
    with pytest.raises(ConnectorError, match="eml"):
        build_source("sharepoint")  # the error lists eml among the real sources


def test_a_stub_names_what_has_to_exist_before_it_could_be_built():
    """A stub earns its place by being specific. A vague one is a TODO pretending to be design."""
    for name, stub in STUBS.items():
        message = f"{stub.name}: {stub.reason}"
        assert len(stub.reason) > 120, f"{name} has no real reason"
        with pytest.raises(NotImplementedError, match="stubbed"):
            stub.fetch()
        # it must say what the contract is, so the next person can fill it in
        assert "EvidenceRecords" in _stub_error(stub)


def _stub_error(stub) -> str:
    try:
        stub.fetch()
    except NotImplementedError as exc:
        return str(exc)
    raise AssertionError("a stub that does not raise is not a stub")


def test_the_teams_stub_names_its_prerequisite_rather_than_its_effort():
    """Teams is the source most likely to close the sign-off gap; it is blocked on consent."""
    teams = build_source("teams")
    assert "Entra" in teams.reason and "consent" in teams.reason
    assert "approved" in teams.reason  # why a channel is evidence at all


def test_an_unknown_source_lists_the_real_ones():
    with pytest.raises(ConnectorError, match="github"):
        build_source("sharepoint")
