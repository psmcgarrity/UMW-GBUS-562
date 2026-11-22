"""supply_mcp_server.py - an MCP server with one company's supply data tools.

The company is a folder (--org; default week-05/brookfield-bikes) holding:
  parts.csv              a part, its description, stock on hand, and supplier
  inbound.csv            scheduled shipments: PO number, part, supplier, quantity, arrival day
  orders.csv             customer orders: order, customer, contact email, product,
                         quantity, ship day, late penalty
  bill-of-materials.csv  product, part, parts per unit
  suppliers.csv          supplier contacts (not used by the tools)

Column names differ between companies (Brookfield's orders.csv says model, qty,
ship_in_days; Copperline's says product, units, ship_by_day), so no name is assumed:
find_columns() works out from each header row which column holds what (see COLUMNS),
and the server logs any column it found under a different name.

Tools (used by the supply analyst only; days are counted from today = day 0)
  get_part(part_id)               Stock on hand, supplier, and scheduled shipments.
  get_orders_for_part(part_id)    Open customer orders whose product uses the part, in
                                  ship-day order, with the parts each one needs.
  check_coverage(part_id, po_number, revised_shipments)
                                  Replaces one purchase order's shipment with a revised
                                  schedule and works out, order by order, whether the
                                  part arrives in time. Passing the PO's scheduled
                                  shipment unchanged checks the current plan.

Coverage rules (decided with the user):
  * Parts go to orders strictly in ship-day order (first come, first served).
  * Partial shipments are allowed: on its ship day an order ships every unit it has
    parts for; the rest follow on the first day the parts for all of it are in.
  * Parts that arrive on a day can be used on that same day.
  * Only the part named is checked; shortages of other parts are not considered.

Customer contact emails are never returned by any tool.

Usage (from the top of the repository):
  .venv\\Scripts\\python.exe week-05\\supply_mcp_server.py --org week-05\\copperline-coffee
  .venv\\Scripts\\python.exe week-05\\supply_mcp_server.py --check MC-500 PO-7781 120@5 280@26
      (prints one check_coverage result as JSON and exits - for testing without Claude)
  .venv\\Scripts\\python.exe week-05\\supply_mcp_server.py --org copperline-coffee --columns
      (prints which column was found for what in each CSV, and exits)

--org takes a folder path, or the name of a folder in week-05.

The server talks MCP over stdin/stdout, so nothing in server mode may print to
stdout; all logging goes to stderr.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent                  # .../UMW-GBUS-562/week-05
DEFAULT_ORG_DIR = HERE / "brookfield-bikes"
DATA_FILES = ("parts.csv", "inbound.csv", "orders.csv", "bill-of-materials.csv", "suppliers.csv")


def log(message: str) -> None:
    """Log to stderr; stdout carries the MCP protocol."""
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Company folder and column names
# --------------------------------------------------------------------------- #
def resolve_org(value: str | Path) -> Path:
    """--org: a folder path, or the name of a folder in week-05."""
    path = Path(value)
    if not path.is_dir() and (HERE / path).is_dir():
        path = HERE / path
    return path.resolve()


def org_name(org_dir: Path) -> str:
    """A display name from the folder name: copperline-coffee -> Copperline Coffee."""
    return " ".join(w.capitalize() for w in re.split(r"[-_ ]+", org_dir.name) if w)


_PART = (r"^part(_?(id|no|number|code))?$", r"part")
_SUPPLIER = (r"^(supplier|vendor)(_?name)?$", r"supplier|vendor")
_PRODUCT = (r"^(product|model|item|sku)(_?(name|id))?$", r"product|model|item|sku")
_QTY = (r"^(qty|quantity|units|count)$", r"qty|quantity|units|count|number_of")

# For each file: our name for a column -> patterns tried in order against the normalized
# header (lowercase; anything other than a letter or digit becomes "_"). The first pattern
# that matches exactly one column not yet taken wins; a pattern that matches two columns
# is an error rather than a guess. Columns are found in the order listed, so e.g.
# contact_email is taken before "customer" could match a customer_email column.
COLUMNS: dict[str, dict[str, tuple[str, ...]]] = {
    "parts.csv": {
        "part_id": _PART,
        "on_hand": (r"on_?hand", r"stock|inventory|available"),
        "supplier": _SUPPLIER,
        "description": (r"desc", r"name"),
    },
    "inbound.csv": {
        "po_number": (r"^po(_?(number|no|id))?$", r"purchase_?order|(^|_)po(_|$)"),
        "part_id": _PART,
        "supplier": _SUPPLIER,
        "arrives_in_days": (r"arriv", r"eta|day"),
        "qty": _QTY,
    },
    "orders.csv": {
        "order_id": (r"^order(_?(id|no|number))?$", r"order"),
        "contact_email": (r"e_?mail",),
        "customer": (r"^(customer|client|account)(_?name)?$", r"customer|client|account"),
        "late_penalty": (r"penalt", r"late"),
        "ship_day": (r"ship.*day|day.*ship", r"(due|deliver|need).*day", r"ship"),
        "product": _PRODUCT,
        "quantity": _QTY,
    },
    "bill-of-materials.csv": {
        "part_id": _PART,
        "per_unit": (r"(^|_)per(_|$)", r"qty|quantity|count|units"),
        "product": _PRODUCT,
    },
}


def normalize(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def find_columns(filename: str, header: list[str]) -> dict[str, str]:
    """Our column name -> the header in this file. Raises ValueError if one can't be found."""
    left = {h: normalize(h) for h in header if h and h.strip()}
    found: dict[str, str] = {}
    for name, patterns in COLUMNS[filename].items():
        for pattern in patterns:
            hits = [h for h, n in left.items() if re.search(pattern, n)]
            if len(hits) > 1:
                raise ValueError(f"{filename}: can't tell which column holds {name} "
                                 f"({', '.join(hits)} both fit). Header: {', '.join(header)}")
            if hits:
                found[name] = hits[0]
                del left[hits[0]]
                break
        else:
            raise ValueError(f"{filename}: no column for {name}. Header: {', '.join(header)}")
    return found


