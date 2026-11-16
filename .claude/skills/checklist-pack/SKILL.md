---
name: checklist-pack
description: Author or extend a YAML checklist pack (ISO 27001, ISO 20000, CMMI, or new ISO 9001 controls). Use when the user says "new pack", "add a control", "second standard", or names a standard to support.
---

# Checklist pack

A standard is data, not code (NFR-4, CR-5). The proof point in v2
demo is "new standard = new file, zero code changes". Every control must be
**record-based and reasoning-checkable**: judged from tickets, emails, and
change records. Infra facts (backups ran, patches applied) are out; they are
manual checklist items.

## Steps

1. **Schema.** The authoritative schema is `core/checklists.py` (`Control`,
   `PackMeta`, `ChecklistPack`); the header comment of
   `assistant/data/checklists/iso9001.yaml` shows the field intent. Read both.

2. **Pick controls** the synthetic corpus can actually evidence. For ISO 27001
   the fit is access-change approval, incident handling and lessons learned,
   change management, supplier/vendor communication, logging of security
   events. Aim for 5-7 controls in a subset pack; breadth is not the point.

3. **Write each control, own-worded.** No sentence lifted from a standard.
   The `sufficiency_rubric` is the product: enumerate (a), (b), (c) conditions,
   state what makes PARTIAL versus GAP, and name the NOT APPLICABLE condition
   explicitly (see `ISO9001-8.2.1-client-comm` for the pattern). `severity` is
   `major` or `minor`. `expected_evidence` names artifact types present in the corpus.

4. **Validate** with the loader, which catches duplicates, bad severities, and
   empty evidence lists:
   ```bash
   uv run python -c "from core.checklists import load_pack; p=load_pack('assistant/data/checklists/<pack>.yaml'); print(p.pack.id, len(p.controls))"
   ```

5. **Prove zero code changes.** Run `check_ticket` for one ticket against one
   new control through the existing tool path. If any Python needed editing to
   load the pack, that is a bug in the engine, fix the engine.

6. **Ground truth.** Label at least the planted-deficiency tickets against the
   new controls so the eval can score the pack. Follow the shape of
   `assistant/eval/ground_truth.yaml`; keep the ISO 9001 file untouched.

7. **Version and document.** Bump `pack.version` on any rubric edit to an
   existing pack (a verdict must be traceable to the criteria version). Mention
   the pack in `assistant/README.md` "What's here". Done when the loader passes,
   one live judgment runs, and the ground-truth file exists.
