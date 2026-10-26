# Kroger Reorder v4 — Photo suggestions and usage profile

Date: 2026-10-26. Builds on v3 (`2026-10-26-order-history-design.md`).

## Goal

Let anyone on the home wifi photograph a fridge, cupboard, or pantry, add a note, and get a recommended
list to start from. Suggestions improve over time by feeding Claude a usage profile derived from order history.

## Decisions

- **Claude access: headless Claude Code on the host** (`claude -p`), chosen by Jake over an API key so it
  runs on his subscription. Constraints: the host must keep Claude Code installed and signed in; each
  analysis takes 5–30 s; the request shares his subscription's usage limits. Model defaults to
  `claude-opus-5`, overridable with `PHOTO_MODEL` in `.env`.
- **List content: seen + suggested + user notes.** Claude returns items it can see, items that look missing
  or low, and anything the user's note implies, each tagged `seen` or `suggested` with a one-line reason.
- **Photo source:** the browser's file picker with `accept="image/*"` and no `capture` attribute, so phones
  offer both camera and library. Up to 4 photos; the browser downscales each to ≤1600 px JPEG before upload.
- **Target list is chosen in the dialog** (default: active list; "New list…" prompts for a name).
- **Usage profile** is derived from `data/orders.json`, not stored separately: per UPC — name, times
  ordered, average quantity, last ordered date, average days between orders. Sent to Claude as compact text
  and shown as a table on the Orders tab.
- **Results reuse the import review table.** Each line becomes a parsed line with `name`, `quantity`, and a
  `note` (the reason); names are resolved against the store catalog as import already does; the user edits
  matches and quantities, then **Add to list** commits to the chosen list.
- Photos are written to a temporary directory for the duration of one request and deleted after. Nothing is
  logged or kept.

## Components

- `app/usage.py` — `usage_profile(orders) -> list[dict]` (pure) and `profile_text(profile) -> str`.
- `app/vision.py` — `analyze(photos: list[Path], note: str, profile_text: str, model: str, timeout: float)
  -> list[Suggestion]`. Builds the prompt, runs `claude -p … --allowedTools Read --output-format json
  --max-turns 4`, parses the `result` field as a JSON array, validates each entry. Raises `VisionError`
  with a human message for: CLI not found, non-zero exit / sign-in problems, timeout, unparseable output.
  The subprocess runner is injectable so tests never spawn Claude Code.
- Routes: `GET /api/usage` → `{"items": [...]}`; `POST /api/photo/suggest` (multipart: `photos[]`,
  `note`) → `{"lines": [{"raw", "name", "quantity", "exact": null, "note", "kind"}]}`. The page then calls
  the existing `/api/import/resolve` and `/api/lists/{slug}/import`.
- Settings: `photo_model` (default `claude-opus-5`), `photo_timeout` (default 90 s).
- UI: **Photo** button beside Import; `<dialog id="photo-dialog">` with list selector, file input
  (multiple), thumbnails, note textarea, progress banner; on success it closes and opens the import dialog
  pre-filled with the review table (reason shown under each name, `seen`/`suggested` tag). Orders tab gains
  a **Usage** section listing the profile.

## Prompt (system part, abbreviated)

"You are helping build a grocery shopping list from photos of a household's fridge, freezer, pantry or
cupboard, plus the user's note and their purchase history. Return ONLY a JSON array. Each element:
{"name": "<generic product name as you'd type it into a grocery search>", "quantity": <int 1–10>,
"kind": "seen" | "suggested", "reason": "<one short sentence>"}. Include: items visible that a household
would re-buy; items that appear empty, nearly empty, or absent but implied by the photo, note, or history;
anything the note asks for. Prefer plain names (e.g. "whole milk", "large eggs") over brands unless the
brand is clearly visible. Do not include non-food household items unless visible or asked. Max 40 items."

## Error handling

- CLI missing → 503 "Claude Code isn't installed on the computer running the app." Not signed in / exit
  code ≠ 0 → 502 with the CLI's last stderr line. Timeout → 504. Bad JSON → 502 "Claude's answer couldn't
  be read; try again." No photos → 400. Too many / too large → 413.
- The UI keeps the chosen photos and note on failure so the user can retry.

## Testing

- `tests/test_usage.py`: profile math (counts, averages, intervals, newest-first by last order, unknown
  dates tolerated).
- `tests/test_vision.py`: prompt includes note and profile; parsing of good output, fenced output,
  garbage; each error path via a fake runner; max item cap and quantity clamp.
- `tests/test_routes.py`: `/api/usage` from recorded orders; `/api/photo/suggest` happy path with a fake
  analyzer, 400 with no photos, 413 over limits, 503/502/504 mapping.
- Manual: phone upload via wifi, both camera and library; PC upload; new-list path; a failure path with
  Claude Code signed out.
