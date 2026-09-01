<h1 align="center">Lakshira's Business Intelligence Layer</h1>

*Part of the [Lakshira engagement](../README.md). This is the deep dive on the Business Intelligence stage specifically; refer to the main README for the full lifecycle: discovery, data foundation, warehouse, this stage, and the operational system.*

The Business Intelligence (BI) layer is a 3-View Tableau Workbook built on top of the MySQL warehouse: Business Overview, Inventory & Operations, and Customer Intelligence. This document covers the design reasoning behind it, not just what it shows: why a dashboard was needed at all once an operational system already existed, why three views instead of one, and how filters and dimensions are used deliberately rather than added for their own sake.

The dashboard design below, every KPI, calculation, chart, and filter, is identical to what the client uses in her own weekly workflow. Only the data differs: these screenshots are from a sanitized, fully synthetic version of the workbook (see the [companion architecture doc](BUSINESS_INTELLIGENCE_ARCHITECTURE.md) for how that dataset was built), so no real customer, supplier, or financial figures appear anywhere in this repo.

<p align="center"><img src="images/business_overview.png" width="800" alt="Business Overview dashboard"></p>
<p align="center"><img src="images/inventory_operations.png" width="800" alt="Inventory & Operations dashboard"></p>
<p align="center"><img src="images/customer_intelligence.png" width="800" alt="Customer Intelligence dashboard"></p>

## 1. Why This Exists

Before any of this, questions about the business got answered off the top of the owner's head, because there was no reliable, structured way to answer them otherwise.

The data model built earlier in this engagement solved that in theory: once inventory and sales data lived in a real schema, connected to MySQL, a technical analyst could query it directly, pull historical patterns, spot trends in sales, intake, and repeat-versus-new customer behavior. But a queryable database doesn't help a non-technical business owner. She can't write a query.

Even before that, a staging schema, a spreadsheet with explicit directives on how to enter each field, had already been handed to the client so an analyst could transfer her input into the master sheet. It wasn't enough. There weren't enough rules or parameters in place to actually catch mathematical and input errors at the point of entry, so mistakes still made it through, and catching them required a second set of eyes who understood the business well enough to recognize when a number looked wrong. That was its own bottleneck: errors had to be sent back to the client to fix, and the time spent auditing her entries was time not spent building the tools the business actually needed to operate at scale.

There's a second, more basic constraint underneath all of this: she runs this business alone, fronting supplier relationships, inventory intake, marketing, sales, customer relationships, pricing, and now building her own storefront from scratch. Every hour spent on manual data entry is an hour not spent on the parts of the business only she can do. The [Inventory Management System](IMS.md) closed both gaps at once, not by eliminating manual entry, that's not realistically avoidable for a one-person operation, but by making the time she does spend on it count: a guided Command-Line Interface (CLI) that validates and constrains input at the moment she enters it instead of catching mistakes after the fact, prompts her for information in a structured, one-field-at-a-time way instead of confronting her with 39 raw spreadsheet columns at once, and cleanly maps and appends every entry to its correct column in the master sheet without her ever needing to think about spreadsheet mechanics. One of its ten operations, Generate Report, closed the output side too, aggregating her data into a PDF on demand.

That still left a real gap. The report generator only runs on fixed periods, monthly, quarterly, annually, or a custom date range, and it's text-heavy, so insight(s) can get buried in a field of text rather than surfaced at a glance. The deeper problem is cadence: at quarterly or annual scale, she'd naturally expect to see large swings, so a genuinely bad month hiding inside an otherwise-good quarter wouldn't surface until the report caught up to it, by which point weeks of missed correction had already happened. A business that wants to grow needs to catch a bad week or month while it's still happening, not read about it three months later.

The dashboard exists specifically to close that timing gap: built for a weekly or monthly refresh instead of quarterly or annual, communicating through Key Performance Indicators (KPIs) and visual charts instead of paragraphs, and letting her slice the data herself, at whatever granularity actually helps her, rather than waiting on a fixed report period. It's a faster feedback loop, not just a layer built on aesthetics.

## 2. Why Three Views, Not One

The first instinct was a single view. That didn't survive contact with a basic lesson learned building and studying dashboards: a dashboard that's colorful, filter-heavy, and stuffed with every visualization type available isn't more informative for it, it's messier. A good dashboard is simple, scoped tightly to what the business and the client actually need, and interactive enough that even the least technical user can find their way around it without onboarding.

Scoping down from "everything" to three views followed the three distinct questions the business actually needed answered, not an arbitrary split:

| View | Question it answers |
|---|---|
| **Business Overview** | How is the business doing overall, and is it trending up or down right now? |
| **Inventory & Operations** | What's hampering the business, where is capital tied up or at risk? |
| **Customer Intelligence** | Who are the customers actually keeping this business running? |

A single combined view would have forced incompatible granularities onto the same page, lifetime health metrics next to individual aging units next to per-customer detail, and made it hard to protect the one thing that matters most on a KPI: that a lifetime number stays lifetime truth regardless of what a viewer happens to be filtering elsewhere on the page (more on that in §4). Three focused views, each free to define its own filters and its own level of detail, avoided that collision entirely.

## 3. Filters and Dimensions as a Design Principle

The report generator's fixed periods meant she could never see how the data actually responded as she got more or less granular; every view was locked to whatever period she requested. The dashboard is built around the opposite idea: put the granularity control in her hands, and let her explore at her own pace instead of waiting for a scheduled report.

