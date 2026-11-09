"""ask.py - answer questions about Copperline Manufacturing's finance documents.

A small retrieval-augmented generation (RAG) tool:
  1. Split every document in copperline-docs/ into self-contained chunks
     (one chunk per section, each starting with its document title and heading).
  2. Store the chunks in a local Chroma database using Chroma's built-in
     embedding model.
  3. For each question, retrieve the 4 most similar chunks and ask Claude to
     answer using only those chunks, citing the source file for each fact.

Usage (from the top of the repository):
  .venv\\Scripts\\python.exe week-03\\ask.py "How much can I spend on dinner in Boston?"
  .venv\\Scripts\\python.exe week-03\\ask.py              (interactive: ask several questions)
  .venv\\Scripts\\python.exe week-03\\ask.py --rebuild    (re-index after adding/removing documents)

Supported document types: .html/.htm, .pdf, .md/.markdown, .txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve().parent              # .../UMW-GBUS-562/week-03
DOCS_DIR = HERE / "copperline-docs"
DB_DIR = HERE / "chroma_db"                         # git-ignored
FINGERPRINT_FILE = DB_DIR / "docs_fingerprint.json"
API_KEY_FILE = HERE.parents[1] / "API-Key.txt"      # GBUS-562-course/API-Key.txt (outside the repo)

COLLECTION_NAME = "copperline_docs"
MODEL = "claude-sonnet-5"
TOP_K = 4
PREVIEW_CHARS = 150
MAX_CHUNK_WORDS = 350        # longer sections are split at paragraph boundaries

SUPPORTED_SUFFIXES = {".html", ".htm", ".pdf", ".md", ".markdown", ".txt"}

# A section containing one of these phrases depends on the section after it,
# so the two are stored as one chunk (e.g. "Alcohol ... as described below").
FORWARD_REFERENCE = re.compile(r"\b(as )?(described|explained|listed|shown|set out) below\b", re.I)

# A first section with a heading like this defines terms the rest of the
# document relies on, so its text is added as context to every other chunk.
DEFINITIONS_HEADING = re.compile(r"^(how .+ works|definitions?|key terms|glossary|terms used)\b", re.I)

SYSTEM_PROMPT = """You answer questions from Copperline Manufacturing employees about company finance policies.

