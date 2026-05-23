# Lumi prompt source — authoritative originals

> Permanent source file for prompts Lumi has authored. Stellan reads from here
> when pasting into runtime modules. Never edit Lumi's text; only append.

## AFFECT.unresolved (field block inside AFFECT_BLOCK_CONTRACT)

> Authored by Lumi, 2026-05-2x (originally lost — recovered from chat
> 2026-05-23 21:13). Paste into AFFECT_BLOCK_CONTRACT in rollup.py /
> sessionend_async.py AFFECT segment prompt as a field section.

```
Unresolved:
  - Record only unresolved emotional episodes.
  - If nothing fits, skip this field and output N/A.
  - Include: if the emotion is still intense at the end of the session, with no resolution or winding down. Can be personal or relationship-related. （e.g.  吵架本session没合好，后天要演讲很紧张，分享喜讯没说完出门了。）
  - Exclude: Resolved emotions, unresolved tasks, emotions related to study/project.（e.g. 已合好，情绪稳定，已聊完，essay还有两段）
```

## reconcile_prev

> Drafted by Stellan, 2026-05-23. Mirrors Unresolved structure (Include / Exclude / N/A + CN examples). Paste verbatim into AFFECT segment prompt.

```
reconcile_prev:
  - Record when this session resolves or winds down a previously-unresolved emotional episode (the one referenced by reconcile_ref).
  - Output a short Chinese phrase, not a sentence.
  - If nothing fits, output N/A.
  - Include: personal / relationship affect resolutions — the previous unresolved emotion has eased, closed, or vented. （e.g. 和好了, 演讲讲完松口气, 喜讯说完了, 情绪平复, 焦虑消了）
  - Exclude: task / study / code resolutions; episodes still open (→ Unresolved). （e.g. essay 写完, bug 修好, phase 收尾, 仍然在吵架, 项目还没收）
```

