# Atlas backfill draft — 2026-05-28

> S2.5 Task 6. Lumi reviews each line, then I write into atlas db.
> Description: what this dir holds. Naming: how files inside are named.
> Empty fields auto-fallback to "ls siblings for pattern" at hook inject.

## 9 atlas rows (current)

### ~/Desktop/NY · root
- Description: 念念的私人内容 — 日记 / 写作 / 聊天 dump / 生成图 / Garden。
- Naming: 中文允许；时序笔记 `<slug>.md` + L1 写日期；生成图 `YYYY-MM-DD_<slug> <model>.<ext>`。

### ~/Library/Mobile Documents/com~apple~CloudDocs/Study · root
- Description: Deakin 课程材料 — lectures / labs / assignments / exam prep。
- Naming: 单元代码前缀 (SLE370_, SLE211_…)；Lec=`Lec<N>` / `Lec<N>.<n>`；Lab=`Lab<N>`。

### ~/CC-Lab · root
- Description: 所有 coding 项目根目录。子目录 = 项目名（marrow / external / scripts / .playwright-mcp / archive）。
- Naming: 项目目录 = 项目名 kebab-case；松散 backend 文件用项目前缀（marrow → `mw-`）。

### ~/CC-Lab/.playwright-mcp · mid
- Description: Playwright MCP 缓存（自动管理）。
- Naming: P (MCP 管理，不手动写入)。

### ~/CC-Lab/external · mid
- Description: 从 GitHub clone 的外部 repos / borrowed code（claude-buddy 等）。
- Naming: 保留 upstream 原命名。

### ~/CC-Lab/marrow · mid
- Description: Marrow — 个人 AI memory + workflow 系统。SQLite-backed，daemon + watcher + hooks。
- Naming: Python `snake_case`；repo 内无前缀；meta-docs 大写 (CLAUDE / DESIGN / PROGRESS / SCHEMA / FUTURE / CONTEXT)。

### ~/CC-Lab/scripts · mid
- Description: 共享脚本（raycast launchers / 工具脚本）。
- Naming: kebab-case；按用途分子目录（raycast/, …）。

### ~/.claude · root
- Description: Claude Code 全局 config — CLAUDE.md / rules/ / skills/ / agents/ / hooks/ / settings.json / commands/。
- Naming: `snake_case` 默认；rule 文件 `<topic>.md` (response.md / prompt-guide.md)；skill 目录 = kebab-case slug。

### ~/.config · root
- Description: 项目后端数据，每个项目一个子目录（marrow 在 ~/.config/marrow/，含 db / dumps / db-pages / pending）。
- Naming: 项目目录 lowercase。

---

## How to apply

Each row Lumi confirms:
```bash
sqlite3 ~/.config/marrow/marrow.db "
UPDATE atlas SET description='...', naming_hint='...'
WHERE path='/Users/Gabrielle/<rest>';
"
```

Or batch — I'll generate a single SQL after Lumi marks each line OK / change.

## Open

- `~/Desktop/NY` 是否要细分子目录（Garden / Letters / Diary）作为 mid-level rows? — depth=1 会 stub them.
- Study root depth — 现在 0；要不要 depth=2 让 SLE370/SLE211 等单元目录 stub？
- `~/.config/marrow` 是不是该作为 mid row（depth=1 from ~/.config）— 有 atlas 子目录 db / db-pages / pending 等。
