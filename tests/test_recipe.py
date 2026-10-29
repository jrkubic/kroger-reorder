import json

import httpx
import pytest

from kroger_reorder.recipe import (BAD_URL, KINDS, NO_RECIPE, NOT_HTML, SYSTEM, TOO_LARGE, MAX_BYTES, Extracted,
                                   RecipeError, extract, fetch_page, parse, suggest, user_text)
from kroger_reorder.storage import Item
from kroger_reorder.vision import CliTimeout, VisionError
from tests.test_vision import api_message, envelope, fake_client, fake_runner, which_ok


def transport(handler):
    return httpx.MockTransport(handler)


def html_response(body="<html><body>hi</body></html>", status=200, content_type="text/html; charset=utf-8"):
    return httpx.Response(status, content=body.encode("utf-8"), headers={"content-type": content_type})


# -- fetch_page -----------------------------------------------------------------------

@pytest.mark.parametrize("url", ["", "   ", "allrecipes.com/recipe/1", "ftp://x/y", "http://", "javascript:alert(1)",
                                 "http://[bad/"])
def test_fetch_rejects_urls_without_http_scheme_and_host(url):
    with pytest.raises(RecipeError) as exc:
        fetch_page(url, transport=transport(lambda r: html_response()))
    assert exc.value.status == 400 and exc.value.message == BAD_URL


def test_fetch_returns_html_and_final_url_after_redirect():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("user-agent", "")))
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://site.test/new"})
        return html_response("<html><title>Soup</title></html>")
    page = fetch_page("https://site.test/old", transport=transport(handler))
    assert page.url == "https://site.test/new" and "<title>Soup</title>" in page.html
    assert all("Mozilla" in ua for _, ua in seen), "recipe sites block non-browser user agents"


def test_fetch_redirect_to_unsupported_scheme_is_400():
    def handler(request):
        return httpx.Response(302, headers={"location": "javascript:alert(1)"})
    with pytest.raises(RecipeError) as exc:
        fetch_page("https://site.test/r", transport=transport(handler))
    assert exc.value.status == 400 and exc.value.message == BAD_URL


def test_fetch_redirect_loop_is_502_without_doubled_period():
    def handler(request):
        return httpx.Response(301, headers={"location": str(request.url)})
    with pytest.raises(RecipeError) as exc:
        fetch_page("https://site.test/loop", transport=transport(handler))
    assert exc.value.status == 502
    assert exc.value.message.startswith("Couldn't fetch that page: ") and ".." not in exc.value.message


def test_fetch_http_error_is_502_with_status_and_import_hint():
    with pytest.raises(RecipeError) as exc:
        fetch_page("https://site.test/r", transport=transport(lambda r: html_response(status=403)))
    assert exc.value.status == 502
    assert exc.value.message == ("That site refused the request (HTTP 403). Copy the ingredients from the page "
                                 "and use Import instead.")


def test_fetch_non_html_is_415():
    with pytest.raises(RecipeError) as exc:
        fetch_page("https://site.test/r.pdf", transport=transport(lambda r: html_response(content_type="application/pdf")))
    assert exc.value.status == 415 and exc.value.message == NOT_HTML


def test_fetch_missing_content_type_is_tolerated():
    def handler(request):
        return httpx.Response(200, content=b"<html>x</html>")
    assert fetch_page("https://site.test/r", transport=transport(handler)).html == "<html>x</html>"


def test_fetch_decodes_declared_charset():
    def handler(request):
        return httpx.Response(200, content="<p>café</p>".encode("iso-8859-1"),
                              headers={"content-type": "text/html; charset=iso-8859-1"})
    assert "café" in fetch_page("https://site.test/r", transport=transport(handler)).html


def test_fetch_exactly_max_bytes_succeeds():
    body = b"x" * MAX_BYTES

    def handler(request):
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})
    assert len(fetch_page("https://site.test/r", transport=transport(handler)).html) == MAX_BYTES


def test_fetch_oversize_is_413():
    big = "<html>" + "x" * (MAX_BYTES + 1) + "</html>"
    with pytest.raises(RecipeError) as exc:
        fetch_page("https://site.test/r", transport=transport(lambda r: html_response(big)))
    assert exc.value.status == 413 and exc.value.message == TOO_LARGE