Rules:
- Answer ONLY from the numbered sources provided in the user's message. Do not use outside knowledge or make assumptions about Copperline's policies.
- Cite the source file for every fact, in square brackets right after the fact, e.g. [travel-expense-policy.html].
- If the sources do not contain the answer, say clearly that the provided documents don't answer the question. If they answer only part of it, answer that part and say what is missing.
- Be concise and specific: give exact amounts, limits, deadlines, and approvers when the sources state them.
- For a yes/no question, first work out the answer by comparing the facts in the question with the rule in the sources (for example, "$70 is under the $75 limit"). Then open with a verdict - Yes, No, or Partly - that agrees with that comparison, followed by the explanation. Use "Partly" when only some of the amount or request is allowed (for example, reimbursed up to a limit, with the rest at the employee's cost). Never give a verdict that your explanation contradicts."""


# --------------------------------------------------------------------------- #
# Document parsing: every parser returns a ParsedDoc of titled sections
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
    status: str                 # e.g. "Version 4.0 (current). Replaces Version 3.2. Owner: ..."
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


def parse_pdf(path: Path) -> ParsedDoc:
    import pymupdf

    doc = pymupdf.open(path)
    lines = []  # (font size, is_bold, text, block id, y position, page height)
    for page_no, page in enumerate(doc):
        for b_no, block in enumerate(page.get_text("dict")["blocks"]):
            for line in block.get("lines", []):
                spans = [s for s in line["spans"] if s["text"].strip()]
                if not spans:
                    continue
                text = _clean("".join(s["text"] for s in spans))
                size = max(s["size"] for s in spans)
                bold = all(s["flags"] & 16 for s in spans)
                lines.append((round(size, 1), bold, text, (page_no, b_no), line["bbox"][1], page.rect.height))

    if not lines:
        raise ValueError("no extractable text (scanned PDFs need OCR first)")

    # Body text = the font size that holds the most characters.
    weights = Counter()
    for size, _, text, *_ in lines:
        weights[size] += len(text)
    body = weights.most_common(1)[0][0]

    heading_sizes = sorted({size for size, *_ in lines if size > body * 1.1})
    section_size = heading_sizes[0] if heading_sizes else None

    meta_title = (doc.metadata or {}).get("title", "").split("|")[0].strip()
    title = meta_title
    preamble, sections = [], []
    current_block, para = None, []

    def flush():
        nonlocal para
        if para:
            (sections[-1].paragraphs if sections else preamble).append(" ".join(para))
            para = []

    for size, bold, text, block_id, y, page_h in lines:
        if size < body * 0.85:                       # page headers/footers, fine print
            continue
        if section_size and size >= section_size:
            flush()
            if size > section_size:                  # title / banner lines
                if not title and size == heading_sizes[-1]:
                    title = text
                continue
            sections.append(Section(text))
            current_block = None
            continue
        if block_id != current_block:                # a new PDF text block = a new paragraph
            flush()
            current_block = block_id
        para.append(text)
    flush()

    status, sections = _sections_from_preamble(preamble, sections)
    if not sections:
        sections = [Section("Full text", preamble)]
    return ParsedDoc(title or _title_from_filename(path), status, sections)


def parse_markdown(path: Path) -> ParsedDoc:
    text = path.read_text(encoding="utf-8", errors="replace")
    title, preamble, sections = "", [], []
    levels = sorted({len(m) for m in re.findall(r"^(#{2,6})\s", text, re.M)})
    split_level = levels[0] if levels else None
    para: list[str] = []

    def flush():
        nonlocal para
        if para:
            (sections[-1].paragraphs if sections else preamble).append(" ".join(para))
            para = []

    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m and len(m.group(1)) == 1 and not title:
            flush(); title = m.group(2).strip(); continue
        if m and len(m.group(1)) == split_level:
            flush(); sections.append(Section(m.group(2).strip())); continue
        if not line:
            flush(); continue
        if re.match(r"^([-*+]|\d+[.)])\s", line):
            flush(); para.append("- " + re.sub(r"^([-*+]|\d+[.)])\s+", "", line)); flush(); continue
        para.append(line.lstrip("#").strip())
    flush()

    status, sections = _sections_from_preamble(preamble, sections)
    if not sections:
        sections = [Section("Full text", preamble)]
    return ParsedDoc(title or _title_from_filename(path), status, sections)


def parse_text(path: Path) -> ParsedDoc:
    paragraphs = [_clean(p) for p in re.split(r"\n\s*\n", path.read_text(encoding="utf-8", errors="replace"))]
    paragraphs = [p for p in paragraphs if p]
    return ParsedDoc(_title_from_filename(path), "", [Section("Full text", paragraphs)])


PARSERS = {".html": parse_html, ".htm": parse_html, ".pdf": parse_pdf,
           ".md": parse_markdown, ".markdown": parse_markdown, ".txt": parse_text}


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def _merge_forward_references(sections: list[Section]) -> list[Section]:
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
    parsed = PARSERS[path.suffix.lower()](path)
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
    h = hashlib.sha256()
    for p in find_documents():
        st = p.stat()
        h.update(f"{p.relative_to(DOCS_DIR).as_posix()}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Chroma database
# --------------------------------------------------------------------------- #
def get_client():
    import chromadb
    from chromadb.config import Settings
    return chromadb.PersistentClient(path=str(DB_DIR), settings=Settings(anonymized_telemetry=False))


def get_collection(client, create: bool = False):
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    kwargs = dict(name=COLLECTION_NAME, embedding_function=DefaultEmbeddingFunction())
    if create:
        return client.create_collection(configuration={"hnsw": {"space": "cosine"}}, **kwargs)
    return client.get_collection(**kwargs)


def build_index() -> None:
    docs = find_documents()
    if not docs:
        sys.exit(f"No supported documents found in {DOCS_DIR}")

    unsupported = sorted(p.name for p in DOCS_DIR.rglob("*")
                         if p.is_file() and p.suffix.lower() not in SUPPORTED_SUFFIXES)
    for name in unsupported:
        print(f"  skipped (unsupported file type): {name}")

    all_chunks = []
    for path in docs:
        try:
            chunks = chunk_document(path)
        except Exception as exc:  # one bad file shouldn't stop the rest
            print(f"  skipped {path.name}: {exc}")
            continue
        print(f"  {path.relative_to(DOCS_DIR).as_posix()}: {len(chunks)} chunks")
        all_chunks.extend(chunks)

    client = get_client()
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass  # no existing collection
    collection = get_collection(client, create=True)
    print("Embedding chunks (the first run downloads Chroma's embedding model, ~80 MB)...")
    collection.add(ids=[c["id"] for c in all_chunks],
                   documents=[c["text"] for c in all_chunks],
                   metadatas=[c["metadata"] for c in all_chunks])
    FINGERPRINT_FILE.write_text(json.dumps({"fingerprint": docs_fingerprint()}))
    print(f"Indexed {len(all_chunks)} chunks from {len(docs)} documents into {DB_DIR}")


def open_index():
    """Return the collection, building it on first use and warning if the documents changed."""
    client = get_client()
    try:
        collection = get_collection(client)
    except Exception:
        print("No database yet - building it now.")
        build_index()
        return get_collection(get_client())
    try:
        saved = json.loads(FINGERPRINT_FILE.read_text())["fingerprint"]
    except Exception:
        saved = None
    if saved != docs_fingerprint():
        print("Note: documents in copperline-docs have changed since the database was built.\n"
              "      Run with --rebuild to include the changes.\n")
    return collection


def retrieve(collection, question: str) -> list[dict]:
    res = collection.query(query_texts=[question], n_results=TOP_K,
                           include=["documents", "metadatas", "distances"])
    return [{"text": doc, "metadata": meta, "similarity": 1 - dist}
            for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0])]


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #
def load_api_key() -> str:
    """Read the key at runtime; it is never printed or written anywhere."""
    if API_KEY_FILE.exists():
        content = API_KEY_FILE.read_text(encoding="utf-8-sig")
        # The file may hold a label line as well as the key, so pick out the key itself.
        match = re.search(r"sk-ant-[A-Za-z0-9_\-]+", content)
        key = match.group(0) if match else content.strip()
        if key:
            return key
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    sys.exit(f"API key not found. Put it in {API_KEY_FILE} or set ANTHROPIC_API_KEY.")


