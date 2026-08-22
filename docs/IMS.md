<h1 align="center">Lakshira's Inventory Management System</h1>

*Part of the [Lakshira engagement](../README.md). This is the deep dive on the operational-system stage specifically; refer to the main README for the full lifecycle: discovery, data foundation, warehouse, BI, and this system.*

The Inventory Management System (IMS) is a terminal-based Command-Line Interface (CLI) for inventory, sales, and business intelligence at Lakshira Handwoven Weaves, a small, rapidly growing handloom textile business, replacing repetitive manual spreadsheet input and editing with a guided, validated interface built for a non-technical user.

Designed and directed by Arohit Talari: every design decision, bug fix, and test in this repo was specified and validated across a multi-week working session, with **Claude Code** as the execution layer translating that direction into working code, not a one-shot generation. The commit history and the testing section below reflect that iterative process directly.

## Why this exists

Lakshira was running entirely on a hand-edited spreadsheet: no validation, no audit trail, error-prone manual entry, and no way to answer basic questions like "what's our margin trending toward?" without opening a spreadsheet and doing the math by hand. The IMS is a lightweight, purpose-built alternative to a commercial Enterprise Resource Planning (ERP) / Customer Relationship Management (CRM) system. Most small businesses reach for paid software: QuickBooks, Zoho, or an integrated ERP/CRM tied to their Shopify storefront; the IMS does the equivalent (inventory lifecycle, end-to-end sales workflow, customer insights, financial reporting) shaped around how this specific business operates, at a fraction of the integration cost.

## Tech stack

| Layer | Tool |
|---|---|
| System of Record | Google Sheets, via `gspread` |
| CLI / Business Logic | Python (`questionary` for interactive prompts, `phonenumbers` for validated international phone entry) |
| Currency conversion | Live European Central Bank (ECB) historical exchange rates via the Frankfurter API |
| Reporting | `reportlab` (PDF generation), Claude API (executive summary grounded in brand context and stakeholder-discovery findings, injected into the prompt each run) |
| Scheduling | macOS `launchd` (automated monthly/quarterly/annual report generation) |
| Testing | `pytest`, `pexpect` (drives the interactive CLI through a pseudo-terminal, or pty) |

This is the operational-system stage's own stack. The data warehouse (MySQL/AWS RDS) and BI layer (Tableau) that this system feeds are covered in the main [README](../README.md), not duplicated here.

## The Ten Operations

1. **Add Inventory**
2. **Edit Inventory Details**
3. **Reprice a Unit**
4. **Discount Simulator**
5. **Manage Reservation**
6. **Record a Sale**
7. **Record Outstanding Payment**
8. **Cancel a Sale**
9. **Customer Insights**
10. **Generate Report**

Every write operation follows the same guided shape: look up → validate status/eligibility → collect fields one at a time → show a full confirmation summary → require explicit confirmation → write → confirm success. Errors surface in plain English at the point of entry, not after the fact. See [Architecture](ARCHITECTURE.md) for the data model, SKU generation logic, currency handling, and per-operation detail.

## Validation and Testing

Before this system could go live and be trusted with real customer and financial data, it had to be rigorously tested to ensure it solved the business's core problem. Architecting and designing all the operations and business logic the system would depend on wasn't the bar. Validation was run through a structured UAT (User Acceptance Testing) process, which surfaced 355 defects against business workflows and stakeholder-observed scenarios, each classified by business risk rather than technical severity (23 assessed as Critical), and each traced back to a specific requirement or a specific gap in how the manual process previously worked. That process is what transformed a working script that could theoretically handle the business's workflows into a system the stakeholder could actually depend on for day-to-day operations.

**A two-tier, 205-test automated regression suite locks each resolved defect in place:**
- **130 tests** validating core business logic (pricing calculations, date/aging rules, report metrics) in isolation
- **75 tests** driving the full guided workflow end-to-end against a live test environment, covering all 10 operations: correct-path completion, appropriate rejection of invalid states, validation-error handling, and cancellation paths

That validation effort covered four distinct kinds of ground:
- **Functional**: every operation's core flow, verified against data state after each write
- **Edge case / real-world data conditions**: currency values formatted the way the business actually enters them, below-cost pricing, incomplete cost/price data, discount edge cases, four-figure outstanding balances
- **Regression**: every defect below is locked behind a dedicated test, so a resolved issue can't silently reappear
- **Stakeholder UAT**: email delivery and generated report content were verified directly by the business owner against production data, both the live system of record and the independent data warehouse, beyond automated checks alone

