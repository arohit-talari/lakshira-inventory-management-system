# Business Intelligence Architecture

This document describes how the Business Intelligence layer is built and why: the data source structure, the validation methodology behind every figure on it, the synthetic-data approach behind the sanitized public demo, and the calculation architecture behind its most complex metrics. For the design reasoning behind what's actually on the dashboard, why these views, why these KPIs, see [Business Intelligence](BUSINESS_INTELLIGENCE.md); for the full engagement lifecycle this stage is one part of, see the main [README](../README.md).

---

## 1. Data Model and Source Structure

The Tableau data source is built on Relationships, not Joins, connecting the inventory and transaction tables exported from the MySQL warehouse; customer details are already flattened into the transaction table itself, not a separate connection.

This distinction has real, practical consequences. A join permanently merges two tables into one fixed shape at the moment it's defined; every worksheet built afterward queries that same merged table regardless of what it actually needs, and if the tables don't share a clean one-to-one relationship, that fixed merge can silently duplicate rows or drop them entirely, with no error to flag it. A relationship keeps the tables logically separate and lets Tableau generate the appropriate join fresh, at query time, based on exactly which fields a given worksheet is actually using, correctly matched to that worksheet's own granularity.

That flexibility comes with its own real pitfall, one worth naming directly rather than glossing over. Because the query is regenerated per worksheet, the same calculated field can behave differently depending on which other dimensions are in the view. For example, a field counting a customer's purchases evaluates per-customer when Customer Name is the worksheet's dimension, but silently re-evaluates per-state when Geography is the dimension instead, same field, same formula, different result, because the surrounding context changes what Tableau decides to query. Every calculated field on this dashboard was checked against that possibility directly, not assumed safe by default.

## 2. Validation Methodology

No KPI on this dashboard was trusted because Tableau displayed it. Every figure was independently recomputed against the underlying data, verified directly against the same source tables, before being considered correct, and that process caught real, non-obvious bugs that a visual read of the dashboard alone would have missed:

- A cached extract that kept showing outdated figures after the data source refreshed, fixed only by forcing a full reconnect.
- A filter that had silently inherited a "top 5 customers" scope from the worksheet it was duplicated from, narrowing an unrelated chart to a handful of customers instead of the full base.
- A field read in as text instead of a number after a reconnect, turning a numeric comparison into an alphabetical one and producing results that looked plausible but were structurally wrong.

The discipline is the same one applied throughout the rest of this engagement: verify against the source of truth directly, irrespective of whether a tool's output looks correct or nothing visibly broke.

## 3. The Sanitized Demo and Synthetic-Data Methodology

Real customer, supplier, and financial data are not included anywhere in this repository. A separate, fully synthetic dataset, matching the real system's column structure and business logic, powers a sanitized copy of this workbook for public demonstration, preserving how the dashboard behaves without exposing a single real figure.

The synthetic dataset started from Lakshira's real numbers as a baseline, rather than being generated from scratch. Building it out from there still took real care:

- **Revenue and profit carry real variance from month to month**, not capped at a flat ceiling, each month building somewhat on the one before rather than jumping around independently.
- **Customer spending, supplier performance, and collection sell-through rates are deliberately shaped**, not left purely random, with some suppliers and a handful of customers separating themselves from the pack with respect to sell-through and spending while others trail behind.
- **Prices climb over time** instead of staying flat across the whole history, since the dataset represents a business just stepping into a more premium position, not one that's already been there for years.

The synthetic dataset was then reviewed the same way the real one was, checked against expectations rather than assumed correct.

## 4. Calculation Architecture

Not every number on this dashboard reduces to a SUM or COUNT. Three calculations required real engineering, each demonstrating a distinct Tableau calculation model rather than a variation on the same technique.

### Sales Consistency (Swing)

A five-field chain, comparing two 6-month periods (current vs. prior) to assess month-to-month sales variation within each. It doesn't work as a single formula, but as a five-step procedure: (1) each period's highest monthly units sold, (2) each period's lowest monthly units sold, (3) each period's own average across the six months, (4) a swing normalized against that period's own average, dividing the raw high-low spread by that same average (so a business that's simply grown larger between periods isn't penalized with an inflated swing purely from scale), and (5) a final comparison, the current period's normalized swing minus the prior's, showing whether consistency improved or worsened.

### Repeat Customers %

A nested FIXED Level of Detail (LOD) expression: `{FIXED : COUNTD(repeat customers)} / {FIXED : COUNTD(all customers)}`, where "repeat" is itself a separate FIXED expression, `{FIXED [customer_name] : COUNTD(transaction_id)} > 1`, flagging any customer with more than one transaction. The repeat flag is calculated customer by customer. The two counts around it are calculated across the whole business at once, not per customer. Locking every part of this with FIXED means the final percentage, say, "38% repeat", always means the same 38%, irrespective of what's filtered elsewhere on the page. Without FIXED, that same formula could quietly show a different number depending on what else happens to be in the view.

### Supplier vs. Overall Sell-Through Gap

Shown on the Supplier Scorecard, this compares each supplier's own sell-through rate to the business's overall rate, in percentage points. But "overall" isn't a simple average of each supplier's rate. It's the sum of units sold across every supplier, divided by the sum of units acquired across every supplier. That means a supplier's weight in the overall number scales with how many units they've actually brought in, a large supplier moves it more than a small one does. This pooled number ends up being the same as the business's company-wide sell-through rate; it's just calculated from inside a table still broken out by supplier, so each row can be measured against it directly.

---

*Note: real customer, supplier, and financial data are not included anywhere in this repo. Specific business findings and figures from the engagement are documented in a private report for the client instead, standard confidentiality practice for an operating business, not a reflection on the work or its results.*
