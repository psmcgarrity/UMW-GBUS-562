"""supply_mcp_server.py - an MCP server with Brookfield Bicycle Company's supply data tools.

The company is a folder (--data; default week-05/brookfield-bikes) holding:
  parts.csv              part_id, description, on_hand, supplier
  inbound.csv            po_number, part_id, supplier, qty, arrives_in_days
  orders.csv             order_id, customer, contact_email, model, qty, ship_in_days, late_penalty
  bill-of-materials.csv  model, part_id, qty_per_bike
  suppliers.csv          supplier, contact_name, contact_email, part_id

Tools (used by the supply analyst only; days are counted from today = day 0)
  get_part(part_id)               Stock on hand, supplier, and scheduled shipments.
  get_orders_for_part(part_id)    Open customer orders whose model uses the part, in
                                  ship-day order, with the parts each one needs.
  check_coverage(part_id, po_number, revised_shipments)
                                  Replaces one purchase order's shipment with a revised
                                  schedule and works out, order by order, whether the
                                  part arrives in time. Passing the PO's scheduled
                                  shipment unchanged checks the current plan.

Coverage rules (decided with the user):
  * Parts go to orders strictly in ship-day order (first come, first served).
  * Partial shipments are allowed: on its ship day an order ships every bike it has
    parts for; the rest follow on the first day the parts for all of it are in.
  * Parts that arrive on a day can be used on that same day.
  * Only the part named is checked; shortages of other parts are not considered.

Customer contact emails are never returned by any tool.

Usage (from the top of the repository):
  .venv\\Scripts\\python.exe week-05\\supply_mcp_server.py
  .venv\\Scripts\\python.exe week-05\\supply_mcp_server.py --check MC-500 PO-7781 120@5 280@26
      (prints one check_coverage result as JSON and exits - for testing without Claude)

The server talks MCP over stdin/stdout, so nothing in server mode may print to
stdout; all logging goes to stderr.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent                  # .../UMW-GBUS-562/week-05
DEFAULT_DATA_DIR = HERE / "brookfield-bikes"
DATA_FILES = ("parts.csv", "inbound.csv", "orders.csv", "bill-of-materials.csv", "suppliers.csv")


def log(message: str) -> None:
    """Log to stderr; stdout carries the MCP protocol."""
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


@dataclass
class SupplyData:
    parts: dict[str, dict[str, str]]          # part_id -> row
    inbound: list[dict[str, str]]
    orders: list[dict[str, str]]
    bom: list[dict[str, str]]

    @classmethod
    def load(cls, data_dir: Path) -> "SupplyData":
        """Read the CSVs fresh, so edits to the data show up on the next tool call."""
        parts = {r["part_id"].upper(): r for r in read_csv(data_dir / "parts.csv")}
        return cls(parts=parts,
                   inbound=read_csv(data_dir / "inbound.csv"),
                   orders=read_csv(data_dir / "orders.csv"),
                   bom=read_csv(data_dir / "bill-of-materials.csv"))

    def part(self, part_id: str) -> dict[str, str]:
        key = (part_id or "").strip().upper()
        if key not in self.parts:
            raise ToolError(f"No part {part_id!r}. Known parts: {', '.join(sorted(self.parts))}.")
        return self.parts[key]

    def shipments(self, part_id: str) -> list[dict[str, Any]]:
        return sorted(({"po_number": r["po_number"], "supplier": r["supplier"], "qty": int(r["qty"]),
                        "arrives_in_days": int(r["arrives_in_days"])}
                       for r in self.inbound if r["part_id"].upper() == part_id),
                      key=lambda s: s["arrives_in_days"])

    def orders_needing(self, part_id: str) -> list[dict[str, Any]]:
        per_bike = {r["model"]: int(r["qty_per_bike"]) for r in self.bom if r["part_id"].upper() == part_id}
        out = []
        for r in self.orders:
            if r["model"] in per_bike:
                bikes = int(r["qty"])
                out.append({"order_id": r["order_id"], "customer": r["customer"], "model": r["model"],
                            "bikes": bikes, "parts_per_bike": per_bike[r["model"]],
                            "parts_needed": bikes * per_bike[r["model"]],
                            "ship_day": int(r["ship_in_days"]),
                            "late_penalty": r["late_penalty"].lower() == "yes"})
        return sorted(out, key=lambda o: (o["ship_day"], o["order_id"]))


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #
def coverage(on_hand: int, arrivals: list[tuple[int, int]], orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ship-day-order allocation with partial shipments.

    Orders form one queue of parts: order k needs the parts from position
    before_k to before_k + parts_needed. supplied(d) is on_hand plus everything
    arriving on or before day d. On its ship day an order has
    clamp(supplied(ship_day) - before_k, 0, parts_needed) of its parts, so it ships
    that many // parts_per_bike bikes. The rest is ready on the first arrival day
    when supplied(d) >= before_k + parts_needed (None if no shipment ever covers it).
    """
    arrivals = sorted(arrivals, key=lambda a: a[1])

    def supplied(day: int) -> int:
        return on_hand + sum(q for q, d in arrivals if d <= day)

    results, before = [], 0
    for o in orders:
        need, ship = o["parts_needed"], o["ship_day"]
        have = max(0, min(need, supplied(ship) - before))
        bikes_on_time = have // o["parts_per_bike"]
        bikes_late = o["bikes"] - bikes_on_time
        ready_day = ship
        if bikes_late:
            ready_day = next((d for d in sorted({d for _, d in arrivals} | {ship})
                              if d >= ship and supplied(d) >= before + need), None)
        results.append({
            "order_id": o["order_id"], "customer": o["customer"], "model": o["model"],
            "bikes_ordered": o["bikes"], "bikes_on_time": bikes_on_time,
            "units_short": bikes_late,                      # bikes that miss the ship day
            "parts_short": need - have,
            "ship_day": ship, "ready_day": ready_day,
            "days_late": None if ready_day is None else ready_day - ship,
            "late_penalty": o["late_penalty"],
            "at_risk": bikes_late > 0,
        })
        before += need
    return results


