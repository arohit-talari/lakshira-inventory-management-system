# Lakshira Inventory Management System

A terminal-based inventory, sales, and business-intelligence system built end-to-end for Lakshira Handwoven Weaves, a small handloom textile business — replacing manual spreadsheet editing with a guided, validated CLI, and feeding a full downstream data pipeline for warehousing and analytics.

This repo covers the inventory/sales system (IMS) and its reporting layer. It's one piece of a larger stack:

```
Google Sheets (system of record)
   │  gspread API
   ▼
IMS  ──────────────────────────────────────►  Generate Report (this repo)
 (this repo — 10 CLI operations)                 │  PDF + AI-written executive summary
   │                                              │  (Claude API)
   │  scheduled ETL                               ▼
   ▼                                          Emailed to stakeholders
AWS RDS (MySQL)
   │
   ▼
Tableau dashboards
```

Built almost entirely in collaboration with **Claude Code** — every design decision, bug fix, and test in this repo was driven through an actual multi-week working session with an AI pair-programmer, not a one-shot generation. The commit history and the testing section below reflect that iterative process directly.

## Why this exists

A production textile business was running entirely on a hand-edited Google Sheet: no validation, no audit trail, error-prone manual entry, and no way to answer basic questions like "what's our margin trending toward" without opening a spreadsheet and doing math by hand. This system is a lightweight, purpose-built alternative to a commercial ERP/CRM — most small businesses reach for QuickBooks, Zoho, or a Shopify+spreadsheet combo; this does the equivalent job (inventory lifecycle, sales pipeline, customer relationship history, financial reporting) shaped exactly around how this specific business actually operates, at a fraction of the integration cost.

## Tech stack

| Layer | Tool |
|---|---|
| System of record | Google Sheets, via `gspread` |
| CLI / business logic | Python (`questionary` for interactive prompts, `phonenumbers` for validated international phone entry) |
| Currency conversion | Live ECB historical exchange rates via the Frankfurter API |
| Reporting | `reportlab` (PDF generation), Claude API (AI-written executive summary) |
| Data warehouse | ETL pipeline → AWS RDS (MySQL) |
| Analytics / dashboards | Tableau, direct SQL queries |
| Scheduling | macOS `launchd` (automated monthly/quarterly/annual report generation) |
| Testing | `pytest`, `pexpect` (drives the real interactive CLI through a pty) |
| Spreadsheet/legacy interchange | Excel |

## The system: 10 operations

Add Inventory · Edit Inventory Details · Reprice a Unit · Discount Simulator · Manage Reservation · Record a Sale · Record Outstanding Payment · Cancel a Sale · Customer Insights · Generate Report

Every write operation follows the same guided shape: look up → validate eligibility → collect fields one at a time with plain-English error messages → full confirmation summary → explicit confirmation required → write → success message. See [ARCHITECTURE.md](ARCHITECTURE.md) for the data model, SKU generation logic, currency handling, and per-operation detail.

## Testing and quality assurance

This is the section I'd point a technical reviewer to first.

**Two-tier, 205-test automated suite:**
- **130 unit tests** — pure business logic (pricing math, date handling, report metric calculations), zero I/O, zero flakiness
- **75 integration tests** — drive the actual interactive CLI through a real pseudo-terminal (`pexpect`) against a live test-mode Google Sheet, covering all 10 operations end-to-end: happy paths, status-based rejections, validation errors, blank/malformed-data warnings, and cancellation paths

**Testing types represented:**
- **Functional** — every operation's core flow, verified against real sheet state after each write
- **Edge case** — comma-formatted currency values, below-cost pricing, blank cost/price cells, 100%-discount dead-ends, four-figure outstanding balances
- **Regression** — every bug found below has a dedicated test pinning the fix, so it can't silently reappear
- **UAT** — email delivery and generated PDF report content were verified directly by the business stakeholder against production data (the live Google Sheet *and* the independent MySQL warehouse), not just automated assertions

