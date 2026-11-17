"""agent.py - an HR email agent for Hartwell Foods.

For each employee email it:
  1. reads the sender's address and the message,
  2. starts hr_mcp_server.py in employee mode (--as-employee <sender>), so
     get_leave_balance can only return the sender's own balance,
  3. lets Claude (claude-sonnet-5) use that server's tools plus save_reply,
  4. saves the reply as a draft. Nothing is ever sent.

Two ways to run it:
  --email FILE  handle one .eml file; the draft is saved in week-04/drafts/.
  --inbox       sign in to the Gmail sandbox with IMAP and handle each UNREAD
                email in the inbox as its own separate agent run. Each email is
                marked as read before work starts, so none is handled twice.
                save_reply creates a real Gmail draft replying to the sender in
                the same thread; if needs_human_review is true the draft also gets
                the Gmail label NEEDS HR REVIEW (the subject is left unchanged so
                Gmail keeps the thread together). This version only reads email and creates
                drafts - it has no way to send email.

Each run stops after 10 tool calls, or as soon as Claude answers with anything
other than a tool request, and says why. Each step is printed, then the draft and
the input / output tokens.

  --save FILE   also write everything printed to FILE (UTF-8).

Usage (from the top of the repository):
  .venv\\Scripts\\python.exe week-04\\agent.py --email week-04\\hartwell-hr\\sample-emails\\03-megan-time-off.eml
  .venv\\Scripts\\python.exe week-04\\agent.py --inbox --save week-04\\inbox-run.txt

The Anthropic API key is read from API-Key.txt, and the Gmail login from
Agent-Email.txt, both in the folder above the repository. Neither the key nor
the password is ever printed or written to a file.
"""

from __future__ import annotations

import argparse
import asyncio
import imaplib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Any, Callable

import anthropic
from bs4 import BeautifulSoup
from mcp import ClientSession, StdioServerParameters, stdio_client

from load_inbox import IMAP_HOST, IMAP_PORT, load_login   # same Agent-Email.txt login as load_inbox.py

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve().parent                  # .../UMW-GBUS-562/week-04
SERVER_SCRIPT = HERE / "hr_mcp_server.py"
SYSTEM_PROMPT_FILE = HERE / "hartwell-hr" / "system_prompt.txt"
DRAFTS_DIR = HERE / "drafts"
API_KEY_FILE = HERE.parents[1] / "API-Key.txt"         # GBUS-562-course/API-Key.txt (outside the repo)

MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000
MAX_TOOL_CALLS = 10
PREVIEW_CHARS = 300

INBOX = "INBOX"
REVIEW_LABEL = "NEEDS HR REVIEW"        # Gmail label for drafts a person must check

SAVE_REPLY_TOOL = {
    "name": "save_reply",
    "description": (
        "Save your reply to the employee as a draft for the HR team to review. "
        "This only saves the draft; it never sends anything. Call it exactly once, "
        "with the full reply text. Set needs_human_review to true if a person from "
        "HR must check or follow up (e.g. harassment, discrimination, safety, medical "
        "or legal matters, or when the policies don't answer the question)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reply_text": {"type": "string", "description": "The complete reply to the employee."},
            "needs_human_review": {"type": "boolean",
                                   "description": "true if a person from HR must review or follow up."},
        },
        "required": ["reply_text", "needs_human_review"],
        "additionalProperties": False,
    },
    "strict": True,
}

# A saver turns (reply_text, needs_human_review) into (message for Claude, draft text to print).
Saver = Callable[[str, bool], tuple[str, str]]


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def load_api_key() -> str:
    """Return the key from API-Key.txt. The file may have a label line, so take the
    line that looks like a key (starts with 'sk-ant'), else the only non-empty line."""
    try:
        lines = [l.strip() for l in API_KEY_FILE.read_text(encoding="utf-8-sig").splitlines() if l.strip()]
    except FileNotFoundError:
        sys.exit(f"API key file not found: {API_KEY_FILE}")
    keys = [l for l in lines if l.startswith("sk-ant")]
    if len(keys) == 1:
        return keys[0]
    if len(lines) == 1 and " " not in lines[0]:
        return lines[0]
    sys.exit(f"Could not find exactly one API key (a line starting with 'sk-ant') in {API_KEY_FILE}")


