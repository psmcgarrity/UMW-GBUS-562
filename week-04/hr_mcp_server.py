"""hr_mcp_server.py - an MCP server with two HR tools for any organization.

The organization is a folder (--org; default week-04/hartwell-hr) laid out as:
  hr-docs/            HTML policy documents
  employees.csv       one row per employee (column names may differ per organization)
  sample-emails/      example employee emails (used by agent.py / load_inbox.py)
  system_prompt.txt   the agent's system prompt (used by agent.py)

Tools
  search_policy(query)    Returns the 4 HR-document chunks closest to the query,
                          each with its source file, so answers can cite it.
  get_leave_balance(...)  Looks up an employee's leave balances in employees.csv.
                          The email column and the balance columns are worked out
                          from the header row (e.g. "email" / "Email Address";
                          "vacation_days_left" / "PTO Days Remaining"). Two modes:
                            * default (HR staff): takes an employee's work email.
                            * --as-employee EMAIL: takes no inputs and can only
                              return that one employee's balance.

The search reuses the retrieval half of week-03/ask.py (section-based chunks
with a document/section header, Chroma's built-in embedding model, cosine
similarity). It does not call Claude: the app using the tools writes the answer.
Each organization gets its own Chroma database in
week-04/chroma_db/<folder name>-<short hash of its path>/ (git-ignored), rebuilt
automatically when its documents change.

Usage (from the top of the repository):
  .venv\\Scripts\\python.exe week-04\\hr_mcp_server.py
  .venv\\Scripts\\python.exe week-04\\hr_mcp_server.py --as-employee megan.turner@hartwellfoods.example
  .venv\\Scripts\\python.exe week-04\\hr_mcp_server.py --org week-04\\ridgeline-hotels --as-employee jenna.morales@ridgelinehotels.example
  .venv\\Scripts\\python.exe week-04\\hr_mcp_server.py --rebuild   (rebuild the database, then exit)

The server talks MCP over stdin/stdout, so nothing in this file may print to
stdout; all logging goes to stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve().parent              # .../UMW-GBUS-562/week-04
DEFAULT_ORG_DIR = HERE / "hartwell-hr"
DB_ROOT = HERE / "chroma_db"                        # git-ignored; one sub-folder per organization

# Set by use_org() from the --org option.
ORG_DIR = DEFAULT_ORG_DIR
DOCS_DIR = ORG_DIR / "hr-docs"
EMPLOYEES_CSV = ORG_DIR / "employees.csv"
DB_DIR = DB_ROOT
FINGERPRINT_FILE = DB_DIR / "docs_fingerprint.json"
ORG_NAME = ""

COLLECTION_NAME = "hr_documents"
TOP_K = 4
MAX_CHUNK_WORDS = 350        # longer sections are split at paragraph boundaries
SUPPORTED_SUFFIXES = {".html", ".htm"}

# Same chunking rules as week-03/ask.py.
FORWARD_REFERENCE = re.compile(r"\b(as )?(described|explained|listed|shown|set out) below\b", re.I)
DEFINITIONS_HEADING = re.compile(r"^(how .+ works|definitions?|key terms|glossary|terms used)\b", re.I)


def log(message: str) -> None:
    """Log to stderr; stdout carries the MCP protocol."""
    print(message, file=sys.stderr, flush=True)


def org_display_name(org_dir: Path) -> str:
    """'Ridgeline Hotels' from system_prompt.txt ('You are the HR assistant for X.'), else from the folder name."""
    try:
        m = re.search(r"assistant for ([^.\n]+)\.", (org_dir / "system_prompt.txt").read_text(encoding="utf-8"))
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    return org_dir.name.replace("-", " ").replace("_", " ").title()


def use_org(org_dir: Path) -> None:
    """Point the server at an organization folder and its own Chroma database."""
    global ORG_DIR, DOCS_DIR, EMPLOYEES_CSV, DB_DIR, FINGERPRINT_FILE, ORG_NAME
    ORG_DIR = org_dir.resolve()
    DOCS_DIR = ORG_DIR / "hr-docs"
    EMPLOYEES_CSV = ORG_DIR / "employees.csv"
    missing = [p.name for p in (DOCS_DIR, EMPLOYEES_CSV) if not p.exists()]
    if missing:
        sys.exit(f"Organization folder {ORG_DIR} is missing: {', '.join(missing)}")
    path_hash = hashlib.sha256(str(ORG_DIR).lower().encode()).hexdigest()[:8]
    DB_DIR = DB_ROOT / f"{ORG_DIR.name}-{path_hash}"
    FINGERPRINT_FILE = DB_DIR / "docs_fingerprint.json"
    ORG_NAME = org_display_name(ORG_DIR)


# --------------------------------------------------------------------------- #
# HTML parsing and chunking (adapted from week-03/ask.py)
# --------------------------------------------------------------------------- #
@dataclass
class Section:
    heading: str
    paragraphs: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.paragraphs).strip()


@dataclass
class ParsedDoc:
    title: str
    status: str                 # e.g. "Version 6.1, current. Owner: Human Resources."
    sections: list[Section]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _title_from_filename(path: Path) -> str:
    return path.stem.replace("-", " ").replace("_", " ").title()


def _sections_from_preamble(preamble: list[str], sections: list[Section]) -> tuple[str, list[Section]]:
    """Short text before the first heading is a status line; longer text becomes an Overview section."""
    text = " ".join(preamble).strip()
    if not text:
        return "", sections
    if len(text.split()) <= 50 and sections:
        return text, sections
    return "", [Section("Overview", preamble)] + sections


def parse_html(path: Path) -> ParsedDoc:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form", "svg"]):
        tag.decompose()
    for tag in soup.select(".bar, .navbar, .breadcrumb"):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup

    h1 = root.find("h1")
    title = _clean(h1.get_text()) if h1 else ""
    if not title and soup.title:
        title = _clean(soup.title.get_text().split("|")[0])
    title = title or _title_from_filename(path)

    # Sections start at the highest heading level used below the title (normally h2).
    levels = [n for n in range(2, 7) if root.find(f"h{n}")]
    split_tag = f"h{levels[0]}" if levels else None

    block_tags = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "pre", "blockquote", "dt", "dd"]
    status, preamble, sections = "", [], []
    for el in root.find_all(block_tags):
        # Skip blocks nested inside another block we already take (e.g. <p> inside <li>).
        if any(parent.name in block_tags for parent in el.parents if parent is not root):
            continue
        if el.name == "h1":
            continue
        if el.name == split_tag:
            sections.append(Section(_clean(el.get_text())))
            continue
        if el.name == "p" and "meta" in (el.get("class") or []):
            status = _clean(el.get_text())
            continue
        if el.name == "tr":
            text = " | ".join(_clean(c.get_text()) for c in el.find_all(["th", "td"]))
        elif el.name == "li":
            text = "- " + _clean(el.get_text())
        elif el.name in ("h3", "h4", "h5", "h6"):
            text = _clean(el.get_text()) + ":"
        else:
            text = _clean(el.get_text())
        if not text or text == "- ":
            continue
        (sections[-1].paragraphs if sections else preamble).append(text)

    pre_status, sections = _sections_from_preamble(preamble, sections)
    return ParsedDoc(title, status or pre_status, sections)


def _merge_forward_references(sections: list[Section]) -> list[Section]:
    """A section that says 'as described below' is stored together with the next section."""
    merged: list[Section] = []
    i = 0
    while i < len(sections):
        sec = sections[i]
        if FORWARD_REFERENCE.search(sec.text) and i + 1 < len(sections):
            nxt = sections[i + 1]
            sec = Section(f"{sec.heading}; {nxt.heading}",
                          sec.paragraphs + [f"{nxt.heading}:"] + nxt.paragraphs)
            i += 1
        merged.append(sec)
        i += 1
    return merged


def _split_long(paragraphs: list[str]) -> list[list[str]]:
    """Split a long section at paragraph boundaries into pieces of <= MAX_CHUNK_WORDS words."""
    pieces, current, count = [], [], 0
    for p in paragraphs:
        words = len(p.split())
        if current and count + words > MAX_CHUNK_WORDS:
            pieces.append(current)
            current, count = [], 0
        current.append(p)
        count += words
    if current:
        pieces.append(current)
    return pieces


def chunk_document(path: Path) -> list[dict]:
    parsed = parse_html(path)
    source = path.relative_to(DOCS_DIR).as_posix()

    version = re.search(r"\bVersion\s+[\d.]+", parsed.status or "", re.I)
    doc_label = parsed.title + (f" ({version.group(0)})" if version and version.group(0) not in parsed.title else "")

    sections = [s for s in parsed.sections if s.text]
    context = ""
    if len(sections) > 1 and DEFINITIONS_HEADING.match(sections[0].heading):
        context = " ".join(sections[0].paragraphs)
    sections = _merge_forward_references(sections)

    chunks = []
    for s_no, sec in enumerate(sections):
        pieces = _split_long(sec.paragraphs)
        for p_no, piece in enumerate(pieces):
            heading = sec.heading + (f" (part {p_no + 1} of {len(pieces)})" if len(pieces) > 1 else "")
            header = [f"Document: {doc_label}", f"Section: {heading}"]
            if context and s_no > 0:
                header.append(f"Context: {context}")
            chunks.append({
                "id": f"{source}::{len(chunks)}",
                "text": "\n".join(header) + "\n\n" + "\n".join(piece),
                "metadata": {"source": source, "title": parsed.title, "section": heading,
                             "status": parsed.status, "chunk_index": len(chunks)},
            })
    return chunks


def find_documents() -> list[Path]:
    return sorted(p for p in DOCS_DIR.rglob("*")
                  if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
                  and not p.name.startswith(("~", ".")))


def docs_fingerprint() -> str:
    """Changes whenever a document is added, removed or edited."""
    h = hashlib.sha256()
    for p in find_documents():
        st = p.stat()
        h.update(f"{p.relative_to(DOCS_DIR).as_posix()}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Chroma database
# --------------------------------------------------------------------------- #
def _client():
    import chromadb
    from chromadb.config import Settings
    return chromadb.PersistentClient(path=str(DB_DIR), settings=Settings(anonymized_telemetry=False))


def _collection(client, create: bool = False):
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    kwargs = dict(name=COLLECTION_NAME, embedding_function=DefaultEmbeddingFunction())
    if create:
        return client.create_collection(configuration={"hnsw": {"space": "cosine"}}, **kwargs)
    return client.get_collection(**kwargs)


def build_index() -> None:
    docs = find_documents()
    if not docs:
        raise RuntimeError(f"No HTML documents found in {DOCS_DIR}")

    all_chunks = []
    for path in docs:
        try:
            chunks = chunk_document(path)
        except Exception as exc:  # one bad file shouldn't stop the rest
            log(f"  skipped {path.name}: {exc}")
            continue
        log(f"  {path.name}: {len(chunks)} chunks")
        all_chunks.extend(chunks)

    DB_DIR.mkdir(parents=True, exist_ok=True)
    client = _client()
    with contextlib.suppress(Exception):
        client.delete_collection(COLLECTION_NAME)   # no existing collection is fine
    collection = _collection(client, create=True)
    log("Embedding chunks (the first run downloads Chroma's embedding model, ~80 MB)...")
    collection.add(ids=[c["id"] for c in all_chunks],
                   documents=[c["text"] for c in all_chunks],
                   metadatas=[c["metadata"] for c in all_chunks])
    FINGERPRINT_FILE.write_text(json.dumps({"fingerprint": docs_fingerprint()}))
    log(f"Indexed {len(all_chunks)} chunks from {len(docs)} documents into {DB_DIR}")


def open_index():
    """Return the collection, (re)building it if it is missing or the documents changed."""
    # Chroma and its model download must never write to stdout (the MCP channel).
    with contextlib.redirect_stdout(sys.stderr):
        try:
            saved = json.loads(FINGERPRINT_FILE.read_text())["fingerprint"]
            collection = _collection(_client())
        except Exception:
            saved, collection = None, None
        if collection is None or saved != docs_fingerprint():
            log("HR document database is missing or out of date - rebuilding it.")
            build_index()
            collection = _collection(_client())
        collection.query(query_texts=["warm-up"], n_results=1)  # load the embedding model now
    return collection


# --------------------------------------------------------------------------- #
# Leave balances (column names are worked out from the header row)
# --------------------------------------------------------------------------- #
EMAIL_HEADER = re.compile(r"e-?mail", re.I)
BALANCE_HEADER = re.compile(r"\b(days?|hours?|balance|remaining|left|pto|vacation|sick|personal|leave|"
                            r"holiday|annual|accrued|available)\b", re.I)
MANAGER_HEADER = re.compile(r"manager|supervisor|reports? to|boss", re.I)


def normalize_email(value: str) -> str:
    """'Dana <Dana.Whitfield@x.example>' -> 'dana.whitfield@x.example'."""
    return parseaddr(value or "")[1].strip().lower()


def snake(header: str) -> str:
    """'PTO Days Remaining' -> 'pto_days_remaining'."""
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def _number(value: str) -> int | float | None:
    try:
        n = float((value or "").strip())
    except ValueError:
        return None
    return int(n) if n.is_integer() else n


def _looks_like_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalize_email(value)))


@dataclass
class EmployeeFile:
    rows: list[dict[str, str]]
    email_col: str
    balance_cols: list[str]


def read_employees() -> EmployeeFile:
    """Read employees.csv fresh on every call (edits take effect without a restart) and
    work out from the header row which column holds the email address and which hold
    leave balances."""
    with EMPLOYEES_CSV.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = [h for h in (reader.fieldnames or []) if h and h.strip()]
        rows = [r for r in reader if any((v or "").strip() for v in r.values() if isinstance(v, str))]

    def share(col: str, test) -> float:
        values = [(r.get(col) or "").strip() for r in rows]
        values = [v for v in values if v]
        return sum(map(test, values)) / len(values) if values else 0.0

    # Email: a header that says "email" (not the manager's), else a column whose values are addresses.
    candidates = [h for h in headers if EMAIL_HEADER.search(h) and not MANAGER_HEADER.search(h)]
    candidates += [h for h in headers if h not in candidates and share(h, _looks_like_email) >= 0.8]
    candidates = [h for h in candidates if share(h, _looks_like_email) >= 0.5] or candidates
    if not candidates:
        raise ToolError(f"{EMPLOYEES_CSV.name} has no column with email addresses; "
                        "a person from HR must look up the balance.")
    email_col = candidates[0]

    # Balances: a header about days / PTO / leave / etc. whose values are numbers.
    balance_cols = [h for h in headers if h != email_col and BALANCE_HEADER.search(snake(h).replace("_", " "))
                    and share(h, lambda v: _number(v) is not None) >= 0.8]
    return EmployeeFile(rows, email_col, balance_cols)


def _row_for(emp: EmployeeFile, email: str) -> dict[str, str] | None:
    wanted = normalize_email(email)
    if not wanted:
        return None
    return next((r for r in emp.rows if normalize_email(r.get(emp.email_col) or "") == wanted), None)


def find_employee(email: str) -> dict[str, str] | None:
    return _row_for(read_employees(), email)


def no_record_error(email: str) -> ToolError:
    return ToolError(
        f"No employee record matches '{email}'. Do NOT state, estimate or guess any leave "
        "balance in your reply. Tell the sender that HR could not find their record and "
        "a person from HR will confirm their balance.")


def balance_result(email: str) -> dict[str, Any]:
    emp = read_employees()
    if not emp.balance_cols:
        raise ToolError(f"No leave-balance columns were found in {EMPLOYEES_CSV.name}. "
                        "Do NOT state any leave balance; a person from HR must check it.")
    row = _row_for(emp, email)
    if row is None:
        raise no_record_error(email)

    result: dict[str, Any] = {"email": normalize_email(row[emp.email_col])}
    for col in (h for h in row if h and h.strip()):       # name, department, manager... as text
        if col != emp.email_col and col not in emp.balance_cols:
            result[snake(col)] = (row[col] or "").strip() or None
    for col in emp.balance_cols:
        n = _number(row.get(col) or "")
        if n is None:
            raise ToolError(f"The record for '{email}' has a missing or invalid '{col}' value. "
                            "Do NOT state any leave balance; a person from HR must check it.")
        result[snake(col)] = n
    result["source"] = EMPLOYEES_CSV.name
    return result


def describe_employee_file() -> str:
    try:
        emp = read_employees()
        return (f"email column '{emp.email_col}'; balance columns: "
                + (", ".join(f"'{c}'" for c in emp.balance_cols) or "NONE FOUND"))
    except Exception as exc:
        return f"could not read it ({exc})"


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #
def create_server(collection, as_employee: str | None) -> MCPServer:
    server = MCPServer(
        name=ORG_DIR.name,
        instructions=f"{ORG_NAME} HR tools: search the HR policy documents and look up "
                     "employee leave balances. Cite the source file for every policy fact.",
    )
    doc_titles = ", ".join(_title_from_filename(p) for p in find_documents())
    try:
        balance_names = ", ".join(read_employees().balance_cols) or "leave balances"
    except Exception:
        balance_names = "leave balances"

    @server.tool(description=(
        f"Search {ORG_NAME}'s HR policy documents ({doc_titles}). Returns the {TOP_K} most "
        "relevant passages, each with its source file, document title and section. Answer only "
        "from these passages and cite the source file for each fact. A low similarity score "
        "means the passage may not answer the question."))
    def search_policy(query: str) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            raise ToolError("The search query is empty. Describe the policy question in words.")
        res = collection.query(query_texts=[query], n_results=TOP_K,
                               include=["documents", "metadatas", "distances"])
        results = [
            {"source": meta["source"], "title": meta["title"], "section": meta["section"],
             "similarity": round(1 - dist, 3), "text": doc}
            for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0])
        ]
        return {"query": query, "results": results}

    if as_employee:
        @server.tool(name="get_leave_balance", description=(
            f"Get the leave balances ({balance_names}) of the employee this session belongs to, "
            "plus the other details in their employee record. Takes no inputs: it can only "
            "return that one employee's record and cannot look up anyone else."))
        def get_own_leave_balance() -> dict[str, Any]:
            return balance_result(as_employee)
    else:
        @server.tool(description=(
            f"Get an employee's leave balances ({balance_names}), plus the other details in "
            "their employee record, by their work email address. For HR staff use."))
        def get_leave_balance(email: str) -> dict[str, Any]:
            return balance_result(email)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server with HR tools for one organization.")
    parser.add_argument("--org", type=Path, default=DEFAULT_ORG_DIR, metavar="FOLDER",
                        help="organization folder with hr-docs/ and employees.csv (default: week-04/hartwell-hr)")
    parser.add_argument("--as-employee", metavar="EMAIL",
                        help="employee mode: get_leave_balance takes no inputs and returns only this employee's balance")
    parser.add_argument("--rebuild", action="store_true",
                        help="rebuild the organization's HR document database, then exit")
    args = parser.parse_args()
    use_org(args.org)
    log(f"Organization: {ORG_NAME} ({ORG_DIR.name})")
    log(f"Employee file: {describe_employee_file()}")

    if args.rebuild:
        with contextlib.redirect_stdout(sys.stderr):
            build_index()
        return

    as_employee = None
    if args.as_employee is not None:
        as_employee = normalize_email(args.as_employee)
        if not as_employee:
            parser.error("--as-employee needs an email address")
        if find_employee(as_employee) is None:
            log(f"Warning: no employee record matches {as_employee}; get_leave_balance will return an error.")
        log(f"Employee mode: get_leave_balance returns only {as_employee}'s balance.")

    collection = open_index()
    log(f"{ORG_DIR.name} MCP server ready (stdio).")
    create_server(collection, as_employee).run("stdio")


if __name__ == "__main__":
    main()
