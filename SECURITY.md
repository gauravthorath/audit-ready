# Security Policy

## Supported versions

This repository is maintained as a portfolio / research codebase on `main`.
Security fixes are applied to `main` only.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Prefer one of:

1. **GitHub Security Advisories** - use *Report a vulnerability* on the
   repository’s Security tab (private disclosure to the maintainer).
2. **Email** - [gauravjobs25@gmail.com](mailto:gauravjobs25@gmail.com) with
   subject `AuditReady security`.

Include steps to reproduce, impact, and any suggested fix if you have one.
You should hear back within a few days; complex issues may take longer.

## Scope notes

- Retrieved records are treated as **untrusted input** (`core/guardrails.py`).
  The synthetic corpus includes a planted injection case (TCK-1010); please keep
  that path exercised rather than removing it.
- The product drafts findings for a human to verify. It does not certify
  compliance; treat model output as advisory.
- Self-hosted deployments should keep LLM credentials and connector tokens out
  of the repository (use `.env` / your secret store; never commit them).