def read_table(path: Path) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Rows keyed by our column names, and the column map used."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = [h.strip() for h in next(reader, [])]
        cols = find_columns(path.name, header)
        index = {name: header.index(h) for name, h in cols.items()}
        rows = []
        for raw in reader:
            if not any(cell.strip() for cell in raw):
                continue
            rows.append({name: (raw[i].strip() if i < len(raw) else "") for name, i in index.items()})
    return rows, cols


def load_table(org_dir: Path, filename: str) -> list[dict[str, str]]:
    return read_table(org_dir / filename)[0]


def column_report(org_dir: Path, only_renamed: bool = True) -> list[str]:
    """One line per file: which header was found for each column. Raises ValueError."""
    lines = []
    for filename in COLUMNS:
        cols = read_table(org_dir / filename)[1]
        pairs = [f"{name}={h}" for name, h in cols.items() if not only_renamed or normalize(h) != name]
        if pairs:
            lines.append(f"{filename}: {', '.join(pairs)}")
    return lines


def to_int(value: str, what: str) -> int:
    try:
        return int(float(value))
    except ValueError:
        raise ToolError(f"{what} should be a number, got {value!r}.")


def is_yes(value: str) -> bool:
    return value.strip().lower() in ("yes", "y", "true", "1")


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass
class SupplyData:
    parts: dict[str, dict[str, str]]          # part_id -> row
    inbound: list[dict[str, str]]
    orders: list[dict[str, str]]
    bom: list[dict[str, str]]

    @classmethod
    def load(cls, org_dir: Path) -> "SupplyData":
        """Read the CSVs fresh, so edits to the data show up on the next tool call."""
        try:
            parts = {r["part_id"].upper(): r for r in load_table(org_dir, "parts.csv")}
            return cls(parts=parts,
                       inbound=load_table(org_dir, "inbound.csv"),
                       orders=load_table(org_dir, "orders.csv"),
                       bom=load_table(org_dir, "bill-of-materials.csv"))
        except ValueError as exc:
            raise ToolError(str(exc))

    def part(self, part_id: str) -> dict[str, str]:
        key = (part_id or "").strip().upper()
        if key not in self.parts:
            raise ToolError(f"No part {part_id!r}. Known parts: {', '.join(sorted(self.parts))}.")
        return self.parts[key]

    def shipments(self, part_id: str) -> list[dict[str, Any]]:
        return sorted(({"po_number": r["po_number"], "supplier": r["supplier"],
                        "qty": to_int(r["qty"], "inbound qty"),
                        "arrives_in_days": to_int(r["arrives_in_days"], "inbound arrival day")}
                       for r in self.inbound if r["part_id"].upper() == part_id),
                      key=lambda s: s["arrives_in_days"])

    def orders_needing(self, part_id: str) -> list[dict[str, Any]]:
        per_unit = {r["product"]: to_int(r["per_unit"], "parts per unit")
                    for r in self.bom if r["part_id"].upper() == part_id}
        out = []
        for r in self.orders:
            if r["product"] in per_unit:
                units = to_int(r["quantity"], f"order {r['order_id']} quantity")
                out.append({"order_id": r["order_id"], "customer": r["customer"], "product": r["product"],
                            "units": units, "parts_per_unit": per_unit[r["product"]],
                            "parts_needed": units * per_unit[r["product"]],
                            "ship_day": to_int(r["ship_day"], f"order {r['order_id']} ship day"),
                            "late_penalty": is_yes(r["late_penalty"])})
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
    that many // parts_per_unit units. The rest is ready on the first arrival day
    when supplied(d) >= before_k + parts_needed (None if no shipment ever covers it).
    """
    arrivals = sorted(arrivals, key=lambda a: a[1])

    def supplied(day: int) -> int:
        return on_hand + sum(q for q, d in arrivals if d <= day)

    results, before = [], 0
    for o in orders:
        need, ship = o["parts_needed"], o["ship_day"]
        have = max(0, min(need, supplied(ship) - before))
        units_on_time = have // o["parts_per_unit"]
        units_late = o["units"] - units_on_time
        ready_day = ship
        if units_late:
            ready_day = next((d for d in sorted({d for _, d in arrivals} | {ship})
                              if d >= ship and supplied(d) >= before + need), None)
        results.append({
            "order_id": o["order_id"], "customer": o["customer"], "product": o["product"],
            "units_ordered": o["units"], "units_on_time": units_on_time,
            "units_short": units_late,                      # units that miss the ship day
            "parts_short": need - have,
            "ship_day": ship, "ready_day": ready_day,
            "days_late": None if ready_day is None else ready_day - ship,
            "late_penalty": o["late_penalty"],
            "at_risk": units_late > 0,
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
    on_hand = to_int(part["on_hand"], f"{pid} on hand")

    arrivals = [(s["qty"], s["arrives_in_days"]) for s in scheduled if s["po_number"].upper() != po]
    arrivals += [(s.qty, s.arrives_in_days) for s in revised_shipments if s.qty]
    orders = coverage(on_hand, arrivals, data.orders_needing(pid))
    at_risk = [o for o in orders if o["at_risk"]]

    result: dict[str, Any] = {
        "part_id": pid,
        "po_number": original["po_number"],
        "original_shipment": {"qty": original["qty"], "arrives_in_days": original["arrives_in_days"]},
        "revised_shipments": [s.model_dump() for s in sorted(revised_shipments, key=lambda s: s.arrives_in_days)],
        "on_hand": on_hand,
        "rules": "Parts go to orders in ship-day order; an order ships the units it has parts for "
                 "on its ship day and the rest on ready_day. units_short counts units of the product.",
        "orders_at_risk": at_risk,
        "orders_on_time": [o["order_id"] for o in orders if not o["at_risk"]],
        "total_units_late": sum(o["units_short"] for o in at_risk),
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
def create_server(org_dir: Path) -> MCPServer:
    name = org_name(org_dir)
    server = MCPServer(
        name=f"{org_dir.name}-supply",
        instructions=f"{name} supply data: parts, scheduled shipments, customer orders, and "
                     "coverage checks. Days are counted from today (day 0).",
    )

    @server.tool(description=(
        "Get one part's description, stock on hand, supplier, and its scheduled inbound shipments "
        "(purchase order number, quantity, and arrives_in_days, counted from today = day 0)."))
    def get_part(part_id: str) -> dict[str, Any]:
        data = SupplyData.load(org_dir)
        part = data.part(part_id)
        pid = part["part_id"].upper()
        return {"part_id": pid, "description": part["description"],
                "on_hand": to_int(part["on_hand"], f"{pid} on hand"),
                "supplier": part["supplier"], "scheduled_shipments": data.shipments(pid)}

    @server.tool(description=(
        "List the open customer orders whose product uses this part, in ship-day order: "
        "order_id, customer, product, units, parts_per_unit, parts_needed, ship_day (days from "
        "today) and late_penalty."))
    def get_orders_for_part(part_id: str) -> dict[str, Any]:
        data = SupplyData.load(org_dir)
        pid = data.part(part_id)["part_id"].upper()
        orders = data.orders_needing(pid)
        return {"part_id": pid, "orders": orders, "total_parts_needed": sum(o["parts_needed"] for o in orders)}

    @server.tool(description=(
        "Check whether customer orders can still ship on time if one purchase order's shipment "
        "changes. Give the part, the purchase order number, and the revised shipments for that "
        "PO (each with qty and arrives_in_days, counted from today = day 0); they replace the "
        "PO's scheduled shipment, and other scheduled shipments stay as they are. To check the "
        "current schedule, pass the PO's scheduled shipment unchanged. Returns each order at "
        "risk with units_short (units of the product that miss the ship day), ship_day, "
        "ready_day (when the rest can ship), days_late and late_penalty. Parts go to orders in "
        "ship-day order, and an order ships the units it has parts for on time and the rest later."))
    def check_coverage(part_id: str, po_number: str, revised_shipments: list[Shipment]) -> dict[str, Any]:
        return check(SupplyData.load(org_dir), part_id, po_number, revised_shipments)

    return server


def parse_shipment(text: str) -> Shipment:
    qty, _, day = text.partition("@")
    try:
        return Shipment(qty=int(qty), arrives_in_days=int(day))
    except ValueError:
        raise argparse.ArgumentTypeError(f"shipment must look like QTY@DAY, e.g. 120@5 (got {text!r})")


def check_org(org_dir: Path) -> str | None:
    """None if the folder has the five CSVs and every column can be found; else the problem."""
    missing = [f for f in DATA_FILES if not (org_dir / f).is_file()]
    if missing:
        return f"company folder {org_dir} is missing: {', '.join(missing)}"
    try:
        column_report(org_dir)
    except ValueError as exc:
        return str(exc)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server with one company's supply data tools.")
    parser.add_argument("--org", "--data", dest="org", default=str(DEFAULT_ORG_DIR), metavar="FOLDER",
                        help="company folder with the five CSV files (default: week-05/brookfield-bikes)")
    parser.add_argument("--check", nargs="+", metavar="ARG",
                        help="test check_coverage without MCP: PART_ID PO_NUMBER QTY@DAY [QTY@DAY ...]")
    parser.add_argument("--columns", action="store_true",
                        help="print which column was found for what in each CSV, and exit")
    args = parser.parse_args()
    org_dir = resolve_org(args.org)
    problem = check_org(org_dir)
    if problem:
        parser.error(problem)

    if args.columns:
        print("\n".join(column_report(org_dir, only_renamed=False)))
        return
    if args.check:
        if len(args.check) < 2:
            parser.error("--check needs PART_ID PO_NUMBER and zero or more QTY@DAY shipments")
        shipments = [parse_shipment(s) for s in args.check[2:]]
        try:
            result = check(SupplyData.load(org_dir), args.check[0], args.check[1], shipments)
        except ToolError as exc:
            sys.exit(f"Error: {exc}")
        print(json.dumps(result, indent=2))
        return

    log(f"{org_name(org_dir)} supply MCP server ready (stdio), data: {org_dir.name}")
    for line in column_report(org_dir):
        log(f"columns found under other names - {line}")
    create_server(org_dir).run("stdio")


if __name__ == "__main__":
    main()