**Concurrency testing:** every write operation re-reads its target row immediately before committing and aborts if anything has changed since the operation started — verified live with two terminal sessions editing the same inventory unit simultaneously.

**Real bugs found and fixed through this process** (each has a regression test):
- A 100%-reproducible crash in the repricing flow (undefined variable reached on every call)
- A silent no-op path with no user feedback on one confirmation flow
- Six call sites that crashed on any comma-formatted currency value ≥ $1,000 (a locale-formatting bug invisible until tested against realistic data)
- A discount-removal flow that failed to re-check pricing validity after the discount was removed
- A payment-status flow that offered a mathematically impossible option under a specific discount condition

## Process improvement: Lean Six Sigma applied to software

The testing and bug-fixing effort behind this repo followed a DMAIC structure, applied to a codebase instead of a physical process:

- **Define** — the manual, hand-edited spreadsheet was the source of the defects being eliminated: no validation, no audit trail, silent data-entry errors, no repeatable reporting process.
- **Measure** — a 205-test suite (130 unit + 75 live-integration tests covering all 10 operations) established a real, repeatable baseline instead of relying on ad hoc manual spot-checks.
- **Analyze** — every failure was root-caused before being touched. A reproducible crash was traced to an undefined variable reached on every call path, not patched with a broad try/except; a metric mismatch between the report and the master sheet was traced to 10 specific rows missing one formula cell, not written off as noise.
- **Improve** — five confirmed defects fixed at the root cause (listed above). The aging-metric mismatch from the Analyze step was corrected two ways: the 10 affected rows were backfilled, *and* the new-inventory flow was changed to auto-copy every formula-driven column on creation, so the same class of drift can't recur on future rows — a fix aimed at the recurrence mechanism, not just the immediate symptom.
- **Control** — every fix shipped with a dedicated regression test pinning it in place, so a fixed defect can silently reappear without a test failing to catch it. The suite reruns before every change, functioning as this project's control mechanism in place of a physical control chart.

**Specific tools applied, not just DMAIC as a label:**

- **Poka-yoke (mistake-proofing)** — every constrained field (status, category, weave type, sales channel) is a validated pick-list, never free text. An entire class of data-entry defect is made structurally impossible, not just discouraged.
- **Standardized work** — all 10 operations follow the identical look-up → validate → collect → confirm → write pattern, so the system behaves predictably regardless of which operation is running.
- **Waste elimination (Muda)** — manual spreadsheet math and cross-referencing (motion/waiting waste) replaced by automated calculation; recurring data-entry defects (defect waste) eliminated by poka-yoke validation; a report that once required manually compiling numbers across tabs now generates in one command.
- **Root cause analysis** — every bug in this repo's history was traced to its actual origin before a fix was written, not just to a symptom.
- **Kaizen (continuous improvement)** — the system evolved through repeated, incremental audit → fix → test cycles across the project's history, rather than one large rewrite.

## Running it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in your own values
# place a Google service account key as credentials.json in this directory

python3 inventory.py
```

Run the test suite:

```bash
pytest tests/ -v                              # Tier 1 -- instant, no live dependencies
pytest tests/test_interactive_*.py -v -s       # Tier 2 -- needs TEST_SHEET_ID configured
```

## Repository structure

```
inventory.py              # the CLI -- all 10 operations
generate_report.py        # PDF report generation + AI executive summary
report_config.py          # reporting configuration (fonts, brand colors, secrets loading)
scheduler.py              # automated report scheduling (macOS launchd)
tests/                    # 205 tests across both tiers
ARCHITECTURE.md           # data model, SKU logic, currency handling, per-operation detail
```

---

*Real supplier and customer data are not included anywhere in this repo — the system's only data store is Google Sheets (and, downstream, the business's own MySQL warehouse), never this codebase. A few reference values (e.g. the supplier list) are seeded with placeholder data of the same shape as production for demonstration purposes.*
