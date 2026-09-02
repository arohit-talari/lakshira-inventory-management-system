# Business Intelligence Architecture

This document describes how the Business Intelligence layer is built and why: the data source structure, the validation methodology behind every figure on it, the synthetic-data approach behind the sanitized public demo, and the calculation architecture behind its most complex metrics. For the design reasoning behind what's actually on the dashboard, why these views, why these KPIs, see [Business Intelligence](BUSINESS_INTELLIGENCE.md); for the full engagement lifecycle this stage is one part of, see the main [README](../README.md).

---

## 1. Data Model and Source Structure

The Tableau data source is built on Relationships, not Joins, connecting the inventory and transaction tables exported from the MySQL warehouse; customer details are already flattened into the transaction table itself, not a separate connection.

This distinction has real, practical consequences. A join permanently merges two tables into one fixed shape at the moment it's defined; every worksheet built afterward queries that same merged table regardless of what it actually needs, and if the tables don't share a clean one-to-one relationship, that fixed merge can silently duplicate rows or drop them entirely, with no error to flag it. A relationship keeps the tables logically separate and lets Tableau generate the appropriate join fresh, at query time, based on exactly which fields a given worksheet is actually using, correctly matched to that worksheet's own granularity.

That flexibility comes with its own real pitfall, one worth naming directly rather than glossing over. Because the query is regenerated per worksheet, the same calculated field can behave differently depending on which other dimensions are in the view. A field counting a customer's purchases, for example, correctly evaluated per-customer when Customer Name was the worksheet's dimension, but silently re-evaluated per-state when Geography was the dimension instead, same field, same formula, different result, because the surrounding context changed what Tableau decided to query. Every calculated field on this dashboard was checked against that possibility directly, not assumed safe by default.

## 2. Validation Methodology

No KPI on this dashboard was trusted because Tableau displayed it. Every figure was independently recomputed against the underlying data, using Python scripts run directly against the same source tables, before being considered correct, and that process caught real, non-obvious bugs that a visual read of the dashboard alone would have missed:

- A cached extract that continued to display outdated figures after the underlying data source had already been refreshed, until a full reconnect was forced.
- A filter setting that had silently inherited a "top 5 customers" scope from a different worksheet it had been duplicated from, quietly narrowing an unrelated chart down to a handful of customers instead of the full base.
- A field read in as text rather than a number after a data source reconnect, which changed a numeric comparison into an alphabetical one and produced results that looked plausible at a glance but were structurally wrong.

The discipline is the same one applied throughout the rest of this engagement: verify against the source of truth directly, don't assume a tool's output is correct just because nothing visibly broke.

## 3. The Sanitized Demo and Synthetic-Data Methodology

Real customer, supplier, and financial data are not included anywhere in this repository. A separate, fully synthetic dataset, generated with the same column structure and business logic as the real system, powers a sanitized copy of this workbook for public demonstration, preserving how the dashboard actually behaves without exposing a single real figure.

Building a synthetic dataset that reads as genuine, rather than obviously generated, took real methodological care, not just randomized numbers inside a plausible range:

- **Revenue and profit vary organically month to month**, not clipped flat against a ceiling, using a smoothed random walk with occasional shock months rather than independent per-month noise, which produces the kind of uneven, clustered volatility real sales actually show instead of an artificially even oscillation.
- **Customer revenue concentration, supplier performance, and collection sell-through rates are deliberately tuned**, not left to fall out of pure randomness, so the synthetic business shows the same kind of believable structure a real one does: a few standout customers and suppliers, a real spread from strong to weak performers, not a flat, suspiciously uniform distribution across the board.
- **Pricing escalates over time** rather than being applied at one flat tier across the whole history, since the dataset is meant to represent a business just arriving at a more premium market position, not one that's already been there for years.

The synthetic dataset was reviewed the same way the real one was, cross-checked against expectations, examined for the kinds of statistical tells (a metronomic sawtooth pattern in revenue, catastrophic outlier months, uniform color tiers with no real spread) that would read as generated rather than observed, and corrected wherever they showed up.

## 4. Calculation Architecture

Not every number on this dashboard reduces to a SUM or COUNT. Three calculations below required real engineering, chosen because each represents a distinct Tableau calculation model rather than a variation on the same technique:

- **Sales Consistency (Swing)** is a five-field chain, not a single formula: each period's max and min monthly units, each period's own average, a swing normalized against that period's own average (so a business that's simply grown larger between periods isn't penalized with an inflated swing purely from scale), and a final comparison between the current and prior period's normalized swing.
- **Repeat Customers %** is a nested FIXED Level of Detail (LOD) expression: `{FIXED : COUNTD(repeat customers)} / {FIXED : COUNTD(all customers)}`, where "repeat" is itself a separate FIXED expression flagging any customer with more than one transaction. Both levels compute at the customer level regardless of whatever else is in the view, the exact property §1 calls out: a KPI reading "38% repeat" has to mean the same 38% no matter what a viewer has filtered elsewhere on the page. Without FIXED, the same formula would silently recompute at whatever granularity the current view happened to be using.
- **Supplier vs. Overall Sell-Through Gap**, shown on the Supplier Scorecard, compares each supplier's own sell-through rate against the business's overall rate, in percentage points. The "overall" side pools raw units sold and units acquired across every supplier before dividing, rather than averaging each supplier's already-computed rate, so a supplier's influence on that baseline scales with how many units they've actually brought in. Pooled this way, the calculation is mathematically identical to the business's company-wide sell-through rate; it's just computed from inside a view still broken out by supplier, so each row can be measured against it directly.

---

*Note: real customer, supplier, and financial data are not included anywhere in this repo. Specific business findings and figures from the engagement are documented in a private report for the client instead, standard confidentiality practice for an operating business, not a reflection on the work or its results.*