def ask_claude(client, question: str, hits: list[dict]) -> str:
    sources = "\n\n".join(
        f'<source index="{i}" file="{h["metadata"]["source"]}">\n{h["text"]}\n</source>'
        for i, h in enumerate(hits, 1))
    user = (f"<sources>\n{sources}\n</sources>\n\n"
            f"Question: {question}\n\n"
            "Answer using only the sources above, citing the source file for each fact.")

    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    )
    if response.stop_reason == "refusal":
        return "Claude declined to answer this question."
    answer = "".join(b.text for b in response.content if b.type == "text").strip()
    if response.stop_reason == "max_tokens":
        answer += "\n\n[Answer cut off: reached the max_tokens limit.]"
    return answer


def answer_question(collection, claude, question: str) -> None:
    import anthropic

    hits = retrieve(collection, question)
    print(f"\nTop {len(hits)} retrieved chunks:")
    for rank, h in enumerate(hits, 1):
        preview = _clean(h["text"])[:PREVIEW_CHARS]
        print(f"  #{rank}  similarity {h['similarity']:.3f}  {h['metadata']['source']}")
        print(f"       {preview}...")

    print("\nAnswer:")
    try:
        print(ask_claude(claude, question, hits))
    except anthropic.AuthenticationError:
        print("Error: the API key was rejected. Check API-Key.txt.")
    except anthropic.NotFoundError as exc:
        print(f"Error: model or endpoint not found ({exc.message}).")
    except anthropic.RateLimitError:
        print("Error: rate limited by the API. Wait a moment and try again.")
    except anthropic.APIStatusError as exc:
        print(f"Error from the Claude API ({exc.status_code}): {exc.message}")
    except anthropic.APIConnectionError:
        print("Error: could not connect to the Claude API. Check your internet connection.")
    print()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Ask questions about Copperline Manufacturing's finance documents.")
    parser.add_argument("question", nargs="*", help="your question (omit for interactive mode)")
    parser.add_argument("--rebuild", action="store_true",
                        help="rebuild the database from copperline-docs (after adding or removing documents)")
    args = parser.parse_args()

    if args.rebuild:
        print(f"Rebuilding the database from {DOCS_DIR} ...")
        build_index()
        if not args.question:
            return

    collection = open_index()

    import anthropic
    claude = anthropic.Anthropic(api_key=load_api_key())

    if args.question:
        answer_question(collection, claude, " ".join(args.question))
        return

    print("Ask a question about Copperline's finance documents (blank line or 'quit' to exit).")
    while True:
        try:
            q = input("\nQuestion: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() in {"quit", "exit", "q"}:
            break
        answer_question(collection, claude, q)


if __name__ == "__main__":
    main()
