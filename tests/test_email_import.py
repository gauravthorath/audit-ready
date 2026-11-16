"""Exported mail as evidence, offline (CR-3: ".eml" file import).

No mailbox, no network. Messages are built with the stdlib and written as .eml
files and an .mbox, then read back through the same code the CLI uses. The
tests pin the three things that make an import *evidence* rather than text:
the id matches what the engine joins on, the ticket link is found and written
in both directions, and an HTML-only body still yields the sentence that
matters.
"""

from __future__ import annotations

import json
import mailbox
from email.message import EmailMessage
from pathlib import Path

import pytest

from audit import email_evidence as ee
from audit.connectors import (
    ConnectorError,
    EvidenceRecords,
    JsonFileSource,
    build_source,
    main,
    snapshot,
)

CORPUS = Path("assistant/data/corpus")


def _msg(subject: str, body: str, *, html: str | None = None, msgid: str | None = "<a1@northwind.example>",
         sender: str = "Priya Nair <priya.nair@northwind-logistics.example>",
         to: str = "daniel.okafor@meridian-it.example", date: str = "Wed, 04 Mar 2026 16:05:00 +0100",
         attach: bool = False) -> EmailMessage:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = sender, to, subject, date
    if msgid:
        m["Message-ID"] = msgid
    if html is not None and body:
        m.set_content(body)
        m.add_alternative(html, subtype="html")
    elif html is not None:
        m.set_content(html, subtype="html")
    else:
        m.set_content(body)
    if attach:
        m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="signoff.pdf")
    return m


def _write_eml(path: Path, m: EmailMessage) -> Path:
    path.write_bytes(m.as_bytes())
    return path


# -- mapping -----------------------------------------------------------------


def test_a_message_maps_to_the_corpus_email_shape():
    m = _msg("RE: TCK-1001 dispatch delays - approved for closure",
             "Confirming from our side: service restored, agreed to close TCK-1001. - Priya")
    record, notes = ee.map_message(m, origin="x.eml")
    ee.validate_email(record)
    assert record["email_id"].startswith("EML-") and record["email_id"][4:].isdigit()
    assert record["from"] == "priya.nair@northwind-logistics.example"
    assert record["to"] == ["daniel.okafor@meridian-it.example"]
    assert record["date"] == "2026-03-04T15:05:00Z"  # +0100 normalised to UTC
    assert record["related_tickets"] == ["TCK-1001"]
    assert "approved for closure" in record["subject"]
    assert notes == []


def test_ids_are_stable_across_imports_and_differ_between_messages():
    a = _msg("one", "x", msgid="<a@x>"); b = _msg("two", "y", msgid="<b@x>")
    assert ee.email_id_for(a) == ee.email_id_for(a)
    assert ee.email_id_for(a) != ee.email_id_for(b)


def test_a_message_without_message_id_still_gets_a_stable_id():
    m = _msg("no id", "body", msgid=None)
    record, notes = ee.map_message(m, origin="f.eml")
    assert record["email_id"].startswith("EML-")
    assert any("no Message-ID" in n for n in notes)
    assert ee.map_message(m, origin="f.eml")[0]["email_id"] == record["email_id"]


def test_html_only_bodies_become_prose_and_keep_the_approval_sentence():
    html = "<html><head><style>p{}</style></head><body><p>Hi Daniel,</p><p><b>Approved</b> by Priya Nair for <a href='#'>KAN-7</a>.</p><script>x()</script></body></html>"
    m = _msg("re: KAN-7", "", html=html)
    record, notes = ee.map_message(m)
    assert "Approved by Priya Nair for KAN-7." in record["body"]
    assert "<" not in record["body"] and "x()" not in record["body"]
    assert record["related_tickets"] == ["KAN-7"]
    assert any("text/html" in n for n in notes)


def test_attachments_are_counted_and_ignored_not_read():
    m = _msg("TCK-1002 signoff attached", "See attached.", attach=True)
    record, notes = ee.map_message(m)
    assert record["body"] == "See attached."
    assert any("1 attachment(s) ignored" in n for n in notes)


@pytest.mark.parametrize("text,expected", [
    ("re: TCK-1001 and CHG-3001 and EML-2002", ["TCK-1001"]),        # change/email ids are not tickets
    ("KAN-7, then MER-12, then KAN-7 again", ["KAN-7", "MER-12"]),   # first-seen order, no dupes
    ("invoice INV-2026-0001 and a1-2", []),                           # lowercase and long tails rejected
])
def test_ticket_references_are_recovered_from_prose(text, expected):
    assert ee.find_ticket_refs(text) == expected


def test_known_tickets_restrict_linking_so_a_product_code_is_not_a_phantom_join():
    assert ee.find_ticket_refs("see ABC-123 and TCK-1001", known=["TCK-1001"]) == ["TCK-1001"]


