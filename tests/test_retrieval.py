"""Unit tests for core/retrieval.py - the chunking layer.

Embedding and Chroma search need models and a store (scripts/
smoke_retrieval.py); chunking is pure data transformation, and it carries
two contracts worth pinning down:

1. Deterministic chunk ids ("TCK-1001#header", "TCK-1001#c0", "EML-2001") -
   citations reference these, so an id scheme change silently breaks every
   stored citation and upsert-dedup on rebuild.
2. Chroma metadata must be scalar-only (str/int/float/bool) - a list slips
   through as an ingest-time crash, so we catch it at the unit level.
"""

import json

from core.retrieval import Chunk, _change_chunk, _email_chunk, _ticket_chunks, build_chunks


def make_ticket(**overrides) -> dict:
    """Minimal valid ticket; tests override single fields to probe one
    behaviour each (same one-change-per-test idea as the checklist tests)."""
    ticket = {
        "ticket_id": "TCK-9001",
        "title": "Printer on fire",
        "type": "incident",
        "priority": "high",
        "status": "closed",
        "created": "2026-03-01T09:00:00Z",
        "closed": "2026-03-02T17:00:00Z",
        "reporter": "a.user",
        "assignee": "b.engineer",
        "description": "The office printer caught fire.",
        "resolution": "Extinguished and replaced.",
        "signoff": {
            "approver": "M. Chen",
            "role": "service manager",
            "timestamp": "2026-03-02T17:30:00Z",
        },
        "comments": [
            {"author": "b.engineer", "timestamp": "2026-03-01T10:00:00Z", "body": "On it."},
            {"author": "a.user", "timestamp": "2026-03-02T16:00:00Z", "body": "Confirmed fixed."},
        ],
    }
    ticket.update(overrides)
    return ticket


# ---------------------------------------------------------------------------
# Ticket chunking
# ---------------------------------------------------------------------------


def test_ticket_yields_header_plus_one_chunk_per_comment():
    chunks = _ticket_chunks(make_ticket())
    assert [c.chunk_id for c in chunks] == ["TCK-9001#header", "TCK-9001#c0", "TCK-9001#c1"]


def test_chunk_ids_are_deterministic_across_reruns():
    # Same input, same ids - the property that keeps citations stable and
    # makes re-ingestion an upsert instead of a duplication.
    a = [c.chunk_id for c in _ticket_chunks(make_ticket())]
    b = [c.chunk_id for c in _ticket_chunks(make_ticket())]
    assert a == b


def test_header_carries_ticket_level_facts():
    header = _ticket_chunks(make_ticket())[0]
    assert "TCK-9001" in header.text
    assert "approved by M. Chen" in header.text  # signoff - audit-critical
    assert header.metadata["doc_type"] == "ticket"
    assert header.metadata["ticket_id"] == "TCK-9001"


def test_missing_signoff_is_stated_not_omitted():
    # "signoff: none recorded" must appear as TEXT: the absence of a signoff
    # is exactly what gap-hunting queries need to retrieve. An omitted line
    # would make the deficiency invisible to semantic search.
    header = _ticket_chunks(make_ticket(signoff=None))[0]
    assert "signoff: none recorded" in header.text


def test_comment_chunk_carries_author_body_and_provenance():
    comment = _ticket_chunks(make_ticket())[1]
    assert "b.engineer" in comment.text
    assert "On it." in comment.text
    assert comment.metadata["doc_type"] == "ticket_comment"
    assert comment.metadata["doc_id"] == "TCK-9001"


def test_ticket_without_comments_yields_only_header():
    chunks = _ticket_chunks(make_ticket(comments=[]))
    assert len(chunks) == 1


# ---------------------------------------------------------------------------
# Email / change chunking
# ---------------------------------------------------------------------------


def test_email_becomes_one_chunk_with_ticket_join():
    chunk = _email_chunk(
        {
            "email_id": "EML-9101",
            "from": "client@example.com",
            "to": ["pm@meridian-it.example"],
            "date": "2026-03-02",
            "subject": "Approval to close",
            "body": "We approve closure of the incident.",
            "related_tickets": ["TCK-9001"],
        }
    )
    assert chunk.chunk_id == "EML-9101"
    assert "We approve closure" in chunk.text
    # related_tickets joins the email to its ticket - but as a comma-joined
    # STRING, because Chroma metadata cannot hold lists.
    assert chunk.metadata["ticket_id"] == "TCK-9001"


def test_change_without_approver_states_the_absence():
    chunk = _change_chunk(
        {
            "change_id": "CHG-9201",
            "title": "Emergency hotfix",
            "status": "implemented",
            "approver": None,
            "implemented": "2026-03-03",
            "summary": "Deployed without approval.",
            "related_tickets": [],
        }
    )
    assert chunk.chunk_id == "CHG-9201"
    assert "no approval recorded" in chunk.text  # the deficiency, searchable
    assert chunk.metadata["ticket_id"] == "none"


def test_all_metadata_values_are_chroma_safe_scalars():
    chunks = _ticket_chunks(make_ticket())
    for chunk in chunks:
        for key, value in chunk.metadata.items():
            assert isinstance(value, (str, int, float, bool)), (
                f"{chunk.chunk_id} metadata[{key!r}] is {type(value).__name__}; "
                "Chroma only accepts scalar metadata values"
            )


# ---------------------------------------------------------------------------
# build_chunks - whole-corpus assembly from files
# ---------------------------------------------------------------------------


def test_build_chunks_reads_all_three_corpus_files(tmp_path):
    (tmp_path / "tickets.json").write_text(json.dumps([make_ticket()]))
    (tmp_path / "emails.json").write_text(
        json.dumps(
            [
                {
                    "email_id": "EML-9101",
                    "from": "a@x.example",
                    "to": ["b@x.example"],
                    "date": "2026-03-02",
                    "subject": "s",
                    "body": "b",
                }
            ]
        )
    )
    (tmp_path / "changes.json").write_text(
        json.dumps(
            [
                {
                    "change_id": "CHG-9201",
                    "title": "t",
                    "status": "implemented",
                    "summary": "s",
                }
            ]
        )
    )

    chunks = build_chunks(tmp_path)

    # 1 ticket (header + 2 comments) + 1 email + 1 change = 5 chunks
    assert len(chunks) == 5
    assert all(isinstance(c, Chunk) for c in chunks)
    ids = {c.chunk_id for c in chunks}
    assert {"TCK-9001#header", "EML-9101", "CHG-9201"} <= ids
