# Week 03 Writeup

## What I Built
I extended my week 3 RAG tool (`ask.py`) so it can answer questions from any folder of documents, not just the Copperline finance documents. I then pointed it at a new document set, Larkfield, created a separate Chroma database and tested it with seven runs. After the first run, I removed an old file version to archive and re-built the database so that it would only retrieve the current version. 

## Why This Approach
Chunking method:  structure
Makes sense on its own: Yes, chunks are logical by header and up to 350 words long. The chunks metadata has an ID, text and document and sections. 
Label and source: Both, I know because I checked a few chunks and claude explained how it chunked the documents
Short enough: Yes, claude output the data. There are 30 chunks with 58.6 words on average per chunk with a range of 33–119.  

## Example Run: q5
#1  similarity 0.540  source: funds-availability-policy.html
Document: Funds Availability Policy (Version 3)
Section: Check deposits

For checks deposited at a branch or a Larkfield ATM, the first $275 of the day's check deposits is available on the next business day. The rest of the day's check deposits is available on the second business day after the day of deposit.

#2  similarity 0.485  source: funds-availability-policy.html
Document: Funds Availability Policy (Version 3)
Section: Mobile check deposits

Checks deposited with the Larkfield mobile app follow the same availability as checks deposited at a branch. A member can deposit up to $5,000 per business day through the mobile app. Members should keep the paper check for 30 days after a mobile deposit, then destroy it.

#3  similarity 0.423  source: funds-availability-policy.html
Document: Funds Availability Policy (Version 3)
Section: Business days and cutoff times

A business day is Monday through Friday, except federal holidays. A deposit made before 5 p.m. on a business day is received that day. A deposit made after 5 p.m., or on a weekend or holiday, is received on the next business day.

#4  similarity 0.420  source: funds-availability-policy.html
Document: Funds Availability Policy (Version 3)
Section: Cash, direct deposits, and wire transfers

Cash deposited at a branch, direct deposits such as payroll, and incoming wire transfers are available on the business day they are received.

ANSWER
**Partly.** Since the paycheck was deposited as a check at a branch (not a direct deposit), it falls under the check deposit rule: only the first $275 of the day's check deposits is available on the next business day, with the remainder available on the second business day after deposit [funds-availability-policy.html]. So of the $2,000 check, the member can use **$275** on the next business day, and the remaining **$1,725** will be available the following business day [funds-availability-policy.html].

## Reflection

What was wrong: consumer-fee-schedule-v7, the retrieved chunks showed high similarity between the two versions of the document 
How I fixed it: Removed, took it out of the main folder and put it in a separate archive folder. I then rebuilt the database.
Prevention: Document Owner, having a person in charge of making sure only the current version of the file is in  the folder would prevent this from happening again. 
Question 6: Declined. The chunks covered fees and share certificate rates with low similarity. 