This isn't interactivity for its own sake. A concrete example: Revenue by Region on Customer Intelligence isn't just a curiosity chart. As the business takes on more international customers and continues running in-person pop-ups and exhibitions, seeing which regions and countries are actually generating revenue directly informs where she takes the business physically next. The filter isn't a nice-to-have; it's a planning tool.

That said, not every object on the dashboard should move when a filter moves. The KPI cards at the top of each view are meant to answer one question, full stop: how is the business doing? If that number changed depending on whatever a viewer happened to have filtered elsewhere on the page, it would undermine the one thing those cards exist to protect: a stable, trustworthy baseline. So the dashboard draws a deliberate line: headline KPIs stay immune to ordinary filters, while the supporting charts and tables beneath them, Revenue by Customer, the Collection Sell-Through chart, the Recent Transactions table, are explorable and filter-responsive, because their whole job is letting her drill in. Filters exist where exploration adds value, and are deliberately excluded where they'd undermine trust in a number meant to always mean the same thing.

## 4. The Three Views in Detail

### 4.1 Business Overview

This view establishes lifetime health first, then asks whether the business is currently trending in the right direction. Five headline KPIs, Total Revenue, Gross Profit, Gross Margin, Units Sold, and Average Order Value, each report a lifetime total alongside a delta badge comparing the current 30-day window against the prior 30-day window. The delta is the more actionable number: it tells her whether recent conditions are moving up or down so she can start investigating the cause while it's still recent, rather than discovering a decline months later in a scheduled report. The Revenue and Gross Profit Trend chart underneath complements that same delta by showing the shape of the whole trajectory, not just a two-point comparison.

Two supplementary objects add depth without diluting the headline row. **Sales Consistency** answers a question a simple revenue trend can't: not just "is revenue up or down," but "how erratic is it." It compares the swing between a period's best and worst month, relative to a typical month, across the last six months against the prior six, a genuinely custom metric built specifically because volatility itself is information a business owner needs, a business that swings wildly month to month carries a different kind of risk than one growing steadily, even at the same average growth rate. **Recent Transactions** is a rolling 30-day window of individual closed sales, giving her a ground-level view underneath the aggregate KPIs.

### 4.2 Inventory & Operations

Business Overview measures health; this view identifies what's actively hampering it. Five KPIs, framed around capital at risk rather than abstract inventory statistics: Unsold Inventory (capital currently tied up in stock that hasn't sold), Dead Stock Units and their Cash Exposure (units aged 180+ days without moving, the more painful inflection point since these are functionally stuck), and Units Selling Below Cost with their Cash Exposure (units priced under what they cost to acquire, a real loss if sold at that price, not just a smaller profit).

Two charts extend the reasoning. Given the business carries dozens of distinct weave-type collections, **Collection Sell-Through Rate** identifies which are actually moving, informing future intake: allocate more toward what's selling, less toward what isn't, rather than reordering by habit. **Inventory Age Distribution** breaks down how long available units have been sitting, weighted toward flagging the 91-180 day and 180+ day buckets specifically, since with 1,000+ units in inventory at any given time, and that number only growing as the business scales, knowing where the aging risk is concentrated matters more than knowing the total count alone. The **Supplier Scorecard** closes the view by identifying which suppliers the business buys most from and which suppliers' units customers actually buy, information she needs to cultivate the right relationships as sourcing scales with the business.

### 4.3 Customer Intelligence

Before this engagement, no customer information was captured at all, sales went to whoever was buying, with no record of who they were. That's a real gap: a business is built on its most loyal customers, in the same way supplier relationships need active cultivation, customer relationships do too, and repeat customers are what keep a business afloat during a slow stretch for new customer acquisition.

The five KPIs are framed as ready-reference answers to the questions that actually matter for prioritization: how much is still owed on sold units, how many total customers exist, what share are repeat buyers, what does the average customer spend, and how much of lifetime revenue comes from the top 5. Together they tell her who to prioritize, support forecasting, and give her a concrete starting point for converting one-time buyers into repeat ones. **Revenue by Customer** breaks down exactly how much her top 10 have contributed. **Revenue by Region** supports the international and exhibition-planning use case described in §3. The **Customer Breakdown** table is the full detail underneath all of it: every customer who's ever purchased, their preferred collection, purchase count, dollars spent, average order value, any outstanding balance, and their last purchase date, specifically so she has a concrete, evidence-based reason to reach back out if it's been too long.

### Design language

Every tooltip across all three views follows the same rule: it earns its place only by adding information that isn't already visible elsewhere on the card or chart. A KPI card's tooltip never restates the number already shown in large text above it; instead it breaks that number into its current/prior components, or explains a term that isn't self-evident (Gross Margin, Accounts Receivable). A chart's tooltip surfaces whatever the visual encoding *can't* show, a bar's exact underlying count behind a percentage, a supplier's performance relative to the overall average, not a repeat of the axis label sitting right next to it.

Color follows a similarly deliberate rule: green means healthy, gold means caution, red-orange through dark maroon means risk, and that mapping is held constant across every KPI card, chart tier, and table on the dashboard, not reinvented per visualization. A viewer who learns the color language once on Inventory & Operations can carry that same reading straight into the Supplier Scorecard or the Inventory Age Distribution chart without relearning it.

---

For how the underlying data source is structured, how every figure on this dashboard is independently verified, and how the sanitized demo's synthetic dataset was built to read as genuine, see [Business Intelligence Architecture](BUSINESS_INTELLIGENCE_ARCHITECTURE.md).

*Note: real customer, supplier, and financial data are not included anywhere in this repo. Specific business findings and figures from the engagement are documented in a private report for the client instead, standard confidentiality practice for an operating business, not a reflection on the work or its results.*