def test_fetch_connection_failure_is_502():
    def handler(request):
        raise httpx.ConnectError("getaddrinfo failed")
    with pytest.raises(RecipeError) as exc:
        fetch_page("https://nope.test/r", transport=transport(handler))
    assert exc.value.status == 502
    assert exc.value.message.startswith("Couldn't fetch that page: ") and "getaddrinfo failed" in exc.value.message


# -- extract --------------------------------------------------------------------------

def ld(obj):
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


RECIPE = {"@context": "https://schema.org", "@type": "Recipe", "name": "Chicken Tikka Masala",
          "recipeYield": ["4", "4 servings"],
          "recipeIngredient": ["2 lbs boneless chicken thighs", "1 cup plain yogurt", "  2 tbsp  garam masala "]}


def test_extract_reads_recipe_object():
    out = extract(f"<html><head><title>Site</title>{ld(RECIPE)}</head><body>junk</body></html>")
    assert out.title == "Chicken Tikka Masala" and out.yield_ == "4"
    assert out.ingredients == ["2 lbs boneless chicken thighs", "1 cup plain yogurt", "2 tbsp garam masala"]
    assert out.text == ""


def test_extract_finds_recipe_inside_graph_with_type_list_after_a_broken_block():
    doc = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "name": "A page"},
        {"@type": ["Recipe", "NewsArticle"], "name": "Soup", "recipeIngredient": "1 onion"},
    ]}
    html = '<script type="application/ld+json">{not json</script>' + ld(doc)
    out = extract(html)
    assert out.title == "Soup" and out.ingredients == ["1 onion"] and out.yield_ == ""


def test_extract_skips_recipe_objects_without_ingredients():
    html = ld({"@type": "Recipe", "name": "Empty"}) + ld({"@type": "recipe", "name": "Full",
                                                          "recipeIngredient": [{"name": "2 eggs"}, "salt"]})
    out = extract(html)
    assert out.title == "Full" and out.ingredients == ["2 eggs", "salt"]


def test_extract_uses_page_title_when_recipe_has_no_name():
    html = "<title>Grandma's Soup | Site</title>" + ld({"@type": "Recipe", "recipeIngredient": ["salt"]})
    assert extract(html).title == "Grandma's Soup | Site"


def test_extract_falls_back_to_visible_text_without_scripts_nav_or_tags():
    body = "<p>Ingredients</p><ul><li>2 eggs</li><li>1 cup flour</li></ul>" + "<p>more text</p>" * 30
    html = (f"<html><head><title>Pancakes | Site</title><style>p{{}}</style><script>var x=1;</script></head>"
            f"<body><nav>Home Recipes</nav><header>Logo</header>{body}<footer>© Site</footer></body></html>")
    out = extract(html)
    assert out.title == "Pancakes | Site" and out.ingredients == []
    assert "2 eggs 1 cup flour" in out.text
    for gone in ("var x=1", "p{}", "Home Recipes", "Logo", "© Site", "<li>"):
        assert gone not in out.text


def test_extract_with_too_little_text_is_422():
    with pytest.raises(RecipeError) as exc:
        extract("<html><body><p>Search results</p></body></html>")
    assert exc.value.status == 422 and exc.value.message == NO_RECIPE


def test_extract_truncates_long_text():
    html = "<html><body>" + "<p>word</p>" * 10000 + "</body></html>"
    assert len(extract(html).text) == 20_000


def test_extract_title_is_the_first_title_outside_skipped_subtrees():
    body = "<p>more text</p>" * 30
    html = f"<html><head><title>Real Title</title></head><body><svg><title>Icon Name</title></svg>{body}</body></html>"
    assert extract(html).title == "Real Title"
    html = f"<html><body><svg><title>Icon Name</title></svg>{body}</body></html>"
    assert extract(html).title == ""


def test_extract_recovers_from_unclosed_nav_inside_header():
    body = "<p>more text</p>" * 30
    out = extract(f"<html><body><header><nav>Menu</header>{body}</body></html>")
    assert "more text" in out.text and "Menu" not in out.text


def test_extract_tolerates_cdata_wrapped_json_ld():
    html = '<script type="application/ld+json">//<![CDATA[\n' + json.dumps(RECIPE) + '\n//]]></script>'
    assert extract(html).title == "Chicken Tikka Masala"


def test_extract_accepts_script_type_with_charset_parameter():
    html = '<script type="application/ld+json; charset=utf-8">' + json.dumps(RECIPE) + '</script>'
    assert extract(html).title == "Chicken Tikka Masala"