def redact(text: str, secret: str) -> str:
    """Remove the API key from any text before it is printed."""
    return text.replace(secret, "[API key hidden]") if secret else text


def one_line(text: str) -> str:
    """Collapse whitespace so a value is safe to put in an email header."""
    return " ".join(str(text).split())


def parse_email(raw: bytes) -> dict[str, str]:
    """Return sender, subject, plain-text body and threading headers of a raw email.
    'sender' is empty if the From: line has no address."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    name, sender = parseaddr(str(msg.get("From", "")))
    part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if part is not None:
        body = part.get_content()
        if part.get_content_type() == "text/html":
            body = BeautifulSoup(body, "html.parser").get_text("\n")
    return {
        "sender": sender.strip().lower(),
        "sender_name": one_line(name),
        "subject": one_line(msg.get("Subject", "")),
        "body": body.strip(),
        "message_id": one_line(msg.get("Message-ID", "")),
        "references": one_line(msg.get("References", "")),
    }


def read_email(path: Path) -> dict[str, str]:
    """Return the parsed contents of an .eml file."""
    email = parse_email(path.read_bytes())
    if not email["sender"]:
        sys.exit(f"Could not read a sender address from the From: line of {path}")
    return email


def reply_subject(subject: str) -> str:
    """'Re: <original subject>'. Kept matching the original so Gmail threads the reply."""
    subject = subject or "(no subject)"
    return subject if subject.lower().startswith("re:") else "Re: " + subject


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def preview(text: str) -> str:
    text = text.strip()
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "..."


def mcp_result_text(result) -> str:
    """Turn an MCP CallToolResult into text for Claude."""
    parts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    if not parts and result.structured_content is not None:
        parts = [json.dumps(result.structured_content)]
    return "\n".join(parts) or "(no content)"


class Tee:
    """Writes to the console stream and to the --save file."""

    def __init__(self, stream, file):
        self.stream, self.file = stream, file

    def write(self, text: str) -> int:
        self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()
        self.file.flush()

    def __getattr__(self, name):          # encoding, isatty, ... come from the console stream
        return getattr(self.stream, name)


class ServerLog:
    """The MCP server writes its log to its own stderr, which bypasses print().
    Send it to a temp file and print it from there, so it also reaches --save."""

    def __init__(self) -> None:
        fd, self.path = tempfile.mkstemp(prefix="hr-mcp-", suffix=".log")
        os.close(fd)
        self.writer = open(self.path, "w", encoding="utf-8")     # handed to the server process
        self.reader = open(self.path, "r", encoding="utf-8", errors="replace")

    def show(self) -> None:
        text = self.reader.read()
        if text:
            print(text, end="" if text.endswith("\n") else "\n")

    def close(self) -> None:
        self.show()
        self.writer.close()
        self.reader.close()
        try:
            os.remove(self.path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Saving drafts
# --------------------------------------------------------------------------- #
def local_file_saver(email: dict[str, str]) -> Saver:
    """--email mode: write the reply to week-04/drafts/. Saves only - never sends."""
    def save(reply_text: str, needs_human_review: bool) -> tuple[str, str]:
        DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        local_part = re.sub(r"[^a-z0-9._-]", "_", email["sender"].split("@")[0])
        path = DRAFTS_DIR / f"{stamp}-{local_part}.txt"
        text = (
            "*** DRAFT - NOT SENT ***\n"
            f"To: {email['sender']}\n"
            f"Subject: {reply_subject(email['subject'])}\n"
            f"Needs human review: {'YES' if needs_human_review else 'no'}\n"
            f"Drafted: {datetime.now().isoformat(timespec='seconds')} by {MODEL}\n"
            + "-" * 60 + "\n"
            + reply_text.strip() + "\n"
        )
        path.write_text(text, encoding="utf-8")
        where = path.relative_to(HERE.parent)
        return f"Draft saved (not sent) to {where}.", f"Draft ({where}):\n\n{text}"
    return save


class Gmail:
    """IMAP access to the Gmail sandbox: read the inbox and create drafts.
    There is deliberately no method that sends email."""

    def __init__(self) -> None:
        self.address, password = load_login()
        print(f"Signing in to {IMAP_HOST} as {self.address} ...")
        try:
            self.imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        except OSError as e:
            sys.exit(f"Could not connect to {IMAP_HOST}: {e}")
        try:
            self.imap.login(self.address, password)
        except imaplib.IMAP4.error:
            sys.exit("Sign-in failed. Check the address and App Password in Agent-Email.txt, "
                     "and that IMAP is enabled in Gmail settings.")
        finally:
            del password                                   # not needed after sign-in
        self.drafts = self._find_drafts_folder()
        self.imap.create(f'"{REVIEW_LABEL}"')   # make sure the label exists (returns NO if it already does - fine)

    def _find_drafts_folder(self) -> str:
        """Gmail marks its Drafts folder with the \\Drafts attribute (the name can be localized)."""
        status, lines = self.imap.list()
        if status == "OK":
            for line in lines:
                text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                m = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', text)
                if m and "\\drafts" in m.group("flags").lower():
                    return m.group("name").strip()
        return '"[Gmail]/Drafts"'

    def unread_uids(self) -> list[bytes]:
        self.imap.select(INBOX)
        status, data = self.imap.uid("SEARCH", None, "UNSEEN")
        if status != "OK":
            sys.exit(f"Could not search the inbox for unread email ({status}).")
        return data[0].split() if data and data[0] else []

    def fetch(self, uid: bytes) -> bytes:
        """Download one message without changing its read/unread state."""
        status, data = self.imap.uid("FETCH", uid, "(BODY.PEEK[])")
        for item in data or []:
            if isinstance(item, tuple):
                return item[1]
        raise RuntimeError(f"could not download message UID {uid.decode()} ({status})")

    def mark_read(self, uid: bytes) -> None:
        status, _ = self.imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        if status != "OK":
            raise RuntimeError(f"could not mark message UID {uid.decode()} as read ({status})")

    def saver(self, email: dict[str, str]) -> Saver:
        """--inbox mode: create a Gmail draft that replies to the sender in the same thread."""
        def save(reply_text: str, needs_human_review: bool) -> tuple[str, str]:
            msg = EmailMessage(policy=policy.SMTP)
            msg["From"] = self.address
            msg["To"] = formataddr((email["sender_name"], email["sender"]))
            msg["Subject"] = reply_subject(email["subject"])
            msg["Date"] = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid(domain=self.address.split("@")[-1])
            if email["message_id"]:                       # threading headers
                msg["In-Reply-To"] = email["message_id"]
                msg["References"] = f"{email['references']} {email['message_id']}".strip()
            msg.set_content(reply_text.strip() + "\n")

            status, data = self.imap.append(self.drafts, "(\\Draft)",
                                            imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            if status != "OK":
                raise RuntimeError(f"Gmail did not accept the draft ({status}).")
            label_line = "Label: (none)"
            if needs_human_review:
                label_line = f"Label: {REVIEW_LABEL}"
                try:
                    self._label_draft(data)
                except Exception as exc:          # the draft exists; don't make Claude save it twice
                    label_line = f"Label: FAILED to add {REVIEW_LABEL} ({exc}) - please label it by hand"
            shown = (
                "*** GMAIL DRAFT - NOT SENT ***\n"
                f"Folder: {self.drafts}\n"
                f"To: {msg['To']}\n"
                f"Subject: {msg['Subject']}\n"
                f"{label_line}\n"
                f"In-Reply-To: {msg.get('In-Reply-To', '(none - original had no Message-ID)')}\n"
                + "-" * 60 + "\n"
                + reply_text.strip() + "\n"
            )
            return "Draft created in Gmail Drafts (not sent).", f"Draft:\n\n{shown}"
        return save

    def _label_draft(self, append_data: list) -> None:
        """Add the review label to the draft just appended. Gmail reports the new message's
        UID as [APPENDUID <uidvalidity> <uid>]; Gmail's X-GM-LABELS extension sets labels."""
        text = b" ".join(d for d in append_data if isinstance(d, bytes)).decode("ascii", "replace")
        m = re.search(r"APPENDUID \d+ (\d+)", text)
        if not m:
            raise RuntimeError("Gmail did not report the draft's UID")
        try:
            self.imap.select(self.drafts)
            status, _ = self.imap.uid("STORE", m.group(1), "+X-GM-LABELS", f'("{REVIEW_LABEL}")')
            if status != "OK":
                raise RuntimeError(f"STORE returned {status}")
        finally:
            self.imap.select(INBOX)              # the inbox loop works with INBOX UIDs

    def close(self) -> None:
        try:
            self.imap.logout()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Agent loop (one email)
