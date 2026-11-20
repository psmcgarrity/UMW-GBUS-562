"""orchestrator.py - coordinator 2: an orchestrator agent runs Brookfield's AI specialists.

Claude (claude-opus-5-5, prompt in brookfield-bikes/prompts/orchestrator.txt) reads the
supplier email and decides which specialists to use, through four tools:
  run_supply_analyst      the analyst works out the impact (JSON brief)
  draft_supplier_reply    the supplier writer drafts a reply; the reviewer checks it
  draft_customer_notice   the customer writer drafts a notice for one order; reviewed
  finish                  ends the job with a summary for the purchasing manager

The same rules as in pipeline.py are enforced in specialists.py, not left to Claude:
every draft is reviewed, writers only see what they are allowed to, notices only for
checked orders at risk and never while options are undecided, and nothing can be sent.

Usage (from the top of the repository):
  .venv\\Scripts\\python.exe week-05\\orchestrator.py --email week-05\\brookfield-bikes\\supplier-emails\\03-keystone-on-schedule.eml
  .venv\\Scripts\\python.exe week-05\\orchestrator.py --email week-05\\brookfield-bikes\\supplier-emails\\01-northgate-controller-delay.eml --save week-05\\runs\\01-orchestrator.txt

Drafts and the summary are saved in week-05/drafts/<date-time>-orchestrator-<email>/.
"""

from __future__ import annotations

import json
from typing import Any

import specialists as sp

MAX_TOOL_CALLS = 12


def _tool(name: str, description: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    properties = properties or {}
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties,
                             "required": list(properties), "additionalProperties": False}}


TOOLS = [
    _tool("run_supply_analyst",
          "Ask the supply analyst to work out how this email affects Brookfield's customer orders, using "
          "the stock, shipment and order data. Returns the analyst's brief as JSON: what changed, the "
          "orders at risk, the supplier's options, decisions for a person, and the supplier's questions."),
    _tool("draft_supplier_reply",
          "Ask the supplier writer to draft a reply to the supplier from the analyst's brief (run the "
          "analyst first). The reviewer checks the draft, and it is saved for a person. Returns the draft "
          "and the reviewer's verdict."),
    _tool("draft_customer_notice",
          "Ask the customer writer to draft a notice to the customer of one order at risk in the analyst's "
          "brief. The reviewer checks the draft, and it is saved for a person. Notices are held while the "
          "supplier's options are undecided. Returns the draft and the reviewer's verdict.",
          {"order_id": {"type": "string", "description": "An order_id from the brief's orders_at_risk."}}),
    _tool("finish",
          "End the job. Give a short summary for Brookfield's purchasing manager and say whether a person "
          "needs to make a decision.",
          {"summary": {"type": "string", "description": "A short summary for the purchasing manager."},
           "needs_decision": {"type": "boolean", "description": "True if a person must decide something."}}),
]


def draft_result(d: sp.Draft) -> str:
    return json.dumps({"verdict": d.verdict, "rewrites": d.rewrites, "saved_as": d.path.name if d.path else "",
                       "draft": d.text}, indent=2)


async def run_orchestrator(run: sp.Run) -> None:
    finished: dict[str, Any] = {}

    async def handle(name: str, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            if name == "run_supply_analyst":
                if run.brief is not None:
                    return "The supply analyst already ran. Its brief:\n" + json.dumps(run.brief, indent=2), True
                brief = await sp.run_supply_analyst(run, indent="    ")
                result = {"brief": brief}
                if run.warnings:
                    result["warnings"] = run.warnings
                if run.options_pending:
                    result["note"] = "Customer notices are held until a person chooses one of the supplier's options."
                return json.dumps(result, indent=2), False
            if name == "draft_supplier_reply":
                if any(d.kind == "supplier" for d in run.drafts):
                    return "A supplier reply was already drafted.", True
                return draft_result(await sp.draft_supplier_reply(run, indent="    ")), False
            if name == "draft_customer_notice":
                return draft_result(await sp.draft_customer_notice(run, str(args.get("order_id", "")),
                                                                   indent="    ")), False
            if name == "finish":
                finished.update(summary=str(args.get("summary", "")),
                                needs_decision=bool(args.get("needs_decision", True)))
                return "Finished.", False
            return f"Unknown tool: {name}", True
        except sp.NotAllowed as exc:
            return f"Not done: {exc}", True

    print(f"[orchestrator] {sp.ORCHESTRATOR_MODEL}")
    final = await sp.tool_loop(
        run, "orchestrator", sp.ORCHESTRATOR_MODEL, sp.load_prompt("orchestrator"),
        "Coordinate Brookfield's response to this supplier email.\n\n" + sp.email_block(run.email),
        TOOLS, handle, MAX_TOOL_CALLS, is_done=lambda: bool(finished))

    if not finished:
        run.warn("the orchestrator stopped without calling finish.")
        finished.update(summary=final or "(no summary)", needs_decision=True)
    sp.finish(run, finished["summary"], finished["needs_decision"])


if __name__ == "__main__":
    sp.cli_main("orchestrator", "Orchestrator agent: Brookfield's response to one supplier email.",
                run_orchestrator)