def test_extract_accepts_iri_type():
    html = ld({"@type": "https://schema.org/Recipe", "name": "Iri", "recipeIngredient": ["salt"]})
    assert extract(html).title == "Iri"


def test_extract_skips_a_block_nested_too_deep_to_walk():
    html = '<script type="application/ld+json">' + "[" * 3000 + "]" * 3000 + "</script>" + ld(RECIPE)
    assert extract(html).title == "Chicken Tikka Masala"


def test_extract_unescapes_html_entities_in_ingredients():
    html = ld({"@type": "Recipe", "name": "X", "recipeIngredient": ["1 cup flour &amp; sugar"]})
    assert extract(html).ingredients == ["1 cup flour & sugar"]


def test_extract_reads_quantitative_value_yield():
    html = ld({**RECIPE, "recipeYield": {"@type": "QuantitativeValue", "value": 4}})
    assert extract(html).yield_ == "4"


# -- prompt, parse, suggest ---------------------------------------------------------------

EXTRACTED = Extracted("Chicken Tikka Masala", "4 servings", ["2 lbs chicken thighs", "1 cup yogurt"], "")
ITEMS = [Item(upc="1", name="Large Eggs", size="12 ct", quantity=1), Item(upc="2", name="Whole Milk", size="1 gal", quantity=2)]


def test_user_text_has_title_yield_ingredients_list_items_note_and_history():
    text = user_text(EXTRACTED, "double it", "Weekly", ITEMS, "- Whole Milk: 5x")
    for needle in ("Recipe: Chicken Tikka Masala", "Yield: 4 servings", "- 2 lbs chicken thighs", "- 1 cup yogurt",
                   "Items already on the list 'Weekly':", "- Large Eggs ×1", "- Whole Milk ×2",
                   "User's note: double it", "- Whole Milk: 5x"):
        assert needle in text, needle


def test_user_text_uses_page_text_and_empty_placeholder():
    page = Extracted("Pancakes", "", [], "Ingredients 2 eggs 1 cup flour ...")
    text = user_text(page, "", "Weekly", [], "")
    assert "Ingredients:" not in text and "Ingredients 2 eggs 1 cup flour" in text
    assert "(empty)" in text and "User's note: (none)" in text


def test_system_prompt_names_the_three_kinds():
    assert KINDS == ("needed", "staple", "on_list")
    for kind in KINDS:
        assert f'"{kind}"' in SYSTEM


def test_parse_uses_recipe_kinds_and_defaults_to_needed():
    out = parse(json.dumps([{"name": "chicken thighs", "quantity": 2, "kind": "needed", "reason": "r"},
                            {"name": "salt", "kind": "staple"}, {"name": "eggs", "kind": "on_list"},
                            {"name": "yogurt", "kind": "seen"}]))
    assert [(s.name, s.quantity, s.kind) for s in out] == [
        ("chicken thighs", 2, "needed"), ("salt", 1, "staple"), ("eggs", 1, "on_list"), ("yogurt", 1, "needed")]


def test_suggest_api_sends_prompt_without_images():
    make = fake_client(result=api_message(json.dumps([{"name": "chicken thighs", "kind": "needed"}])))
    out = suggest(EXTRACTED, "", "Weekly", ITEMS, "", model="m", timeout=5, api_key="k", make_client=make)
    assert [s.name for s in out] == ["chicken thighs"]
    (call,) = make.calls
    assert call["system"] == SYSTEM
    (block,) = call["messages"][0]["content"]
    assert block["type"] == "text" and "Large Eggs ×1" in block["text"]


def test_suggest_cli_runs_without_tools():
    run = fake_runner(stdout=envelope(json.dumps([{"name": "yogurt"}])))
    out = suggest(EXTRACTED, "", "Weekly", ITEMS, "", model="m", timeout=5, runner=run, which=which_ok)
    assert [s.name for s in out] == ["yogurt"]
    (args, stdin_text, _), = run.calls
    assert "--allowedTools" not in args and args[args.index("--tools") + 1] == ""
    assert "Large Eggs ×1" in stdin_text and SYSTEM in stdin_text


def test_suggest_timeout_mentions_recipe_page():
    def slow(args, stdin_text, timeout):
        raise CliTimeout()
    with pytest.raises(VisionError) as exc:
        suggest(EXTRACTED, "", "Weekly", [], "", model="m", timeout=30, runner=slow, which=which_ok)
    assert exc.value.message == "Claude took longer than 30 seconds; try a shorter recipe page."
