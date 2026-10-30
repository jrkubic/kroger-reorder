# Review table: category breakout and search-for-another-item (v6.2) — design

**Problems (Jake, 2026-10-30).**

1. Photo suggestions came back as categories with the choices buried in the reason ("Fresh fruit:
   bananas or strawberries"), so one row stood for several products and the dropdown could only
   pick a catalog match for the category name.
2. A row's dropdown offers three catalog matches and Skip. When none fits (cheese sticks, but the
   household wants the light ones) there is no way to search for something else without leaving the
   review.

**Decisions.** Claude returns one row per specific product; rows that are alternatives for one need
carry the same `group` and are shown together under a heading, every option starting on add (Jake's
choice). Every dropdown gains "Search for a different item…", which turns the cell into a search box
and re-matches that row against the store catalog.

## Changes

1. **Prompt and model.** `Suggestion` gains `group: str = ""`; `parse_suggestions` reads it (trimmed,
   40 chars). Both system prompts (`vision.SYSTEM`, `recipe.SYSTEM`) add the field and the rule: one
   row per specific product, never a category; alternatives for one need share a `group`; a single
   specific item leaves it empty. `suggestion_line()` passes `group` to the page.
2. **Review table.** `reviewLines()` places rows with the same group next to each other (first
   appearance order). `renderImport()` draws a heading row before each group. Kind defaults are
   unchanged: grouped photo options are `low`/`missing`/`asked`, so they start on add.
3. **Search in place.** Each select ends with `Search for a different item…` (value `-2`). Choosing
   it swaps the cell for a text box prefilled with the row's name and a Search button; Enter or the
   button calls the existing `POST /api/import/resolve` with that one term, replaces the row's
   candidates and name, and re-renders only that row (other rows keep their picks and quantities).
   No match keeps the row on "No match found — skip" with a warning banner. A row in search mode
   counts as not picked. Works for Import, Photo, and Recipe rows alike.
4. **Docs.** README Importing and Photo suggestions paragraphs; version 6.2.0.

Out of scope: more than three catalog candidates per row (search covers it); remembering searches.
