# Kroger Reorder

A tiny local web page that pushes your saved "standard" grocery list into your Kroger cart in one click. You finish checkout (pickup time, payment) in the Kroger app or at kroger.com.

## One-time setup

### 1. Register a Kroger developer app (free, ~5 minutes)

1. Go to https://developer.kroger.com and sign in with your regular Kroger account.
2. Open **My Apps** -> **Register App**.
3. Name it anything (e.g. `kroger-reorder`).
4. Under **Redirect URL**, enter exactly: `http://localhost:8000/callback`
5. Request the **Products**, **Locations**, and **Cart** APIs.
6. Save, then copy the **Client ID** and **Client Secret**.

### 2. Install

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

## Everyday use

1. Empty your Kroger cart in the app (the API can only *add*; it can't see or clear what's already there).
2. Run `.venv\Scripts\python -m app`.
3. Uncheck anything you don't need this trip, tweak quantities, click **Send to cart**.
4. Open the Kroger app, choose a pickup time, pay.

To add an item to the standard list, search for it in the **Add an item** box and click the match.

## Where your data lives

Everything is plain JSON in `data/` (gitignored):

- `list.json` - your standard list. Safe to edit by hand.
- `config.json` - chosen store.
- `tokens.json` - Kroger sign-in tokens. Delete this file to sign out.

## Troubleshooting

- **"Missing KROGER_CLIENT_ID..."** - `.env` isn't filled in.
- **"Kroger sign-in failed: ..."** - you denied access on Kroger's page, or the redirect URL on your Kroger app doesn't match `http://localhost:8000/callback`. Fix and click Sign in again.
- **Sign-in button came back** - Kroger rejected the refresh token. Just sign in again; your list is untouched.
- **An item is highlighted red after Send** - Kroger rejected that UPC (often discontinued). Remove it and search for a replacement.
- **"Kroger said: ..."** - the raw message from Kroger's API. If it mentions rate limits, wait a minute.

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
