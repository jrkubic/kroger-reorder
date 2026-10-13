# Kroger Reorder v2 — Named lists, images, sale badges, LAN access, import

Date: 2026-10-12. Builds on the v1 design (`2026-10-12-kroger-reorder-design.md`), which shipped and passed the real-API smoke test.

## Scope decisions

- **Multiple named lists**, switchable from a dropdown; create, rename, duplicate, delete. Chosen over "one master list with saved selections" and over "lists plus trip history" (YAGNI).
- **Product images** come from the Products API `images` field. No scraping needed.
- **Sale badges** use the Products API `price.promo` vs `price.regular`. Prices are cached per item with a timestamp and refreshed in the background when stale (>24h) or on demand.
- **Digital coupons / clipping: deferred.** Kroger's public API has no coupon endpoint and no clip action. A kroger.com scraper (Playwright + persistent profile) was discussed and explicitly postponed by the user.
- **LAN access**: bind `0.0.0.0`, print LAN URL. Kroger sign-in works only from the host (redirect URL is `localhost`); tokens are server-side so one sign-in covers every device. No page login; trust the home wifi.
- **Import** CSV / JSON / Markdown / plain text through a review screen with the top search match preselected and alternates available. Chosen over auto-pick and over unresolved placeholders.

## Data model

One file per list at `data/lists/<slug>.json`:

```json
{
  "name": "Weekly staples",
  "items": [
    {
      "upc": "0001111041700",
      "name": "Kroger Whole Milk",
      "size": "1 gal",
      "image": "https://.../medium/front/0001111041700",
      "quantity": 1,
      "included": true,
      "price": 3.49,
      "promo": 2.99,
      "price_checked_at": "2026-10-12T14:30:00"
    }
  ]
}
```

- Slug derived from name (`weekly-staples`); collisions get `-2`, `-3`. Renaming keeps the slug.
- `image`, `price`, `promo`, `price_checked_at` are optional (null) so hand-written files load.
- `config.json` gains `active_list`.
- **Migration**: on startup, if `data/list.json` exists and `data/lists/` does not, move it to `data/lists/standard.json` named "Standard" and set it active. Rename, never delete.
- `Storage` becomes list-aware: `list_lists()`, `load_list(slug)`, `save_list(slug, name, items)`, `delete_list(slug)`, `rename_list`, `duplicate_list`. Per-file corrupt backup and locking carry over.

## Kroger client changes

- `search_products` also returns `image` (medium front image URL or null).
- New `lookup_product(upc, location_id)` → single product, same shape as a search result. Used for price refresh.

## Routes

| Route | Purpose |
|---|---|
| `GET /api/state` | adds `lists: [{slug, name, count}]`, `active_list` |
| `POST /api/lists` `{name}` | create → `{slug}` |
| `PATCH /api/lists/{slug}` `{name}` | rename |
| `POST /api/lists/{slug}/duplicate` `{name}` | copy |
| `DELETE /api/lists/{slug}` | delete; refuses the last list; if active, switches to first remaining |
| `PUT /api/active` `{slug}` | switch active list |
| `GET /api/lists/{slug}` | `{name, items, warning}` |
| `POST /api/lists/{slug}/items`, `PUT .../items`, `DELETE .../items/{upc}` | as v1, scoped |
| `POST /api/lists/{slug}/prices` | refresh price/promo for all items (sequential lookups; per-item failures keep old values) → updated list |
| `POST /api/lists/{slug}/push` | as v1, scoped |
| `POST /api/import/parse` | multipart file or `{text}` → `{lines: [{raw, name, quantity}]}` |
| `POST /api/import/resolve` | `{names: [...]}` → `{matches: [{name, candidates: [product...]}]}` (top 3 each) |
| `POST /api/lists/{slug}/import` | `[{upc, name, size, image, quantity}]` → merged list; existing UPCs get quantity bumped |

## Page

- JS moves to `app/static/app.js` (vanilla, no build).
- Header: list dropdown + "⋯" menu (New, Rename, Duplicate, Delete, Import).
- Rows: 56px thumbnail; sale badge "**$2.99** ~~$3.49~~" when `promo` < `price`; "Check prices" link with "checked N ago"; auto-refresh on open when stale.
- Search results show thumbnail and badge.
- Import dialog: file picker or paste box → Parse → review table (raw text, quantity, preselected match with alternates; no-match rows default to Skip) → "Add N items".
- Footer unchanged; push targets the active list.
- Sign-in button on non-host devices shows "Sign in from the computer running the app" instead of a link (detected by comparing `location.hostname` to `localhost`/`127.0.0.1`).

## Import parsing

Format is sniffed, not extension-trusted:
- JSON: array of strings, or objects `{name, quantity?}`.
- CSV: optional header (`name`, `quantity` columns); else col 1 = name, numeric col 2 = quantity.
- Markdown/text: one item per line; strip bullets (`-`, `*`, `1.`), checkbox markers (`[ ]`, `[x]`), headings; quantity from `2x milk`, `milk x2`, `milk (2)`.
- Default quantity 1. Blank lines ignored.

## LAN

- `uvicorn.run(host="0.0.0.0")`. Startup prints `http://localhost:PORT` and `http://<lan-ip>:PORT` plus "Sign in to Kroger from this computer." LAN IP via a UDP socket connect trick; falls back to omitting the line.
- Port probe and OAuth redirect unchanged.

## Error handling (additions)

- Delete active list → switch to first remaining. Delete last list → 400 with message.
- Per-list load warning banner; per-file backup on write.
- Price refresh: per-item failures silent; one banner if all fail.
- Import: unreadable file → "Couldn't read that file as CSV, JSON, or text."

## Testing

- Storage: multi-list CRUD, slugs and collisions, migration, per-list backup.
- Kroger: image extraction, `lookup_product` parsing.
- Import parser: every format, quantity patterns, junk lines (pure functions).
- Routes: list CRUD, active switching, last-list refusal, price refresh with partial failure, import parse/resolve/commit, quantity bump on duplicate import.
- Manual: phone on wifi sees images and a badge; import a five-line text file; push.