class Shipment(BaseModel):
    qty: int = Field(description="Number of parts in this shipment.")
    arrives_in_days: int = Field(description="Days from today until it arrives (today = 0).")


def check(data: SupplyData, part_id: str, po_number: str, revised_shipments: list[Shipment]) -> dict[str, Any]:
    part = data.part(part_id)
    pid = part["part_id"].upper()
    scheduled = data.shipments(pid)
    po = (po_number or "").strip().upper()
    target = [s for s in scheduled if s["po_number"].upper() == po]
    if not target:
        raise ToolError(f"{po_number!r} is not a scheduled shipment of {pid}. "
                        f"Scheduled: {', '.join(s['po_number'] for s in scheduled) or 'none'}.")
    for s in revised_shipments:
        if s.qty < 0 or s.arrives_in_days < 0:
            raise ToolError("Quantities and arrives_in_days must be 0 or more (days are counted from today).")
    original = target[0]
    revised_total = sum(s.qty for s in revised_shipments)

    arrivals = [(s["qty"], s["arrives_in_days"]) for s in scheduled if s["po_number"].upper() != po]
    arrivals += [(s.qty, s.arrives_in_days) for s in revised_shipments if s.qty]
    orders = coverage(int(part["on_hand"]), arrivals, data.orders_needing(pid))
    at_risk = [o for o in orders if o["at_risk"]]

    result: dict[str, Any] = {
        "part_id": pid,
        "po_number": original["po_number"],
        "original_shipment": {"qty": original["qty"], "arrives_in_days": original["arrives_in_days"]},
        "revised_shipments": [s.model_dump() for s in sorted(revised_shipments, key=lambda s: s.arrives_in_days)],
        "on_hand": int(part["on_hand"]),
        "rules": "Parts go to orders in ship-day order; an order ships the bikes it has parts for "
                 "on its ship day and the rest on ready_day. units_short counts bikes.",
        "orders_at_risk": at_risk,
        "orders_on_time": [o["order_id"] for o in orders if not o["at_risk"]],
        "total_bikes_late": sum(o["units_short"] for o in at_risk),
    }
    if revised_total != original["qty"]:
        result["warning"] = (f"The revised shipments total {revised_total} parts, but {original['po_number']} "
                             f"is for {original['qty']}. Check this matches the email.")
    if any(o["ready_day"] is None for o in at_risk):
        result["note"] = "ready_day null: no scheduled shipment covers that order."
    return result


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
def create_server(data_dir: Path) -> MCPServer:
    server = MCPServer(
        name="brookfield-supply",
        instructions="Brookfield Bicycle Company supply data: parts, scheduled shipments, customer "
                     "orders, and coverage checks. Days are counted from today (day 0).",
    )

    @server.tool(description=(
        "Get one part's description, stock on hand, supplier, and its scheduled inbound shipments "
        "(purchase order number, quantity, and arrives_in_days, counted from today = day 0)."))
    def get_part(part_id: str) -> dict[str, Any]:
        data = SupplyData.load(data_dir)
        part = data.part(part_id)
        pid = part["part_id"].upper()
        return {"part_id": pid, "description": part["description"], "on_hand": int(part["on_hand"]),
                "supplier": part["supplier"], "scheduled_shipments": data.shipments(pid)}

    @server.tool(description=(
        "List the open customer orders whose bike model uses this part, in ship-day order: "
        "order_id, customer, model, bikes, parts_per_bike, parts_needed, ship_day (days from "
        "today) and late_penalty."))
    def get_orders_for_part(part_id: str) -> dict[str, Any]:
        data = SupplyData.load(data_dir)
        pid = data.part(part_id)["part_id"].upper()
        orders = data.orders_needing(pid)
        return {"part_id": pid, "orders": orders, "total_parts_needed": sum(o["parts_needed"] for o in orders)}

    @server.tool(description=(
        "Check whether customer orders can still ship on time if one purchase order's shipment "
        "changes. Give the part, the purchase order number, and the revised shipments for that "
        "PO (each with qty and arrives_in_days, counted from today = day 0); they replace the "
        "PO's scheduled shipment, and other scheduled shipments stay as they are. To check the "
        "current schedule, pass the PO's scheduled shipment unchanged. Returns each order at "
        "risk with units_short (bikes that miss the ship day), ship_day, ready_day (when the rest "
        "can ship), days_late and late_penalty. Parts go to orders in ship-day order, and an "
        "order ships the bikes it has parts for on time and the rest later."))
    def check_coverage(part_id: str, po_number: str, revised_shipments: list[Shipment]) -> dict[str, Any]:
        return check(SupplyData.load(data_dir), part_id, po_number, revised_shipments)

    return server