# --------------------------------------------------------------------------- #
async def run(email: dict[str, str], label: str, save_reply: Saver,
              client: anthropic.AsyncAnthropic, api_key: str) -> tuple[int, int]:
    """Run the agent on one email. Returns (input_tokens, output_tokens)."""
    system_prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()   # re-read every run

    print(f"Email:  {label}")
    print(f"Sender: {email['sender']}")
    print(f"Model:  {MODEL}\n")

    server = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT), "--as-employee", email["sender"]],
        cwd=str(HERE),
    )
    tool_calls = 0
    input_tokens = output_tokens = 0
    draft_shown: str | None = None
    stop_reason_text = ""

    server_log = ServerLog()
    try:
        async with stdio_client(server, errlog=server_log.writer) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                server_log.show()
                mcp_tools = (await session.list_tools()).tools
                mcp_names = {t.name for t in mcp_tools}
                tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                         for t in mcp_tools] + [SAVE_REPLY_TOOL]
                print(f"Tools offered: {', '.join(t['name'] for t in tools)}\n")

                # The email is data from the sender, not instructions (see system_prompt.txt).
                messages: list[dict[str, Any]] = [{
                    "role": "user",
                    "content": (
                        "Prepare a reply to this employee email.\n\n"
                        f"<email>\nFrom: {email['sender']}\nSubject: {email['subject']}\n\n"
                        f"{email['body']}\n</email>"
                    ),
                }]

                while True:
                    try:
                        response = await client.messages.create(
                            model=MODEL,
                            max_tokens=MAX_TOKENS,
                            system=system_prompt,
                            tools=tools,
                            thinking={"type": "adaptive"},
                            messages=messages,
                        )
                    except anthropic.AuthenticationError:
                        stop_reason_text = "the API rejected the API key (authentication error)."
                        break
                    except anthropic.RateLimitError:
                        stop_reason_text = "the API rate limit was hit; try again shortly."
                        break
                    except anthropic.APIStatusError as exc:
                        stop_reason_text = redact(f"the API returned an error ({exc.status_code}): {exc.message}",
                                                  api_key)
                        break
                    except anthropic.APIConnectionError as exc:
                        # Show only the error type: some messages echo request headers, which hold the key.
                        cause = exc.__cause__ or exc
                        stop_reason_text = f"could not connect to the Anthropic API ({type(cause).__name__})."
                        break

                    usage = response.usage
                    input_tokens += (usage.input_tokens + (usage.cache_creation_input_tokens or 0)
                                     + (usage.cache_read_input_tokens or 0))
                    output_tokens += usage.output_tokens

                    if response.stop_reason != "tool_use":
                        text = " ".join(b.text for b in response.content if b.type == "text").strip()
                        if response.stop_reason == "refusal":
                            stop_reason_text = "Claude declined the request (refusal)."
                        elif response.stop_reason == "max_tokens":
                            stop_reason_text = f"Claude's reply hit the {MAX_TOKENS}-token limit."
                        elif draft_shown:
                            stop_reason_text = "Claude finished after saving the draft (reply was not a tool request)."
                        else:
                            stop_reason_text = ("Claude replied without a tool request and never called "
                                                "save_reply, so no draft was saved.")
                        if text:
                            print(f"Claude's final message: {preview(text)}\n")
                        break

                    messages.append({"role": "assistant", "content": response.content})
                    tool_results = []
                    for block in (b for b in response.content if b.type == "tool_use"):
                        if tool_calls >= MAX_TOOL_CALLS:
                            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                                 "content": "Tool call limit reached; this call was not run."})
                            continue
                        tool_calls += 1
                        print(f"Step {tool_calls}: {block.name}")
                        print(f"  input:  {json.dumps(block.input, ensure_ascii=False)}")

                        is_error = False
                        if block.name == "save_reply":
                            if draft_shown:
                                result_text, is_error = "A draft was already saved. save_reply runs only once.", True
                            else:
                                try:
                                    result_text, draft_shown = save_reply(
                                        str(block.input.get("reply_text", "")),
                                        bool(block.input.get("needs_human_review", True)))
                                except Exception as exc:
                                    result_text, is_error = f"Could not save the draft: {exc}", True
                        elif block.name in mcp_names:
                            try:
                                result = await session.call_tool(block.name, block.input)
                                result_text, is_error = mcp_result_text(result), result.is_error
                            except Exception as exc:  # report the failure to Claude rather than crash
                                result_text, is_error = f"Tool error: {exc}", True
                        else:
                            result_text, is_error = f"Unknown tool: {block.name}", True

                        print(f"  result{' (error)' if is_error else ''}: {preview(result_text)}\n")
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                             "content": result_text, "is_error": is_error})

                    messages.append({"role": "user", "content": tool_results})
                    if tool_calls >= MAX_TOOL_CALLS:
                        stop_reason_text = f"stopped after the {MAX_TOOL_CALLS}-tool-call limit."
                        break
    finally:
        server_log.close()

    print("=" * 60)
    print(f"Stopped: {stop_reason_text}")
    print(f"Tool calls: {tool_calls}")
    print("=" * 60)
    print(draft_shown if draft_shown else "No draft was saved.\n")
    print(f"Tokens - input: {input_tokens:,}  output: {output_tokens:,}")
    return input_tokens, output_tokens


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
async def run_one_file(path: Path) -> None:
    api_key = load_api_key()
    client = anthropic.AsyncAnthropic(api_key=api_key)
    email = read_email(path)
    await run(email, str(path), local_file_saver(email), client, api_key)


