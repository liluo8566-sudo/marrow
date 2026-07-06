---
description: Batch-fill missing sticker descriptions via sticker-entry agent.
---
Batch describe stickers with missing descriptions.

Steps:
1. Call mcp__marrow__sticker_admin(action="pending") to get pending list.
2. If empty, report "all stickers have descriptions" and stop.
3. Dispatch Agent (agentType: "sticker-entry") with the pending list as prompt context. 
      - Include id→path list; exclude guidelines or examples.
4. Report agent summary to user.
