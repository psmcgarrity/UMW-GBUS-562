# Week 05 Writeup: Pipeline vs. Orchestrator

## What I Built
A team of AI specialists that prepares Brookfield Bicycle Company's response to one supplier email:

| Specialist | Model | Job |
|---|---|---|
| Supply analyst | `claude-opus-5-5` | Uses three data tools from an MCP server (`supply_mcp_server.py`): `get_part`, `get_orders_for_part`, `check_coverage`. Returns a JSON brief. |
| Supplier writer | `claude-sonnet-5-5` | Drafts the reply to the supplier. |
| Customer writer | `claude-sonnet-5-5` | Drafts a notice to the customer of one order at risk. |
| Reviewer | `claude-sonnet-5-5` | Checks every draft against six rules: APPROVE or REVISE (up to two rewrites). |

Two coordinators run the same specialists (`specialists.py`):

- **Pipeline** (`pipeline.py`): Python runs the same steps every time: analyst, then supplier reply, then one notice per order at risk, then a summary written by code.
- **Orchestrator** (`orchestrator.py`): a `claude-opus-5-5` agent decides which specialists to call, through the tools `run_supply_analyst`, `draft_supplier_reply`, `draft_customer_notice` and `finish`.

Some rules are enforced in code for both coordinators, so neither one can skip them:
- every draft is reviewed;
- the supplier writer never sees customer names or order numbers;
- the customer writer sees only that one order's facts;
- notices are only for orders that `check_coverage` confirmed;
- no notices while the supplier's options are undecided;
- nothing can be sent.

## The Three Emails

| Email | What the supplier says | Correct impact (from `check_coverage`) |
|---|---|---|
| 01 Northgate | PO-7781: 120 of 400 MC-500 controllers on the scheduled date (day 5), 280 three weeks later (day 26) | 3 orders late: O-1043 (10 bikes, 12 days, **late penalty**), O-1044 (30 bikes, 8 days), O-1045 (50 bikes, 5 days) |
| 02 Kendall | PO-7795: two options, choose within two business days, or Option 2 by default | Option 1 (160 on day 8, 240 on day 22): no orders late. Option 2 (all 400 on day 12): O-1046 is 20 bikes, 3 days late |
| 03 Keystone | PO-7790 shipped on schedule, no action needed | No orders at risk |

## Results

| | 01 Pipeline | 01 Orchestrator | 02 Pipeline | 02 Orchestrator | 03 Pipeline | 03 Orchestrator |
|---|---|---|---|---|---|---|
| Analyst run? | Yes (4 tool calls) | Yes (4 tool calls) | Yes (4) | Yes (5) | Yes (3) | **No** |
| Orders at risk found | O-1043, O-1044, O-1045 | Same | O-1046, under Option 2 only | Same | None | Not checked |
| Supplier reply | Approved (1 rewrite) | Approved (0 rewrites) | Approved (1 rewrite) | Approved (0 rewrites) | NO REPLY NEEDED | None |
| Customer notices | **3**, all approved first time | **None** | Held: options undecided | Held: options undecided | None needed | None |
| Decision for a person? | Yes | Yes | Yes | Yes | No | No |
| Tokens in / out | 18,607 / 5,679 | 18,504 / 4,647 | 14,318 / 4,695 | 17,612 / 4,190 | 7,957 / 769 | **1,390 / 575** |
| Approx. cost* | $0.13 | $0.14 | $0.11 | $0.13 | $0.05 | **$0.02** |

\*At list prices: Opus 5.5 $4 / $20 and Sonnet 5.5 $2 / $10 per million input / output tokens.

Transcripts are in `runs/` (`01-pipeline.txt`, `01-orchestrator.txt`, ...). Drafts, briefs and summaries are in `drafts/<date-time>-<coordinator>-<email>/`.

## Comparison