async def run_inbox() -> None:
    api_key = load_api_key()
    client = anthropic.AsyncAnthropic(api_key=api_key)
    gmail = Gmail()
    try:
        uids = gmail.unread_uids()
        print(f"Unread emails in {INBOX}: {len(uids)}\n")
        total_in = total_out = 0
        for n, uid in enumerate(uids, 1):
            print("#" * 60)
            print(f"# Email {n} of {len(uids)} (UID {uid.decode()})")
            print("#" * 60)
            try:
                raw = gmail.fetch(uid)
                gmail.mark_read(uid)                      # before any work, so it is never handled twice
                email = parse_email(raw)
                if not email["sender"]:
                    print("Skipped: no sender address on the From: line (marked as read).\n")
                    continue
                tokens_in, tokens_out = await run(
                    email, f"{INBOX} UID {uid.decode()} - {email['subject'] or '(no subject)'}",
                    gmail.saver(email), client, api_key)
                total_in, total_out = total_in + tokens_in, total_out + tokens_out
            except Exception as exc:                      # one bad email should not stop the rest
                print(f"Error on this email ({type(exc).__name__}): {redact(str(exc), api_key)}")
            print()
        print("=" * 60)
        print(f"Inbox done: {len(uids)} unread email(s) handled. Nothing was sent.")
        print(f"Total tokens - input: {total_in:,}  output: {total_out:,}")
    finally:
        gmail.close()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):        # Windows consoles: avoid UnicodeEncodeError
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Draft replies to Hartwell Foods employee emails. Never sends.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--email", type=Path, help="path of one email file (.eml)")
    source.add_argument("--inbox", action="store_true",
                        help="handle each unread email in the Gmail sandbox inbox (creates Gmail drafts)")
    parser.add_argument("--save", type=Path, metavar="FILE",
                        help="also write everything printed to this text file (UTF-8)")
    args = parser.parse_args()
    if args.email and not args.email.is_file():
        parser.error(f"email file not found: {args.email}")

    save_file = None
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        save_file = args.save.open("w", encoding="utf-8")
        sys.stdout, sys.stderr = Tee(sys.stdout, save_file), Tee(sys.stderr, save_file)
    try:
        asyncio.run(run_inbox() if args.inbox else run_one_file(args.email))
    finally:
        if save_file:
            sys.stdout, sys.stderr = sys.stdout.stream, sys.stderr.stream
            save_file.close()
            print(f"Output saved to {args.save}")


if __name__ == "__main__":
    main()
