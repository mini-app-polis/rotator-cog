# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) for this
repository. ADRs document significant architectural decisions, the
context around them, and their consequences.

## Format

Each ADR is a markdown file named `ADR-NNNN-short-slug.md` where `NNNN`
is a zero-padded four-digit sequence number starting at `0001`. This
matches the ecosystem-standards DOC-005 specification. An ADR in another
repository is cited with that repository's name first, as in
`ecosystem-standards ADR-0008`.

Each ADR uses three sections: **Context** (what forces motivated the
decision), **Decision** (what change is being made), and
**Consequences** (what becomes easier or harder).

## Index

- [ADR-0001: A Lambda worker for keys it can rotate alone; rotate.py for the rest](./ADR-0001-worker-beside-rotate-script.md)
- [ADR-0002: Hand a routine rotation over in two phases](./ADR-0002-two-phase-handover.md)
- [ADR-0003: One delivery per job, and how a job asked twice rotates once](./ADR-0003-one-delivery-and-duplicates.md)
- [ADR-0004: A daily sweep that retires, rotates one key and reports drift](./ADR-0004-daily-sweep.md)
- [ADR-0005: Empty a previous slot rather than delete it](./ADR-0005-previous-slot-emptied.md)
- [ADR-0006: Doppler delivers the GitHub copies; rotation writes Doppler alone](./ADR-0006-doppler-delivers-github-copies.md)
