2026-06-14

# Sticker Sourcing Guide

## LINE
- `sticker-convert` (pipx installed, no auth needed)
- `sticker-convert --download-line <store_url> --output-dir <dir> --no-confirm --no-compress`
- URL must include region suffix: `.../product/1369/en`
- Output: PNG ~320px, high quality. Tested: Chiikawa 40/40 success.
- Curated packs: Chiikawa, Spoiled Rabbit, Mofusand, Mochi Peach Cat, Puffy Bear Rabbit Love, Brown & Cony, Molang, Little Fat Shiba, Pusheen, Kanahei.

## Telegram
- `sticker-convert --download-telegram <pack_url> --telegram-token <token>`
- Token from @BotFather. Output: WEBP/PNG 512px.

## WeChat (export-wechat-emoji)
- App at `/Applications/(导出微信表情包).app` (liusheng22/export-wechat-emoji v0.1.4)
- DYLD inject extracts SQLCipher key, downloads from Tencent CDN.
- Requires: WeChat Mac logged in. Pause auto-move-downloads during export (`launchctl bootout/bootstrap com.nianyu.move-downloads`).
- Output: GIF/PNG to `~/Downloads/(微信表情包_导出_xxx)/`. Tested 06-14: 300/321 success.
- Keep app installed — safe when not running (no background process, no auto-start).
- Local decrypt blocked: emoticon.db SQLCipher encrypted, Persist/ encrypted, fav.archive gone in 4.x.

## Ingest
- Drop files into `~/Desktop/NY/stickers/` — watcher auto-ingests, deduplicates (SHA256 + phash), standardizes (PIL LANCZOS, max 240px), generates thumbnail.
- stk_NNN files with no DB row are auto-detected (runtime + boot-time sweep).
- Descriptions: batch fill via `/sticker-entry`, single update via `sticker_update` MCP tool, or edit stickers.md directly.


## Downloaded
- Chiikawa TC 40p https://store.line.me/stickershop/product/24329/zh-Hant (author: San-Byte Creative / nagano)