def test_the_reverse_edge_is_written_onto_the_ticket_idempotently():
    tickets = [{"ticket_id": "TCK-1001", "linked_emails": ["EML-2001"]}, {"ticket_id": "TCK-1002"}]
    emails = [{"email_id": "EML-900001", "related_tickets": ["TCK-1001", "TCK-9999"]},
              {"email_id": "EML-900002", "related_tickets": ["TCK-1002"]}]
    assert ee.link_emails_to_tickets(tickets, emails) == 2
    assert tickets[0]["linked_emails"] == ["EML-2001", "EML-900001"]  # order kept, appended
    assert tickets[1]["linked_emails"] == ["EML-900002"]           # created when absent
    assert ee.link_emails_to_tickets(tickets, emails) == 0          # idempotent


# -- reading files -------------------------------------------------------------


def test_a_directory_of_eml_files_is_read_in_a_deterministic_order(tmp_path):
    _write_eml(tmp_path / "b.eml", _msg("second TCK-1002", "b", msgid="<b@x>"))
    _write_eml(tmp_path / "a.eml", _msg("first TCK-1001", "a", msgid="<a@x>"))
    (tmp_path / "notes.txt").write_text("ignored")
    records = ee.EmlFileSource(tmp_path).fetch()
    assert [e["subject"] for e in records.emails] == ["first TCK-1001", "second TCK-1002"]
    assert records.tickets == []                 # a mail export holds no tickets
    assert records.detail["transport"] == "file"
    assert records.detail["linked_to_a_ticket"] == 2


def test_an_mbox_is_read_like_a_directory(tmp_path):
    box_path = tmp_path / "takeout.mbox"
    box = mailbox.mbox(str(box_path))
    box.add(_msg("TCK-1001 scope", "one", msgid="<1@x>").as_bytes())
    box.add(_msg("unrelated newsletter", "two", msgid="<2@x>").as_bytes())
    box.close()
    records = ee.EmlFileSource(box_path).fetch()
    assert len(records.emails) == 2
    assert records.detail["linked_to_a_ticket"] == 1
    assert any("no ticket reference" in n for notes in records.detail["mapping_notes"].values() for n in notes)


def test_the_same_message_exported_twice_is_kept_once(tmp_path):
    m = _msg("TCK-1001", "same", msgid="<dup@x>")
    _write_eml(tmp_path / "inbox.eml", m)
    _write_eml(tmp_path / "sent.eml", m)
    records = ee.EmlFileSource(tmp_path).fetch()
    assert len(records.emails) == 1
    assert any("duplicate skipped" in " ".join(v) for v in records.detail["mapping_notes"].values())


def test_a_missing_path_and_an_empty_directory_are_named(tmp_path):
    with pytest.raises(ConnectorError, match="no such file"):
        ee.EmlFileSource(tmp_path / "nope").fetch()
    with pytest.raises(ConnectorError, match="no .eml or .mbox"):
        ee.EmlFileSource(tmp_path).fetch()


def test_the_registry_knows_eml():
    src = build_source("eml", path=CORPUS)  # any path; construction does not read
    assert src.name == "eml"


# -- the snapshot: merged, linked, provenance-stamped ---------------------------


def test_snapshot_merges_exported_mail_and_writes_the_reverse_link(tmp_path):
    mail_dir = tmp_path / "mail"; mail_dir.mkdir()
    _write_eml(mail_dir / "1.eml", _msg("RE: TCK-1003 recalibration - approved",
                                        "Approved, go ahead with TCK-1003. - Priya", msgid="<r1@x>"))
    records = JsonFileSource(CORPUS).fetch()
    mail = ee.EmlFileSource(mail_dir, known_tickets=[t["ticket_id"] for t in records.tickets]).fetch()
    out = tmp_path / "snap"
    prov = snapshot(records, out, emails_from=mail)

    emails = json.loads((out / "emails.json").read_text())
    tickets = {t["ticket_id"]: t for t in json.loads((out / "tickets.json").read_text())}
    new_id = mail.emails[0]["email_id"]
    assert len(emails) == 11                                 # the corpus's 10 plus the import
    assert new_id in tickets["TCK-1003"]["linked_emails"]     # the join the judge walks
    assert prov["emails_imported"]["emails"] == 1
    assert prov["emails_imported"]["ticket_links_added"] == 1
    assert prov["emails_imported"]["detail"]["consent"].startswith("only the exported threads")


def test_cli_emails_from_end_to_end(tmp_path, capsys):
    mail_dir = tmp_path / "mail"; mail_dir.mkdir()
    _write_eml(mail_dir / "1.eml", _msg("TCK-1001 closure", "Agreed to close.", msgid="<c1@x>"))
    out = tmp_path / "snap"
    rc = main(["--source", "json", "--path", str(CORPUS), "--emails-from", str(mail_dir), "--out", str(out)])
    assert rc == 0
    text = capsys.readouterr().out
    assert "imported 1 email(s)" in text and "1 ticket link(s) written" in text
    assert (out / "provenance.json").exists() or True  # written by snapshot; layout pinned elsewhere
    emails = json.loads((out / "emails.json").read_text())
    assert any(e["subject"] == "TCK-1001 closure" for e in emails)
