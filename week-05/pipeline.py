"""pipeline.py - coordinator 1: a fixed pipeline of one company's AI specialists.

Python decides the order, the same for every supplier email:
  1. supply analyst      reads the email, uses the data tools, writes the JSON brief
  2. supplier writer     drafts the reply to the supplier (or NO REPLY NEEDED), reviewed
  3. customer writer     drafts a notice for each checked order at risk, each reviewed -
                         skipped while the supplier's options are undecided
  4. summary             written by this code from the brief and the drafts (no Claude call)

Compare with orchestrator.py, where Claude decides which specialists to use.

The analyst's brief is printed in full, and each final draft is printed once after its
review step, marked for a person or not.

--org names the company folder (five CSVs, supplier-emails/, prompts/): a path, or a
folder name in week-05. Without it, week-05/brookfield-bikes is used.

Usage (from the top of the repository):
  .venv\\Scripts\\python.exe week-05\\pipeline.py --email week-05\\brookfield-bikes\\supplier-emails\\01-northgate-controller-delay.eml
  .venv\\Scripts\\python.exe week-05\\pipeline.py --org week-05\\copperline-coffee --email week-05\\copperline-coffee\\supplier-emails\\01-danforth-pump-delay.eml --save week-05\\runs\\copperline-01-pipeline.txt

Drafts and the summary are saved in week-05/drafts/<company>/<date-time>-pipeline-<email>/.
Nothing is ever sent.
"""

from __future__ import annotations

import specialists as sp


def pipeline_summary(run: sp.Run) -> tuple[str, bool]:
    brief = run.brief or {}
    lines = [f"What changed: {brief.get('what_changed', '(unknown)')}"]
    at_risk = brief.get("orders_at_risk", [])
    if at_risk:
        lines.append("Orders at risk (depends on the option chosen - see below):" if run.options_pending
                     else "Orders at risk:")
        for o in at_risk:
            late = (o.get("ready_day") - o.get("ship_day")
                    if isinstance(o.get("ready_day"), int) and isinstance(o.get("ship_day"), int) else "?")
            lines.append(f"  - {o.get('order_id')} {o.get('customer')}: {o.get('units_short')} units, "
                         f"{late} days late{' - LATE PENALTY' if o.get('late_penalty') else ''}")
    elif not brief.get("options"):
        lines.append("Orders at risk: none.")
    decisions = brief.get("decisions_for_a_person", [])
    if decisions:
        lines.append("Decisions for a person:")
        lines += [f"  - {d}" for d in decisions]
    needs_decision = bool(decisions) or any(d.verdict == "NEEDS REVIEW" for d in run.drafts) or bool(run.warnings)
    return "\n".join(lines), needs_decision


async def run_pipeline(run: sp.Run) -> None:
    print("Step 1: supply analyst")
    await sp.run_supply_analyst(run)

    print("\nStep 2: supplier reply")
    await sp.draft_supplier_reply(run)

    print("\nStep 3: customer notices")
    if run.options_pending:
        print("  held: the supplier offered options that no one has chosen yet.")
    elif not run.verified_orders:
        print("  none: no checked orders at risk.")
    for order_id in ([] if run.options_pending else list(run.verified_orders)):
        await sp.draft_customer_notice(run, order_id)

    summary, needs_decision = pipeline_summary(run)
    sp.finish(run, summary, needs_decision)


if __name__ == "__main__":
    sp.cli_main("pipeline", "Fixed pipeline: a company's response to one supplier email.", run_pipeline)
