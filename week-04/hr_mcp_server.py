"""hr_mcp_server.py - an MCP server with two HR tools for Hartwell Foods.

Tools
  search_policy(query)    Returns the 4 HR-document chunks closest to the query,
                          each with its source file, so answers can cite it.
  get_leave_balance(...)  Looks up vacation / sick / personal days left in
                          hartwell-hr/employees.csv. Two modes:
                            * default (HR staff): takes an employee's work email.
                            * --as-employee EMAIL: takes no inputs and can only
                              return that one employee's balance.

The search reuses the retrieval half of week-03/ask.py (section-based chunks
with a document/section header, Chroma's built-in embedding model, cosine
similarity). It does not call Claude: the app using the tools writes the answer.
The Chroma database lives in week-04/chroma_db/ (git-ignored) and is rebuilt
automatically when the documents change.

Usage (from the top of the repository):
  .venv\\Scripts\\python.exe week-04\\hr_mcp_server.py
  .venv\\Scripts\\python.exe week-04\\hr_mcp_server.py --as-employee megan.turner@hartwellfoods.example
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
DATA_DIR = HERE / "hartwell-hr"
DOCS_DIR = DATA_DIR / "hr-docs"
EMPLOYEES_CSV = DATA_DIR / "employees.csv"
DB_DIR = HERE / "chroma_db"                         # git-ignored (chroma_db/ in .gitignore)
FINGERPRINT_FILE = DB_DIR / "docs_fingerprint.json"

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
# Leave balances
# --------------------------------------------------------------------------- #
BALANCE_FIELDS = ("vacation_days_left", "sick_days_left", "personal_days_left")


def normalize_email(value: str) -> str:
    """'Dana <Dana.Whitfield@x.example>' -> 'dana.whitfield@x.example'."""
    return parseaddr(value or "")[1].strip().lower()


def find_employee(email: str) -> dict[str, Any] | None:
    """Read employees.csv fresh on every call, so edits take effect without a restart."""
    wanted = normalize_email(email)
    if not wanted:
        return None
    with EMPLOYEES_CSV.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if normalize_email(row.get("email", "")) == wanted:
                return row
    return None


def no_record_error(email: str) -> ToolError:
    return ToolError(
        f"No employee record matches '{email}'. Do NOT state, estimate or guess any leave "
        "balance in your reply. Tell the sender that HR could not find their record and "
        "a person from HR will confirm their balance.")


def balance_result(email: str) -> dict[str, Any]:
    row = find_employee(email)
    if row is None:
        raise no_record_error(email)
    try:
        balances = {k: int(row[k]) for k in BALANCE_FIELDS}
    except (KeyError, TypeError, ValueError):
        raise ToolError(f"The record for '{email}' has a missing or invalid balance. "
                        "Do NOT state any leave balance; a person from HR must check it.")
    return {
        "email": row["email"],
        "name": row["name"],
        "department": row["department"],
        "manager": row["manager"] or None,
        **balances,
        "source": "employees.csv",
    }


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #
def create_server(collection, as_employee: str | None) -> MCPServer:
    server = MCPServer(
        name="hartwell-hr",
        instructions="Hartwell Foods HR tools: search the HR policy documents and look up "
                     "employee leave balances. Cite the source file for every policy fact.",
    )

    @server.tool()
    def search_policy(query: str) -> dict[str, Any]:
        """Search Hartwell Foods' HR policy documents (leave, jury duty, bereavement,
        parental and family leave, benefits enrollment, remote work, reporting workplace
        concerns). Returns the 4 most relevant passages, each with its source file, document
        title and section. Answer only from these passages and cite the source file for each
        fact. A low similarity score means the passage may not answer the question."""
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
        @server.tool(name="get_leave_balance")
        def get_own_leave_balance() -> dict[str, Any]:
            """Get the leave balance (vacation, sick and personal days left) of the employee
            this session belongs to. Takes no inputs: it can only return that one employee's
            balance and cannot look up anyone else."""
            return balance_result(as_employee)
    else:
        @server.tool()
        def get_leave_balance(email: str) -> dict[str, Any]:
            """Get an employee's leave balance (vacation, sick and personal days left), plus
            their name, department and manager, by their work email address
            (e.g. dana.whitfield@hartwellfoods.example). For HR staff use."""
            return balance_result(email)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server with Hartwell Foods HR tools.")
    parser.add_argument("--as-employee", metavar="EMAIL",
                        help="employee mode: get_leave_balance takes no inputs and returns only this employee's balance")
    parser.add_argument("--rebuild", action="store_true",
                        help="rebuild the HR document database from hartwell-hr/hr-docs, then exit")
    args = parser.parse_args()

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
    log("hartwell-hr MCP server ready (stdio).")
    create_server(collection, as_employee).run("stdio")


if __name__ == "__main__":
    main()