def parse_shipment(text: str) -> Shipment:
    qty, _, day = text.partition("@")
    try:
        return Shipment(qty=int(qty), arrives_in_days=int(day))
    except ValueError:
        raise argparse.ArgumentTypeError(f"shipment must look like QTY@DAY, e.g. 120@5 (got {text!r})")


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server with Brookfield's supply data tools.")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_DIR, metavar="FOLDER",
                        help="folder with the five CSV files (default: week-05/brookfield-bikes)")
    parser.add_argument("--check", nargs="+", metavar="ARG",
                        help="test check_coverage without MCP: PART_ID PO_NUMBER QTY@DAY [QTY@DAY ...]")
    args = parser.parse_args()
    data_dir = args.data.resolve()
    missing = [f for f in DATA_FILES if not (data_dir / f).is_file()]
    if missing:
        parser.error(f"data folder {args.data} is missing: {', '.join(missing)}")

    if args.check:
        if len(args.check) < 2:
            parser.error("--check needs PART_ID PO_NUMBER and zero or more QTY@DAY shipments")
        shipments = [parse_shipment(s) for s in args.check[2:]]
        try:
            result = check(SupplyData.load(data_dir), args.check[0], args.check[1], shipments)
        except ToolError as exc:
            sys.exit(f"Error: {exc}")
        print(json.dumps(result, indent=2))
        return

    log(f"Brookfield supply MCP server ready (stdio), data: {data_dir.name}")
    create_server(data_dir).run("stdio")


if __name__ == "__main__":
    main()
