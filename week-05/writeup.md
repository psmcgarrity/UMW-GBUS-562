## \## What I Built We constructed two coordinators, one pipeline and one orchestrator. To make it work for any company including Copperline and not just Brookfield, a few changes had to be made, including an instruction (--org) that changed the company name, which helped the drafted emails become more orientated from the company. Also the default csv format that is used by Brookefield had to be changed so that the new companies data still fit. 

## 

## 

## \## Why This Approach

## Task A: Script, this is a repeatable task with no decisions. 

## Task B: Team. In normal business practice each of these steps would likely fall to a different person within the organization. 

## For Tasks C and D, decide which coordinator fits: start with Pipeline or Orchestrator, then say why.

## Task C: Pipeline. No steps involve any loops or tools needed.

## Task D: Orchestrator. There is a need to handle each type of email differently, which could involve scripts, agents, or teams. 

## 

## \## Example Run

## Coordinator: orchestrator

## Email:       02-marlow-boiler-options.eml

## From:        Victor Lindqvist <v.lindqvist@marlowmetal.example>

## Subject:     PO-5520 - BL-12 boilers - please choose a shipping option

## 

## \[orchestrator] claude-opus-5-5

## &#x20; orchestrator step 1: run\_supply\_analyst {}

## &#x20;   \[supply analyst] claude-opus-5-5

## &#x20;     \[server] Copperline Coffee supply MCP server ready (stdio), data: copperline-coffee

## &#x20;     \[server] columns found under other names - orders.csv: ship\_day=ship\_by\_day, quantity=units

## &#x20;     analyst step 1: get\_part {"part\_id": "BL-12"}

## &#x20;       -> { "part\_id": "BL-12", "description": "1.2-liter boiler", "on\_hand": 60, "supplier": "Marlow Metalworks", "scheduled\_shipments": \[ { "po\_number": "PO-5520", "supplier": "Marlow Metalworks", "qty": 200, "arrives\_in\_days": 7 } ] }

## &#x20;     analyst step 2: get\_orders\_for\_part {"part\_id": "BL-12"}

## &#x20;       -> { "part\_id": "BL-12", "orders": \[ { "order\_id": "O-2201", "customer": "Morning Ritual Cafe", "product": "Cafe One", "units": 20, "parts\_per\_unit": 1, "parts\_needed": 20, "ship\_day": 6, "late\_penalty": false }, { "order\_id": "O-2202", "customer": "Harbor Hotel Group", "product": "Cafe Duo", "units": ...

## &#x20;     analyst step 3: check\_coverage {"part\_id": "BL-12", "po\_number": "PO-5520", "revised\_shipments": \[{"qty": 80, "arrives\_in\_days": 7}, {"qty": 120, "arrives\_in\_days": 21}]}

## &#x20;       -> { "part\_id": "BL-12", "po\_number": "PO-5520", "original\_shipment": { "qty": 200, "arrives\_in\_days": 7 }, "revised\_shipments": \[ { "qty": 80, "arrives\_in\_days": 7 }, { "qty": 120, "arrives\_in\_days": 21 } ], "on\_hand": 60, "rules": "Parts go to orders in ship-day order; an order ships the units it has...

## &#x20;     analyst step 4: check\_coverage {"part\_id": "BL-12", "po\_number": "PO-5520", "revised\_shipments": \[{"qty": 200, "arrives\_in\_days": 13}]}

## &#x20;       -> { "part\_id": "BL-12", "po\_number": "PO-5520", "original\_shipment": { "qty": 200, "arrives\_in\_days": 7 }, "revised\_shipments": \[ { "qty": 200, "arrives\_in\_days": 13 } ], "on\_hand": 60, "rules": "Parts go to orders in ship-day order; an order ships the units it has parts for on its ship day and the re...

## &#x20;     analyst step 5: check\_coverage {"part\_id": "BL-12", "po\_number": "PO-5520", "revised\_shipments": \[{"qty": 200, "arrives\_in\_days": 7}]}

## &#x20;       -> { "part\_id": "BL-12", "po\_number": "PO-5520", "original\_shipment": { "qty": 200, "arrives\_in\_days": 7 }, "revised\_shipments": \[ { "qty": 200, "arrives\_in\_days": 7 } ], "on\_hand": 60, "rules": "Parts go to orders in ship-day order; an order ships the units it has parts for on its ship day and the res...

## &#x20;     ANALYST'S BRIEF

## &#x20;     {

## &#x20;       "part\_id": "BL-12",

## &#x20;       "what\_changed": "PO-5520's 200 BL-12 boilers, due on day 7, will not all arrive on time: Option A sends 80 on day 7 and 120 on day 21, and Option B sends all 200 on day 13 (Marlow's default if Copperline doesn't reply within three business days).",

## &#x20;       "orders\_at\_risk": \[

## &#x20;         {

## &#x20;           "order\_id": "O-2203",

## &#x20;           "customer": "Elm State University Dining",

## &#x20;           "units\_short": 40,

## &#x20;           "ship\_day": 12,

## &#x20;           "ready\_day": 13,

## &#x20;           "late\_penalty": true

## &#x20;         },

## &#x20;         {

## &#x20;           "order\_id": "O-2205",

## &#x20;           "customer": "Tall Pines Coffee House",

## &#x20;           "units\_short": 5,

## &#x20;           "ship\_day": 17,

