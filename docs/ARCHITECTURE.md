# Operational System Architecture

This document describes how the Inventory Management System is built and why: the data model, the pricing and currency logic, and the design decisions behind each operation. For setup and usage, see [Inventory Management System](IMS.md); for the full engagement lifecycle this system is one stage of, see the main [README](../README.md).

---

## 1. What This System Is

A terminal-based inventory, sales, and reporting system built for Lakshira Handwoven Weaves, a small handwoven-textile business, backed by a Google Sheet acting as the System of Record. It replaces manual spreadsheet editing for anyone who needs to touch inventory data, reducing data-entry errors, enforcing business rules a spreadsheet has no way to apply on its own, and giving the business owner a reporting layer a spreadsheet alone can't produce.

It's not a generalized inventory platform. It's this business's own lightweight alternative to a commercial Enterprise Resource Planning (ERP) / Customer Relationship Management (CRM) system, shaped specifically around its operations: markup-based pricing derived from a live foreign-exchange rate, a per-category SKU numbering scheme, and sales that close across Instagram, WhatsApp, in-person exhibitions, and (eventually) a Shopify storefront.

## 2. Who Uses It

Designed and directed by **Arohit Talari**, the business's analyst and consultant throughout this engagement, with **Claude Code** as the execution layer that translated that direction into working code (see the main [README](../README.md) for the full discovery-through-delivery lifecycle this system is one stage of). The system itself serves:

- **The business owner**: the system's primary daily user, non-technical, runs sourcing, pricing, and sales through the Command-Line Interface (CLI)
- **A non-technical staff user**: runs day-to-day retail operations (customer-facing sales via social/messaging channels), needs the CLI to guide them step by step
- **Future employees**: unknown technical background, every prompt has to explain itself

Building the system around the constraint of a non-technical primary user with little-to-no onboarding is what drove most of the User Experience (UX) decisions below: numbered menus instead of free text wherever a value is constrained, a full confirmation summary before every write, plain-English errors instead of raw exceptions, and validated pick-lists instead of type-anything fields.

## 3. Data Model

The master sheet has 39 columns. A few worth calling out:

| Column | Entry method |
|---|---|
| SKU | Auto-generated, never user-entered (see §4) |
| Category Code | Auto-assigned from weave type selection, a 1:1 mapping, enforced |
| Total Cost (USD) | Derived: Total Cost (INR) ÷ ECB rate on acquisition date |
| Selling Price (USD/INR) | User provides one, the other two derive from it (see §5) |
| Gross Profit / Markup % / Margin % | All derived, never entered directly |
| Days to Sell / Days in Inventory / Aging Bucket / Dead Stock Flag | Formula-driven in the sheet itself; the script reads these, never writes them |
| Status | Constrained to a fixed set (`Available`, `Reserved`, `Sold`, `Sold - Partial Payment`, `Unassigned`), never free text |

The `Unassigned` status is a deliberate design choice: a SKU can be generated in sequence with no physical unit behind it yet, preserving the numbering sequence without corrupting cost and pricing columns with placeholder data.

## 4. SKU Generation

**Format:** `LAH-[CATEGORY CODE][SERIAL]`, e.g. `LAH-KKVSV108`.

- Serial numbers are per-category, starting at 100, no zero-padding
- Before generating a new serial, the system checks for existing `Unassigned` rows in that category and offers to reuse one first, new inventory doesn't always mean a new row
- The generated SKU is always shown to the user for confirmation before anything is written

## 5. Pricing and Currency Logic

All INR/USD conversion uses the historical European Central Bank (ECB) exchange rate for the unit's *acquisition date*, fetched live via the [Frankfurter API](https://www.frankfurter.app/), not today's rate and not a fixed constant. This matters for margin accuracy: a unit acquired eight months ago should have its cost basis computed at the exchange rate that actually applied then.

Two pricing paths, user's choice:
- **By markup %**: Selling Price (INR) = Total Cost (INR) × (1 + markup), Selling Price (USD) derives from that
- **By target USD price**: Selling Price (INR) derives from that instead

Either path recalculates Gross Profit, Markup %, and Margin % automatically, and warns (without blocking) if the result falls below a configured margin threshold. A low-margin sale is sometimes the right call, but it should never happen by accident.

## 6. The Ten Operations

1. **Add Inventory**: new SKU, cost and pricing entry, ECB-rate-derived cost basis
2. **Edit Inventory Details**: amend an existing unit's fields, with cost-field edits triggering a full pricing recalculation
3. **Reprice a Unit**: change an unsold unit's selling price via either pricing path
4. **Discount Simulator**: model a discount's effect on margin before committing to it, no write to the sheet
5. **Manage Reservation**: place/release a hold for a specific customer, with overdue-reservation surfacing
6. **Record a Sale**: the most complex flow, customer capture and matching, discount handling, payment status, below-cost gating
7. **Record Outstanding Payment**: apply a payment against a partially-paid sale, track running balance
8. **Cancel a Sale**: reverse a sale, restore the unit to `Available`, track any pending refund
9. **Customer Insights**: lifetime and period-scoped purchase history, spend, and outstanding balance per customer
10. **Generate Report**: a full PDF business-intelligence report (revenue, margin, inventory turnover, customer retention, aging and dead-stock, accounts receivable), with a structured Claude API executive summary and optional email delivery

Every write operation follows the same shape: look up, validate status/eligibility, collect fields one at a time, show a full confirmation summary, require explicit confirmation, write, confirm success. Nothing is written to the sheet without that final confirmation step.

## 7. Concurrency Safety

More than one person can touch the same record at once: two staff members, or the same person across two sessions, could act on the same inventory unit at the same time. Without this safeguard, a system would let the second write silently overwrite the first, losing a price change, a payment, or a status update with no way to know it happened, so this was scoped as a hard requirement, not an edge case to accept. Every write-path operation re-reads the target row immediately before committing and compares it against the state it started from; if anything relevant changed in the meantime (a status flip, a cost edit, a payment recorded by someone else), the write is rejected with a clear explanation instead of silently overwriting it. Verified directly: two sessions editing the same unit at once, with the second write correctly rejected once the first one's change had landed.

## 8. Test/Live Mode

A single `MODE` variable at the top of `inventory.py` switches the entire system between a test sheet and the production sheet: same code path, different destination. The CLI displays a clear banner on startup so it's always clear which mode is active. Sheet IDs are supplied via environment variables, not hardcoded (see `.env.example`).

## 9. What the System Deliberately Never Does

- Writes to the three formula-driven sheet columns (Margin Distribution, Aging Bucket, Dead Stock Flag)
- Accepts free text for any status/category/channel field, always a validated pick-list
- Lets a user type a SKU or category code by hand
- Overwrites the append-only note columns; every note action appends, never replaces
- Assigns a category code that's already mapped to a different weave type
- Sends customer Personally Identifiable Information (PII) through anything other than the sheet API itself: no data lake, no analytics Software Development Kit (SDK), no third-party storage beyond the sheet and the business's own MySQL warehouse
