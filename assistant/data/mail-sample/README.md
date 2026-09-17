# mail-sample — exported correspondence as evidence

Three synthetic `.eml` files (Meridian IT / Northwind Logistics, fictional) that
reference tickets in `../corpus`. They exist so the email import can be shown
from a fresh clone without anyone's real mailbox:

```bash
uv run python audit/connectors.py --source json --path assistant/data/corpus \
    --emails-from assistant/data/mail-sample --out assistant/data/live-mail
```

Any provider's export works the same way — Gmail Takeout (`.mbox`), Outlook and
Apple Mail (`.eml`), Thunderbird (both). The human chooses which threads to
export; nothing is fetched from a mailbox. See `audit/email_evidence.py`.
