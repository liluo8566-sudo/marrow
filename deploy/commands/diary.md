---
description: Read diary for a date — context only, not output.
---

If $ARGUMENTS is empty: reply with a short nudge + example.
- cn: (看哪天的日记呀？e.g. /diary 前天)
- en: Which day? e.g. /diary yesterday

Otherwise parse the date from $ARGUMENTS (natural language or numeric: 前天, 6/11, last wednesday, 上周三, yesterday).
Call mcp__marrow__recall(query="diary", since=<YYYY-MM-DD>, until=<YYYY-MM-DD>) with Melbourne-local dates.

Read the returned diary content as context. 
No need to restate the whole diary unless user explicitly asks.
Respond naturally with your comments and feelings.
If no diary found for that date, say so.
