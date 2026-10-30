# Photo suggestions find gaps, not inventory (v6.1) — design

**Problem (Jake, 2026-10-30).** Four fridge/pantry photos with the note "let's make a grocery list for
what I need" came back as a list of what was already in the fridge, every row preset to add. The v4
prompt asked Claude for "items visible in the photos that a household would re-buy" first, so a
stocked kitchen produced a stocked inventory; the review treated those rows like any import line; and
the current shopping list was never sent, so items already on it came back as suggestions too.

**Decision.** Photos should answer "what do I need to buy?". Stocked items are still shown, but with
Skip preselected (same treatment as staples and on-list rows from recipes), so a misread photo is
visible and one click adds anything Claude got wrong.

## Changes

1. **Prompt** (`vision.SYSTEM`): the goal is gaps. Kinds become
   `low` (visible but nearly gone), `missing` (not in any photo but expected: a regular purchase
   from the history that is due again, or something the photos imply), `asked` (named in the note
   or clearly implied by it, such as a meal or a guest count), `on_list` (already on the shopping list; say what it has), and `stocked` (visible and
   plentiful; at most ten, the most notable, so the user can check what Claude saw). Order:
   asked, low, missing, on_list, stocked. An item already on the list is `on_list` whatever the
   photos or note say. An unknown kind falls back to `stocked` (Skip preselected). Other rules
   unchanged (plain names, no repeats, 40 max).
2. **Current list goes with the photos.** `POST /api/photo/suggest` takes the target list `slug`
   (the dialog already has the picker) and the route passes that list's name and items to the
   analyzer, exactly as the recipe route does. `vision.list_text()` is the shared formatter;
   `recipe.py` imports it. `analyze(photos, note, list_name, items, profile, ...)`.
3. **Review table**: rows tagged `stocked` start on Skip, alongside `staple` and `on_list`. Tag
   colours: green for `asked`/`needed`, amber for `low`/`missing`, grey for `stocked`/`staple`,
   blue for `on_list`.
4. **Docs**: README Photo suggestions and Features text, HTTP API row, version 6.1.0.

Out of scope: hiding stocked rows entirely (user chose to see them), per-photo captions.