One specific risk that discipline caught: because more than one person can act on the same inventory record at once, every write was required to detect and reject a conflicting concurrent change rather than silently overwrite it, verified directly with two simultaneous sessions attempting to edit the same unit. See [Architecture](ARCHITECTURE.md#7-concurrency-safety) for how this is actually implemented.

**Defects this process surfaced, each now closed with a permanent regression test:**
- A repricing workflow that failed on every attempt, traced to a business-logic gap in how one pricing path was handled
- A confirmation step that completed with no feedback to the user, masking whether the action had actually happened
- A payment-tracking flow that broke on realistic transaction amounts (any outstanding balance of $1,000 or more), a defect invisible until tested against real business scale
- A discount-removal flow that didn't re-validate pricing after the discount was reversed
- A payment-status option offered in a scenario where it was mathematically impossible to fulfill

## Process improvement: Lean Six Sigma applied to defect management

The validation effort above was run as a structured DMAIC cycle, the same framework used to drive process improvement on the business side of this engagement, applied here to closing the gap between the manual process and the delivered system:

- **Define**: the manual, hand-edited spreadsheet was the source of the defects being eliminated: no validation, no audit trail, silent data-entry errors, no repeatable reporting process.
- **Measure**: 355 defects identified and prioritized by business risk (23 Critical), against a 205-test suite establishing a repeatable baseline instead of ad hoc spot-checks.
- **Analyze**: every defect was root-caused against the actual business workflow it broke, never just patched at the symptom. A reporting mismatch, for example, was traced back to specific incomplete source records rather than written off as noise.
- **Improve**: each defect resolved at its root cause, with the underlying process changed so the same class of issue can't recur. Fixing the immediate instance alone wasn't the goal.
- **Control**: every fix locked behind a permanent regression test, re-run before any future change, functioning as the control mechanism in place of a physical control chart.

**Specific Lean Six Sigma tools applied here, beyond DMAIC as a label:**

- **Poka-yoke (mistake-proofing)**: every constrained business field (status, category, weave type, sales channel) is a validated pick-list, never free text. An entire class of data-entry defect is made structurally impossible, no longer just discouraged.
- **Standardized work**: all 10 operations follow an identical look-up → validate → collect → confirm → commit pattern, so the system behaves predictably for a non-technical user regardless of which task they're performing.
- **Waste elimination (Muda)**: manual cross-referencing and calculation (motion/waiting waste) replaced by automation; recurring data-entry defects (defect waste) prevented at the point of entry; a report that once required manually compiling numbers across tabs now generates on demand.
- **Root cause analysis**: every defect traced to its actual origin in the business process before a fix was specified, never just its symptom.
- **Kaizen (continuous improvement)**: the system evolved through repeated audit → fix → validate cycles across the engagement rather than one large rewrite.

The implementation itself, how each fix was written into the codebase, was directed AI-assisted development through Claude Code, reviewed and validated at every step against the criteria above; see the repository code and test suite for that layer directly.

## Running it

All commands run from the repo root.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cd inventory_management_system
cp .env.example .env   # fill in your own values
# place a Google service account key as credentials.json in this directory
cd ..

python3 inventory_management_system/inventory.py
```

Run the test suite:

```bash
pytest tests/ -v                              # Tier 1 -- instant, no live dependencies
pytest tests/test_interactive_*.py -v -s       # Tier 2 -- needs TEST_SHEET_ID configured
```

## Repository structure

```
inventory_management_system/
  inventory.py             # the CLI -- all 10 operations
  generate_report.py       # PDF report generation + AI executive summary
  report_config.py         # reporting configuration (fonts, brand colors, secrets loading)
  scheduler.py             # automated report scheduling (macOS launchd)
  assets/                  # fonts and logo used by generated PDF reports
  .env.example             # template for the .env this system reads at runtime
tests/                     # 205 tests across both tiers
docs/
  ARCHITECTURE.md          # data model, SKU logic, currency handling, per-operation detail
  STAKEHOLDER_DISCOVERY.md # the requirements-gathering framework that shaped every decision above
```

---

*Note: supplier and customer data are not included anywhere in this repo. The system's only data store is Google Sheets (and, downstream, the business's own MySQL warehouse), never this codebase. Specific business findings and figures from the engagement are documented in a private report for the client instead, standard confidentiality practice for an operating business, not a reflection on the work or its results. A few reference values (e.g. the supplier list) are seeded with placeholder data of the same shape as production for demonstration purposes.*