**01 Northgate: same facts, different judgment.** Both analysts found the same three orders, with the same numbers. The pipeline then did what it always does and drafted all three customer notices. The orchestrator drafted only the supplier reply. It held the notices on purpose, because Northgate offered to discuss other options that week, so the dates might still change. It listed "when to tell the three customers" as a decision for the purchasing manager. Both choices are defensible. The pipeline gives the manager more ready-to-send drafts, and the orchestrator avoids notices that may have to be corrected. The two runs cost about the same. The orchestrator saved by skipping three notices, but its own calls (about 7.8k tokens) used up that saving.

**02 Kendall: the same result.** Both coordinators ran the analyst, drafted a supplier reply that accepts neither option, held the customer notice for Hilltop Outfitters (O-1046), and flagged the decision with its two-business-day deadline. The deadline matters because Kendall's default, Option 2, is the one that makes O-1046 late. The pipeline cost slightly less here because it has no orchestrator calls. The difference in rewrites (1 vs. 0) comes from the writer, not the coordinator.

**03 Keystone: where the orchestrator wins.** The pipeline ran the analyst with three tool calls just to confirm that nothing changed, then asked the supplier writer, which answered NO REPLY NEEDED. The orchestrator read the email and called `finish` immediately. It used about a fifth of the pipeline's tokens (1,965 vs. 8,726) and about a third of its cost. It didn't check the data, so it relied on the supplier's word that nothing changed. For an on-schedule email that is a reasonable risk.

**Summary.**

| | Pipeline | Orchestrator |
|---|---|---|
| Predictability | Same steps, same kinds of output, every time | Varies with the email, and with Claude's judgment |
| Cost on emails that need work (01, 02) | Slightly lower or equal | Pays ~7–8k tokens for orchestration |
| Cost on emails that need no work (03) | Pays for the full analysis anyway | ~6× fewer input tokens |
| Judgment | None: drafts notices even when timing is debatable | Held Northgate notices for a sensible business reason |
| Risk | Does unnecessary work | May skip work a manager expected (no Northgate notices; no data check on Keystone) |

The rules in code made the comparison fair. Both coordinators had to follow the same guardrails, so the differences above come only from **who decides which steps run**, not from one coordinator bending the rules.

## What Testing Found and Fixed
- **The writer and reviewer prompts conflicted.** `customer-writer.txt` says to give the cause as "a delay in a part from one of our suppliers". The reviewer, which hadn't been given that wording as a fact, rejected it as an invented cause and a supplier mention. In the first run, all three customer notices ended as NEEDS REVIEW after two rewrites each. Fix: the approved cause wording is now one of the order facts. The prompts are unchanged.
- **The writer guessed how late bikes would ship.** It kept writing "all 50 bikes ship together", and the reviewer kept rejecting that as unsupported. Fix: a `shipment_plan` fact from `check_coverage` (for example, "all 50 bikes ship together in one shipment, 5 days after the planned date"). After this, all notices passed review on the first try.
- **The reviewer missed one error, and the orchestrator caught it.** An approved supplier reply said "90 units short on the scheduled ship date". The 90 is the shortfall across customer orders due on days 14–21, not on day 5. The cause was a misleading field name in the supplier's facts (`parts_short_on_ship_days`), now renamed. The orchestrator spotted the error while writing its summary. That is a second check the pipeline doesn't have.
- **Options were summarized wrongly.** The analyst named the field `order_ids_at_risk` instead of `order_ids`, so both Kendall options showed "delays no orders". The code now accepts either name.
- **Still open:** the supplier writer is told to confirm "by the time they asked for". Northgate gave no deadline, so the writer invents one, and the reviewer rejects it under rule 2. That causes the remaining supplier rewrites. It could be fixed the same way, with a `decision_deadline` fact.

## Privacy Check
All 11 saved supplier replies were scanned for every customer name and order number, and all 12 customer notices for supplier names, penalties, discounts and credits. Nothing was found. Northgate's reply didn't name any customer, even though Karen Walsh asked which customers were most affected.
