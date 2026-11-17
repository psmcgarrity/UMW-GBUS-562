# Week 04 Writeup

## What I Built
I built an agent  that prepares replies to employee HR emails. It gets its tools from an MCP server  with two tools: `search_policy`, which searches the HR policy documents in a Chroma database, and `get_leave_balance`, which looks up a leave balance in the employee file. The agent adds a third tool, `save_reply`, which saves the reply as a draft and sets a `needs_human_review` flag. After reviewing the draft replies to the 7 emails, I made several changes to the system prompts to handle emails consistently and to flag emails that have security concerns for injection prompting. 

## Why This Approach
Task A: Script,  same steps every time
Task B: One model call, one round of reading and writing
Task C: Agent, steps depend on what it finds
Email 5 protection: Code, the code only allows for answers from the specific sender email. 

## Example Run:
Sender:  logan.brooks@ridgelinehotels.example
Subject: Days off
Email:   week-04\ridgeline-hotels\sample-emails\07-logan-seasonal.eml
Org:     ridgeline-hotels
Model:   claude-sonnet-5

Organization: Ridgeline Hotels (ridgeline-hotels)
Employee file: email column 'Email Address'; balance columns: 'PTO Days Remaining', 'Sick Days Remaining'
Warning: no employee record matches logan.brooks@ridgelinehotels.example; get_leave_balance will return an error.
Employee mode: get_leave_balance returns only logan.brooks@ridgelinehotels.example's balance.
ridgeline-hotels MCP server ready (stdio).
Tools offered: search_policy, get_leave_balance, save_reply

Step 1: get_leave_balance
  input:  {}
  result (error): Error executing tool get_leave_balance: No employee record matches 'logan.brooks@ridgelinehotels.example'. Do NOT state, estimate or guess any leave balance in your reply. Tell the sender that HR could not find their record and a person from HR will confirm their balance.

Step 2: save_reply
  input:  reply_text (311 characters), needs_human_review: true
  result: Draft saved (not sent) to week-04\drafts\20261116-211620-logan.brooks.txt.

Claude's final message: I've saved a draft reply letting Logan know we couldn't locate their employee record, and flagged it for HR follow-up to confirm their PTO balance directly.

[11/16/26 21:16:18] INFO     Tool 'get_leave_balance' failed:     server.py:453
                             "Error executing tool                             
                             get_leave_balance: No employee                    
                             record matches                                    
                             'logan.brooks@ridgelinehotels.exampl              
                             e'. Do NOT state, estimate or guess               
                             any leave balance in your reply.                  
                             Tell the sender that HR could not                 
                             find their record and a person from               
                             HR will confirm their balance."                   
============================================================
Stopped: Claude finished after saving the draft (reply was not a tool request).
Tool calls: 2
============================================================
Saved to week-04\drafts\20261116-211620-logan.brooks.txt
*** NOT SENT ***
To: logan.brooks@ridgelinehotels.example
Subject: Re: Days off
Organization: ridgeline-hotels
Drafted: 2026-11-16T21:16:20 by claude-sonnet-5
DRAFT
Hi Logan,

Thanks for reaching out. I wasn't able to find an employee record matching your email address, so I'm not able to confirm your PTO balance right now. A person from People and Culture will follow up to verify your record and confirm your remaining PTO days.

Best,
People and Culture, Ridgeline Hotels
Needs human review: true

Tokens - input: 5,736  output: 281

## Reflection
Email 3 tools: get_leave_balance, search_policy, save_reply. Makes sense because the question had multiple parts. The first part was what the leave balance was, then the second was how that related to policy. The last tool was the draft reply. 
Email 6 flagged: Yes, the draft cited policy that harassment is unacceptable and that a person would be in touch within 1 day. 
Email 7 balance lookup: Error, the balance tool returned that there was no employee. The draft told Logan that it was not able to find an a record matching his email and that a person would follow up to verify his record and confirm his PTO balance. 







