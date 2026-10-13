# Kroger Reorder

A tiny local web page that pushes your saved grocery lists into your Kroger cart in one click. Keep several named lists, see product photos and sale prices, and import a list from a file. You finish checkout (pickup time, payment) in the Kroger app or at kroger.com.

## One-time setup

### 1. Register a Kroger developer app (free, ~5 minutes)

1. Go to https://developer.kroger.com and sign in with your regular Kroger account.
2. Open **My Apps**, then create a new app.
3. Name it anything (e.g. `kroger-reorder`).
4. Under **Redirect URL**, enter exactly: `http://localhost:8000/callback`
5. Make sure the app has access to the public **Products**, **Locations**, and **Cart** APIs. If the portal doesn't ask, they're included by default.
6. Save, then copy the **Client ID** and **Client Secret**.

### 2. Install

Requires Python 3.12 or newer.

```powershell
cd C:\Projects\kroger-reorder
python -m venv .venv
.venv\Scripts\python -m pip install -e .
copy .env.example .env
```

Open `.env` and paste in your Client ID and Client Secret.

### 3. Run

```powershell
.venv\Scripts\python -m app
```

Your browser opens to http://localhost:8000. Enter your zip, pick your store, and click **Sign in to Kroger**. You only do this once; the app stays signed in using a refresh token.

The console also prints a wifi address like `http://192.168.1.20:8000`. Open that on your phone or any device on the same network. Sign in to Kroger from this computer; every device shares the sign-in.

## Everyday use

1. Empty your Kroger cart in the app (the API can only *add*; it can't see or clear what's already there).
2. Run `.venv\Scripts\python -m app`.
3. Pick a list from the dropdown. The ⋯ menu creates, renames, duplicates, deletes, or imports.
4. Uncheck anything you don't need this trip, tweak quantities, click **Send to cart**.
5. Open the Kroger app, choose a pickup time, pay.

To add an item to the current list, search for it in the **Add an item** box and click the match.

## Sale prices

Items show a green sale price with the regular price struck through when your store has a promo. Prices are checked in the background when you open a list that hasn't been checked in a day, or any time with **Check prices**. These are shelf promos from Kroger's catalog, not digital coupons; coupons still have to be clipped in the Kroger app.

## Importing a list

From the ⋯ menu choose **Import items**, then pick a file or paste text. Accepted shapes:

- Plain text or Markdown: one item per line. `- [ ] 2x milk`, `eggs x3`, and `bread (4)` all work.
- CSV: a `name` column and optional `quantity` column, or just two columns.
- TSV: two columns pasted straight from a spreadsheet.
- JSON: an array of strings or of `{"name": ..., "quantity": ...}` objects.

Each line is matched against your store's catalog. Pick a different match or skip a line in the review table, then **Add to list**. Items already in the list get their quantity bumped.

## Where your data lives

Everything is plain JSON in `data/` (gitignored):

- `lists/<name>.json` - one file per list. Safe to edit by hand; an unreadable or malformed file is shown as a warning and moved aside as `<name>.json.corrupt-<timestamp>` the next time you add an item.
- After upgrading from v1, your old `list.json` becomes `lists/standard.json` and the original is kept as `list.json.migrated`.
- `config.json` - chosen store.
- `tokens.json` - Kroger sign-in tokens. Delete this file to sign out.

## Troubleshooting

- **"Missing KROGER_CLIENT_ID..."** - `.env` isn't filled in.
- **"Kroger sign-in failed: ..."** - you denied access on Kroger's page, or the redirect URL on your Kroger app doesn't match `http://localhost:8000/callback`. Fix and click Sign in again.
- **Sign-in button came back** - Kroger rejected the refresh token. Just sign in again; your list is untouched.
- **An item is highlighted red after Send** - Kroger rejected that UPC (often discontinued). Remove it and search for a replacement.
- **"Kroger said: ..."** - the raw message from Kroger's API. If it mentions rate limits, wait a minute.
- **"Port 8000 is already in use"** - another copy is running, or something else owns the port. Close it, or set `PORT` in `.env` and update the redirect URL on your Kroger app to match.
- **Can't sign in from my phone** - expected. Sign in once from the computer running the app.
- **Can't delete a list** - you can't delete the last one. Create another first.

## Development

```powershell
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
```

Tests never hit the network; the Kroger client is exercised through `httpx.MockTransport`.

### Manual smoke test (do this once after setup)

1. Start the app, finish store + sign-in.
2. Search `milk`, click one result.
3. Click **Send to cart**.
4. Open the Kroger app: the item should be in your cart with pickup selected.
5. Open the wifi URL on a phone. Images and the list should appear; the sign-in button is replaced by a note.
6. Search an item that's on sale at your store; the result and the list row show a green badge.
7. Import a five-line text file via the ⋯ menu; confirm matches; the items appear.
8. Create a second list, switch between them, delete one.