## &#x20;           "ready\_day": 21,

## &#x20;           "late\_penalty": true

## &#x20;         }

## &#x20;       ],

## &#x20;       "options": \[

## &#x20;         {

## &#x20;           "name": "Option A: 80 on day 7, remaining 120 on day 21",

## &#x20;           "order\_ids\_at\_risk": \[

## &#x20;             "O-2205"

## &#x20;           ]

## &#x20;         },

## &#x20;         {

## &#x20;           "name": "Option B: all 200 on day 13",

## &#x20;           "order\_ids\_at\_risk": \[

## &#x20;             "O-2203"

## &#x20;           ]

## &#x20;         }

## &#x20;       ],

## &#x20;       "decisions\_for\_a\_person": \[

## &#x20;         "Choose Option A (O-2205 for Tall Pines Coffee House ships 5 of 25 units 4 days late, on day 21; late penalty applies) or Option B (O-2203 for Elm State University Dining ships 40 of 50 units 1 day late, on day 13; late penalty applies).",

## &#x20;         "Reply to Marlow Metalworks within three business days, or Marlow will go with Option B by default."

## &#x20;       ],

## &#x20;       "supplier\_questions": \[

## &#x20;         "Which shipping option (A or B) does Copperline want for PO-5520? Marlow asks for an answer within three business days."

## &#x20;       ]

## &#x20;     }

## &#x20;   -> { "brief": { "part\_id": "BL-12", "what\_changed": "PO-5520's 200 BL-12 boilers, due on day 7, will not all arrive on time: Option A sends 80 on day 7 and 120 on day 21, and Option B sends all 200 on day 13 (Marlow's default if Copperline doesn't reply within three business days).", "orders\_at\_risk": ...

## &#x20; orchestrator step 2: finish {"summary": "Marlow Metalworks can't ship all 200 BL-12 boilers on PO-5520 on the scheduled date (day 7) because their welding cell is at half capacity. Each of their two options puts one customer order at risk, and both orders carry late penalties.\\n\\n- \*\*Option A:\*\* 80 boilers arrive on day 7 and the other 120 on day 21. Tall Pines Coffee House's order O-2205 would ship 5 of its 25 units 4 days late, on day 21 instead of day 17.\\n- \*\*Option B:\*\* all 200 boilers arrive on day 13. Elm State University Dining's order O-2203 would ship 40 of its 50 units 1 day late, on day 13 instead of day 12.\\n\\n\*\*Decision needed:\*\* choose Option A or Option B. Marlow wants an answer within three business days. If they don't hear from us, they'll go with Option B, which is the one that delays the Elm State order.\\n\\nI haven't drafted a reply to Marlow, because the reply depends on which option you choose. Customer notices are on hold until you choose. Once you decide, I can draft the reply to Marlow and the notice for whichever customer is affected. Nothing has been sent.", "needs\_decision": true}

## &#x20;   -> Finished.

## 

## ============================================================

## Company:     Copperline Coffee (copperline-coffee)

## Email:       02-marlow-boiler-options.eml - PO-5520 - BL-12 boilers - please choose a shipping option

## Coordinator: orchestrator

## Decision needed by a person: YES

## 

## Marlow Metalworks can't ship all 200 BL-12 boilers on PO-5520 on the scheduled date (day 7) because their welding cell is at half capacity. Each of their two options puts one customer order at risk, and both orders carry late penalties.

## 

## \- \*\*Option A:\*\* 80 boilers arrive on day 7 and the other 120 on day 21. Tall Pines Coffee House's order O-2205 would ship 5 of its 25 units 4 days late, on day 21 instead of day 17.

## \- \*\*Option B:\*\* all 200 boilers arrive on day 13. Elm State University Dining's order O-2203 would ship 40 of its 50 units 1 day late, on day 13 instead of day 12.

## 

## \*\*Decision needed:\*\* choose Option A or Option B. Marlow wants an answer within three business days. If they don't hear from us, they'll go with Option B, which is the one that delays the Elm State order.

## 

## I haven't drafted a reply to Marlow, because the reply depends on which option you choose. Customer notices are on hold until you choose. Once you decide, I can draft the reply to Marlow and the notice for whichever customer is affected. Nothing has been sent.

## 

## Options offered (customer notices held until one is chosen):

## &#x20; - Option A: 80 on day 7, remaining 120 on day 21: delays O-2205

## &#x20; - Option B: all 200 on day 13: delays O-2203

## 

## Drafts: none.

## ============================================================

## Saved in week-05\\drafts\\copperline-coffee\\20261122-171651-orchestrator-02-marlow-boiler-options

## Tokens - orchestrator input:    3,708  output:     706

## Tokens - analyst      input:    9,427  output:   1,786

## Tokens - total        input:   13,135  output:   2,492



## 

## \## Reflection

## Three labeled lines about what your saved runs show.

## Email 1 reading: Matches. They are the same, it correctly interpreted it as 15 days after the scheduled date, so they are both day 19. The brief gave day 19 for the 160. 

## Email 1 customer names: No. It answered Diane's request by asking for clarification, then asking if any of three identified scenarios might be possible. It was not marked for a person. 

## Email 4 orchestrator: It only called run\_supply\_analyst, as its orders were not impacted and no email was necessary. The pipeline ran through all of its steps and drafted an email as it always does.

