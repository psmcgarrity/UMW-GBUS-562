"""load_inbox.py - put the sample HR emails into a Gmail sandbox inbox.

It signs in to Gmail with IMAP and places each .eml file from
week-04/hartwell-hr/sample-emails into the INBOX as an UNREAD message dated
today. It uses IMAP APPEND, so nothing is sent: the messages are written
directly into the mailbox.

Usage (from the top of the repository):
  .venv\\Scripts\\python.exe week-04\\load_inbox.py

The login is read at run time from Agent-Email.txt in the folder above the
repository. The password is never printed or written to a file.

Agent-Email.txt format:
  EMAIL_ADDRESS=your.sandbox.address@gmail.com
  APP_PASSWORD=abcdefghijklmnop
(Other labels such as "email:" / "password:", or just the address on one line
and the password on the next, also work.) Gmail requires an App Password for IMAP, not your normal password.
"""

from __future__ import annotations

import imaplib
import re
import sys
import time
from email import policy
from email.parser import BytesParser
from email.utils import formatdate, make_msgid
from pathlib import Path

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve().parent                    # .../UMW-GBUS-562/week-04
EMAILS_DIR = HERE / "hartwell-hr" / "sample-emails"
LOGIN_FILE = HERE.parents[1] / "Agent-Email.txt"          # GBUS-562-course/Agent-Email.txt (outside the repo)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
MAILBOX = "INBOX"

EMAIL_LABELS = ("email", "e-mail", "user", "username", "login", "address", "account")
PASSWORD_LABELS = ("password", "app password", "app-password", "pass", "pwd")


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
def load_login() -> tuple[str, str]:
    """Return (email, password) from Agent-Email.txt. Errors never include the password."""
    try:
        text = LOGIN_FILE.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        sys.exit(f"Login file not found: {LOGIN_FILE}")

    email = password = None
    unlabeled: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z][A-Za-z \-_]*?)\s*[:=]\s*(.+)$", line)
        # Normalize labels like "EMAIL_ADDRESS" / "App-Password" to "email address" / "app password".
        label = re.sub(r"[_\-\s]+", " ", m.group(1)).strip().lower() if m else ""
        words = set(label.split())
        if m and any(p in label for p in PASSWORD_LABELS):
            password = m.group(2).strip()
        elif m and words & set(EMAIL_LABELS):
            email = m.group(2).strip()
        else:
            unlabeled.append(line)

    # Unlabeled form: the line with an "@" is the address, the next line is the password.
    if email is None:
        at_lines = [i for i, l in enumerate(unlabeled) if "@" in l and " " not in l]
        if at_lines:
            i = at_lines[0]
            email = unlabeled.pop(i)
            if password is None and i < len(unlabeled):
                password = unlabeled[i]
    if password is None and len(unlabeled) == 1:
        password = unlabeled[0]

    if not email or "@" not in email:
        sys.exit(f"Could not find the email address in {LOGIN_FILE.name}.")
    if not password:
        sys.exit(f"Could not find the password in {LOGIN_FILE.name}.")

    # Gmail shows App Passwords as four groups of four letters; IMAP wants them without spaces.
    if re.fullmatch(r"[A-Za-z]{4}( [A-Za-z]{4}){3}", password):
        password = password.replace(" ", "")
    return email, password


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
def prepare_message(path: Path, to_address: str) -> bytes:
    """Return the .eml file as bytes, with today's Date, a fresh Message-ID and the
    sandbox address added as Delivered-To (the original From/To are kept)."""
    with path.open("rb") as f:
        msg = BytesParser(policy=policy.SMTP).parse(f)
    for header in ("Date", "Message-ID", "Delivered-To"):
        del msg[header]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="hartwellfoods.example")
    msg["Delivered-To"] = to_address
    return msg.as_bytes()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    files = sorted(EMAILS_DIR.glob("*.eml"))
    if not files:
        sys.exit(f"No .eml files found in {EMAILS_DIR}")

    email, password = load_login()
    print(f"Signing in to {IMAP_HOST} as {email} ...")

    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    except OSError as e:
        sys.exit(f"Could not connect to {IMAP_HOST}: {e}")

    try:
        try:
            imap.login(email, password)
        except imaplib.IMAP4.error:
            sys.exit("Sign-in failed. Check the address and App Password in Agent-Email.txt, "
                     "and that IMAP is enabled in Gmail settings.")
        finally:
            password = None  # not needed after sign-in

        loaded = 0
        for path in files:
            data = prepare_message(path, email)
            # Empty flag list "()" -> no \Seen flag, so the message is unread.
            # The internal date (what Gmail sorts by) is set to now.
            status, _ = imap.append(MAILBOX, "()", imaplib.Time2Internaldate(time.time()), data)
            if status == "OK":
                loaded += 1
                print(f"  placed in {MAILBOX} (unread): {path.name}")
            else:
                print(f"  FAILED: {path.name} ({status})")

        print(f"\nDone: {loaded} of {len(files)} emails placed in {MAILBOX}. Nothing was sent.")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
