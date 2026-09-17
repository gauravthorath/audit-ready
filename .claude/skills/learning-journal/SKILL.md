---
name: learning-journal
description: Write or update a LEARNING.md entry for a build step. Use when a step finishes, the user says "journal", "LEARNING.md", "write up what we learned", or asks to add reviewer Q&A.
---

# Learning journal

`LEARNING.md` is Gaurav's gitignored study record and the reviewer-defense study
sheet. Entries are written for him to re-learn from, so they explain the
decision and the rejected alternatives, not just the code.

## Steps

1. Read the last `## Step N` heading and its body to match the number (N+1),
   the date format `(YYYY-MM-DD)`, and the voice. The template sits at the top
   of the file.

2. Write the entry with every template field filled:
   - **What we built** — files, functions, deps added.
   - **Why (the design decision)** — bold lead per decision, the requirement id
     it serves, the failure it prevents.
   - **How it works** — the mechanism a reader could re-implement from.
   - **Alternatives we didn't pick & why** — at least two, named.
   - **Concepts to go deeper on** — 3–4 named ideas, essays, or library features.
   - **My own notes/questions:** … (leave the ellipsis; that section is his).

3. Bugs found during the step get their own paragraph with the lesson stated
   as a rule (see Step 15 for the shape).

4. Insert the entry **after the last step and before** the
   `# Reviewer Q&A` section. If the step gives a new likely reviewer question,
   add a Q/A pair to the matching Q&A subsection, grounded in a file, commit,
   or eval number.

5. Done when the entry is placed, the heading number is consecutive, and no
   template field is empty other than "My own notes/questions".
