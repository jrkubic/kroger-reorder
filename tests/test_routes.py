# tests/test_routes.py
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.kroger import KrogerError, NotSignedIn
from app.main import create_app
from app.storage import Item, Storage


def product(upc, name, **over):
    base = {"upc": upc, "name": name, "brand": "Kroger", "size": "1 gal", "price": 3.49, "promo": None,
            "image": f"https://img/{upc}.jpg"}
    base.update(over)
    return base


class FakeKroger:
    def __init__(self):
        self.signed_in = False
        self.exchanged_code = None
        self.exchanged_state = None
        self.cart_calls = []
        self.cart_error: Exception | None = None
        self.lookups = {}       # upc -> product dict or Exception
        self.lookup_calls = []

    def is_signed_in(self):
        return self.signed_in

    def find_stores(self, zip_code):
        return [{"id": "01400943", "name": "Kroger Hyde Park", "address": "3760 Paxton Ave, Cincinnati"}]

    def search_products(self, term, location_id):
        return [product("0001", f"{term} at {location_id}", promo=2.99),
                product("0009", f"{term} alt")]

    def lookup_product(self, upc, location_id):
        self.lookup_calls.append(upc)
        result = self.lookups.get(upc)
        if isinstance(result, Exception):
            raise result
        return result or product(upc, f"Looked up {upc}", price=5.0, promo=4.0)

    def authorize_url(self):
        return "https://api.kroger.com/v1/connect/oauth2/authorize?fake=1"

    def exchange_code(self, code, state):
        self.exchanged_code = code
        self.exchanged_state = state
        self.signed_in = True

    def add_to_cart(self, items):
        if self.cart_error:
            raise self.cart_error
        self.cart_calls.append(items)


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path)


@pytest.fixture
def kroger():
    return FakeKroger()


@pytest.fixture
def client(storage, kroger):
    return TestClient(create_app(storage, kroger), follow_redirects=False)


@pytest.fixture
def with_store(storage):
    storage.save_config({**storage.load_config(), "store_id": "01400943", "store_name": "Kroger Hyde Park"})


MILK = {"upc": "0001", "name": "Whole Milk", "size": "1 gal"}
EGGS = {"upc": "0002", "name": "Eggs", "size": "12 ct"}
FULL = {"quantity": 1, "included": True, "image": None, "price": None, "promo": None, "price_checked_at": None}


# -- state / setup ---------------------------------------------------------------

def test_initial_state_has_default_list(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    assert r.json() == {
        "store": None, "signed_in": False,
        "lists": [{"slug": "standard", "name": "Standard", "count": 0}],
        "active_list": "standard",
    }


def test_find_stores(client):
    r = client.get("/api/stores", params={"zip": "45209"})
    assert r.json()["stores"][0]["id"] == "01400943"


def test_choose_store_persists_and_keeps_active_list(client, storage):
    r = client.put("/api/store", json={"id": "01400943", "name": "Kroger Hyde Park"})
    assert r.status_code == 200
    assert storage.load_config()["store_id"] == "01400943"
    assert client.get("/api/state").json()["active_list"] == "standard"


def test_login_redirects_to_kroger(client):
    r = client.get("/auth/login")
    assert r.status_code in (302, 307)
    assert r.headers["location"].startswith("https://api.kroger.com/v1/connect/oauth2/authorize")


def test_callback_exchanges_code_and_redirects_home(client, kroger):
    r = client.get("/callback", params={"code": "abc", "state": "s"})
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"
    assert (kroger.exchanged_code, kroger.exchanged_state) == ("abc", "s")
    assert client.get("/api/state").json()["signed_in"] is True


def test_callback_denied_redirects_home_with_error(client, kroger):
    r = client.get("/callback", params={"error": "access_denied"})
    assert r.headers["location"] == "/?auth_error=access_denied"
    assert kroger.exchanged_code is None


def test_callback_exchange_failure_redirects_home_with_error(client, kroger):
    def boom(code, state):
        raise KrogerError(400, "invalid_grant")
    kroger.exchange_code = boom
    r = client.get("/callback", params={"code": "x", "state": "s"})
    assert r.headers["location"] == "/?auth_error=invalid_grant"


def test_search_requires_store(client):
    r = client.get("/api/search", params={"term": "milk"})
    assert r.status_code == 400
    assert "store" in r.json()["error"].lower()


def test_search_returns_images_and_promo(client, with_store):
    r = client.get("/api/search", params={"term": "milk"})
    first = r.json()["results"][0]
    assert first["image"] == "https://img/0001.jpg"
    assert first["promo"] == 2.99


def test_kroger_error_becomes_502_with_message(client, with_store, kroger):
    def boom(term, location_id):
        raise KrogerError(429, "rate limited")
    kroger.search_products = boom
    r = client.get("/api/search", params={"term": "milk"})
    assert r.status_code == 502
    assert r.json() == {"error": "Kroger said: rate limited"}


def test_transport_error_is_502_without_kroger_said_prefix(client, with_store, kroger):
    def boom(term, location_id):
        raise KrogerError(0, "Could not reach Kroger: ConnectError")
    kroger.search_products = boom
    assert client.get("/api/search", params={"term": "milk"}).json() == {"error": "Could not reach Kroger: ConnectError"}


def test_index_and_static_js_are_served(client):
    assert "text/html" in client.get("/").headers["content-type"]
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_index_versions_the_script_and_disables_stale_caching(client):
    # A browser that cached an old app.js must not run it against a newer page: the script URL
    # changes whenever the file does, and both page and static files must always be revalidated.
    r = client.get("/")
    assert r.headers["cache-control"] == "no-cache"
    m = re.search(r'src="/static/app\.js\?v=(\d+)"', r.text)
    assert m, "index.html should reference a versioned app.js"
    js = client.get(f"/static/app.js?v={m.group(1)}")
    assert js.status_code == 200
    assert js.headers["cache-control"] == "no-cache"


def test_validation_error_uses_error_shape(client):
    r = client.post("/api/lists/standard/items", json={"upc": "1"})
    assert r.status_code == 422
    assert "name" in r.json()["error"]


# -- list CRUD ---------------------------------------------------------------------

def test_create_list_and_switch_active(client):
    r = client.post("/api/lists", json={"name": "Taco Night"})
    assert r.status_code == 200
    assert r.json() == {"slug": "taco-night"}
    r = client.put("/api/active", json={"slug": "taco-night"})
    assert r.json() == {"active_list": "taco-night"}
    state = client.get("/api/state").json()
    assert [l["slug"] for l in state["lists"]] == ["standard", "taco-night"]
    assert state["active_list"] == "taco-night"


def test_create_list_rejects_blank_name(client):
    assert client.post("/api/lists", json={"name": "   "}).status_code == 400


def test_rename_list(client):
    r = client.patch("/api/lists/standard", json={"name": "Weekly"})
    assert r.json() == {"slug": "standard", "name": "Weekly"}
    assert client.get("/api/lists/standard").json()["name"] == "Weekly"


def test_duplicate_list_copies_items(client):
    client.post("/api/lists/standard/items", json=MILK)
    r = client.post("/api/lists/standard/duplicate", json={"name": "Party"})
    assert r.json() == {"slug": "party"}
    assert [i["upc"] for i in client.get("/api/lists/party").json()["items"]] == ["0001"]


def test_delete_active_list_switches_to_remaining(client):
    client.post("/api/lists", json={"name": "Other"})
    r = client.delete("/api/lists/standard")
    assert r.status_code == 200
    assert r.json() == {"lists": [{"slug": "other", "name": "Other", "count": 0}], "active_list": "other"}


def test_delete_last_list_is_refused(client):
    r = client.delete("/api/lists/standard")
    assert r.status_code == 400
    assert "last" in r.json()["error"].lower()
    assert client.get("/api/state").json()["active_list"] == "standard"


def test_missing_list_is_404_with_error_shape(client):
    assert client.get("/api/lists/ghost").status_code == 404
    assert "error" in client.get("/api/lists/ghost").json()
    assert client.put("/api/active", json={"slug": "ghost"}).status_code == 404
    assert client.patch("/api/lists/ghost", json={"name": "x"}).status_code == 404


def test_get_list_includes_warning_when_file_is_corrupt(client, tmp_path):
    (tmp_path / "lists" / "standard.json").write_text("nope", encoding="utf-8")
    body = client.get("/api/lists/standard").json()
    assert body["items"] == []
    assert "not valid JSON" in body["warning"]


def test_rename_corrupt_list_is_refused_and_file_untouched(client, tmp_path):
    path = tmp_path / "lists" / "standard.json"
    path.write_text("nope", encoding="utf-8")
    r = client.patch("/api/lists/standard", json={"name": "Weekly"})
    assert r.status_code == 400
    assert "not valid JSON" in r.json()["error"]
    assert path.read_text(encoding="utf-8") == "nope"


def test_duplicate_corrupt_list_is_refused(client, tmp_path, storage):
    path = tmp_path / "lists" / "standard.json"
    path.write_text("nope", encoding="utf-8")
    r = client.post("/api/lists/standard/duplicate", json={"name": "Party"})
    assert r.status_code == 400
    assert "unreadable" in r.json()["error"]
    assert path.read_text(encoding="utf-8") == "nope"
    assert storage.list_slugs() == ["standard"]


def test_slug_traversal_is_404(client, tmp_path, storage):
    before = (tmp_path / "config.json").read_text(encoding="utf-8")
    r = client.put("/api/active", json={"slug": "../config"})
    assert r.status_code == 404
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == before
    assert storage.active_list() == "standard"


def test_delete_non_active_list_keeps_active_route(client):
    client.post("/api/lists", json={"name": "Other"})
    r = client.delete("/api/lists/other")
    assert r.status_code == 200
    assert r.json() == {"lists": [{"slug": "standard", "name": "Standard", "count": 0}], "active_list": "standard"}
    assert client.get("/api/lists/standard").status_code == 200


# -- items --------------------------------------------------------------------------

def test_add_item_stores_image_and_dedupes(client):
    r = client.post("/api/lists/standard/items", json={**MILK, "image": "https://img/1.jpg", "price": 3.49, "promo": 2.99})
    [milk] = r.json()["items"]
    assert milk.pop("price_checked_at")  # a priced add is stamped: the price was fetched just now
    expected = {**MILK, **FULL, "image": "https://img/1.jpg", "price": 3.49, "promo": 2.99}
    expected.pop("price_checked_at")
    assert milk == expected
    client.post("/api/lists/standard/items", json=MILK)
    assert len(client.get("/api/lists/standard").json()["items"]) == 1


def test_add_item_without_price_is_not_stamped(client):
    r = client.post("/api/lists/standard/items", json=EGGS)
    assert r.json()["items"] == [{**EGGS, **FULL}]


def test_null_size_from_kroger_is_accepted_and_stored_as_empty(client, storage):
    r = client.post("/api/lists/standard/items", json={"upc": "0003", "name": "Bread", "size": None})
    assert r.status_code == 200
    r = client.post("/api/lists/standard/import", json=[{"upc": "0004", "name": "Jam", "size": None, "image": None}])
    assert r.status_code == 200
    assert [i.size for i in storage.load_list("standard").items] == ["", ""]


def test_replace_items_saves_quantities_and_flags(client, storage):
    body = [{**MILK, **FULL, "quantity": 3, "included": False}, {**EGGS, **FULL}]
    r = client.put("/api/lists/standard/items", json=body)
    assert r.json()["items"] == body
    assert storage.load_list("standard").items[0].quantity == 3


def test_replace_items_with_empty_body_does_not_touch_corrupt_file(client, tmp_path):
    (tmp_path / "lists" / "standard.json").write_text("nope", encoding="utf-8")
    client.put("/api/lists/standard/items", json=[])
    assert (tmp_path / "lists" / "standard.json").read_text(encoding="utf-8") == "nope"


def test_delete_item_and_missing_item(client):
    client.post("/api/lists/standard/items", json=MILK)
    client.post("/api/lists/standard/items", json=EGGS)
    assert [i["upc"] for i in client.delete("/api/lists/standard/items/0001").json()["items"]] == ["0002"]
    assert [i["upc"] for i in client.delete("/api/lists/standard/items/9999").json()["items"]] == ["0002"]


# -- push ------------------------------------------------------------------------------

def test_push_sends_only_included_items_with_positive_quantity(client, kroger, storage):
    body = [{**MILK, **FULL, "quantity": 2}, {**EGGS, **FULL, "included": False},
            {"upc": "0003", "name": "Bread", "size": "", **FULL, "quantity": 0}]
    r = client.post("/api/lists/standard/push", json=body)
    assert r.status_code == 200
    assert kroger.cart_calls == [[("0001", 2)]]
    assert [i["upc"] for i in r.json()["added"]] == ["0001"]
    assert storage.load_list("standard").items[1].included is False


def test_push_with_nothing_checked_is_400(client, kroger):
    r = client.post("/api/lists/standard/push", json=[{**MILK, **FULL, "included": False}])
    assert r.status_code == 400
    assert kroger.cart_calls == []


def test_push_when_not_signed_in_is_401(client, kroger):
    kroger.cart_error = NotSignedIn()
    r = client.post("/api/lists/standard/push", json=[{**MILK, **FULL}])
    assert r.status_code == 401
    assert r.json()["signed_in"] is False


def test_push_kroger_rejection_flags_whole_token_upcs_and_keeps_edits(client, kroger, storage):
    kroger.cart_error = KrogerError(400, "Invalid UPC 0002")
    body = [{**MILK, **FULL}, {**EGGS, **FULL}, {"upc": "02", "name": "Partial", "size": "", **FULL}]
    r = client.post("/api/lists/standard/push", json=body)
    assert r.status_code == 502
    assert r.json() == {"error": "Kroger said: Invalid UPC 0002", "bad_upcs": ["0002"]}
    assert [i.upc for i in storage.load_list("standard").items] == ["0001", "0002", "02"]


def test_push_records_an_order_with_store_and_total(client, kroger, storage, with_store):
    body = [{**MILK, **FULL, "quantity": 2, "price": 3.49, "promo": 2.99}, {**EGGS, **FULL, "included": False}]
    r = client.post("/api/lists/standard/push", json=body)
    assert r.status_code == 200
    order = r.json()["order"]
    assert r.json()["warning"] is None
    assert order["list_slug"] == "standard" and order["list_name"] == "Standard"
    assert order["store_id"] == "01400943" and order["store_name"] == "Kroger Hyde Park"
    assert order["item_count"] == 1 and order["estimated_total"] == 5.98
    assert [i["upc"] for i in order["items"]] == ["0001"]
    assert [o["id"] for o in storage.list_orders()] == [order["id"]]


def test_rejected_push_records_nothing(client, kroger, storage):
    kroger.cart_error = KrogerError(400, "Invalid UPC 0001")
    client.post("/api/lists/standard/push", json=[{**MILK, **FULL}])
    assert storage.list_orders() == []


def test_push_still_succeeds_when_history_cannot_be_written(client, storage, monkeypatch):
    def boom(order):
        raise OSError("disk full")
    monkeypatch.setattr(storage, "record_order", boom)
    r = client.post("/api/lists/standard/push", json=[{**MILK, **FULL}])
    assert r.status_code == 200
    assert r.json()["order"] is None
    assert "order history" in r.json()["warning"]


# -- orders ------------------------------------------------------------------------------

def push_one(client, **over):
    r = client.post("/api/lists/standard/push", json=[{**MILK, **FULL, "price": 3.49, **over}])
    assert r.status_code == 200
    return r.json()["order"]


def test_list_orders_newest_first_with_full_items(client, kroger):
    first = push_one(client)
    second = push_one(client, quantity=3)
    r = client.get("/api/orders")
    assert r.status_code == 200
    orders = r.json()["orders"]
    assert [o["id"] for o in orders] == [second["id"], first["id"]]
    assert orders[0]["items"][0]["quantity"] == 3


def test_delete_order(client):
    order = push_one(client)
    assert client.delete(f"/api/orders/{order['id']}").status_code == 204
    assert client.get("/api/orders").json()["orders"] == []
    r = client.delete(f"/api/orders/{order['id']}")
    assert r.status_code == 404
    assert "No order" in r.json()["error"]


def test_reorder_pushes_snapshot_and_records_with_source(client, kroger, with_store):
    source = push_one(client, quantity=2)
    kroger.cart_calls.clear()
    r = client.post(f"/api/orders/{source['id']}/reorder")
    assert r.status_code == 200
    assert kroger.cart_calls == [[("0001", 2)]]
    new = r.json()["order"]
    assert new["reorder_of"] == source["id"]
    assert new["list_slug"] == "standard" and new["list_name"] == "Standard"
    assert new["item_count"] == 1 and new["estimated_total"] == 6.98
    assert [o["id"] for o in client.get("/api/orders").json()["orders"]] == [new["id"], source["id"]]


def test_reorder_unknown_is_404(client):
    r = client.post("/api/orders/nope/reorder")
    assert r.status_code == 404
    assert r.json() == {"error": "No order with id 'nope'."}


def test_reorder_with_nothing_sendable_is_400(client, storage):
    storage.record_order({"id": "empty", "placed_at": "2026-10-26T00:00:00+00:00", "items": [{"upc": "0001", "quantity": 0}]})
    r = client.post("/api/orders/empty/reorder")
    assert r.status_code == 400
    assert r.json()["error"] == "Nothing to send."


def test_reorder_kroger_rejection_flags_upcs_and_records_nothing(client, kroger, storage):
    source = push_one(client)
    kroger.cart_error = KrogerError(400, "Invalid UPC 0001")
    r = client.post(f"/api/orders/{source['id']}/reorder")
    assert r.status_code == 502
    assert r.json() == {"error": "Kroger said: Invalid UPC 0001", "bad_upcs": ["0001"]}
    assert [o["id"] for o in storage.list_orders()] == [source["id"]]


def test_reorder_when_not_signed_in_is_401(client, kroger):
    source = push_one(client)
    kroger.cart_error = NotSignedIn()
    r = client.post(f"/api/orders/{source['id']}/reorder")
    assert r.status_code == 401


# -- prices ------------------------------------------------------------------------------

def test_refresh_prices_requires_store(client):
    assert client.post("/api/lists/standard/prices").status_code == 400


def test_refresh_prices_updates_each_item(client, kroger, with_store):
    client.post("/api/lists/standard/items", json=MILK)
    client.post("/api/lists/standard/items", json=EGGS)
    r = client.post("/api/lists/standard/prices")
    assert r.status_code == 200
    items = r.json()["items"]
    assert kroger.lookup_calls == ["0001", "0002"]
    assert all(i["price"] == 5.0 and i["promo"] == 4.0 and i["price_checked_at"] for i in items)
    assert all(i["price_checked_at"].endswith("+00:00") for i in items)  # UTC, never naive local time
    assert items[0]["image"] == "https://img/0001.jpg"
    assert r.json()["failed"] == 0


def test_refresh_prices_partial_failure_keeps_old_values(client, kroger, with_store):
    # Seed via PUT so milk carries an old price with no stamp (a priced POST would stamp it).
    client.put("/api/lists/standard/items", json=[{**MILK, **FULL, "price": 1.0}, {**EGGS, **FULL}])
    kroger.lookups["0001"] = KrogerError(404, "not found")
    r = client.post("/api/lists/standard/prices")
    assert r.status_code == 200
    milk, eggs = r.json()["items"]
    assert milk["price"] == 1.0 and milk["price_checked_at"] is None
    assert eggs["price"] == 5.0
    assert r.json()["failed"] == 1


def test_refresh_prices_total_failure_is_502(client, kroger, with_store):
    client.post("/api/lists/standard/items", json=MILK)
    kroger.lookups["0001"] = KrogerError(503, "down")
    r = client.post("/api/lists/standard/prices")
    assert r.status_code == 502
    assert "down" in r.json()["error"]


def test_refresh_prices_on_empty_list_is_noop(client, with_store, kroger):
    r = client.post("/api/lists/standard/prices")
    assert r.status_code == 200
    assert kroger.lookup_calls == []


def test_refresh_prices_merges_into_latest_list_state(client, kroger, storage, with_store):
    # The lookups are slow; the user may edit the list meanwhile. The refresh must not revert those edits.
    client.post("/api/lists/standard/items", json=MILK)
    client.post("/api/lists/standard/items", json=EGGS)

    def lookup(upc, location_id):
        if upc == "0001":  # mid-refresh: user unchecks milk and removes eggs
            storage.save_list("standard", "Standard", [Item(**MILK, included=False)])
        return product(upc, f"Looked up {upc}", price=5.0, promo=4.0)
    kroger.lookup_product = lookup

    r = client.post("/api/lists/standard/prices")
    assert r.status_code == 200
    [milk] = r.json()["items"]
    assert milk["included"] is False
    assert milk["price"] == 5.0 and milk["price_checked_at"]
    assert r.json()["failed"] == 0


def test_refresh_prices_stops_after_three_transport_failures(client, kroger, with_store):
    for n in range(6):
        client.post("/api/lists/standard/items", json={"upc": f"000{n}", "name": f"Item {n}"})
        kroger.lookups[f"000{n}"] = KrogerError(0, "Could not reach Kroger: ConnectError")
    r = client.post("/api/lists/standard/prices")
    assert r.status_code == 502
    assert "Could not reach Kroger" in r.json()["error"]
    assert len(kroger.lookup_calls) == 3  # transport failures aren't item-specific; stop hammering


# -- import ------------------------------------------------------------------------------

def test_import_parse_returns_lines(client):
    r = client.post("/api/import/parse", json={"text": "- 2x milk\n- eggs"})
    assert r.json() == {"lines": [
        {"raw": "- 2x milk", "name": "milk", "quantity": 2, "exact": None},
        {"raw": "- eggs", "name": "eggs", "quantity": 1, "exact": None},
    ]}


def test_import_parse_exported_csv_returns_exact_rows(client):
    text = "upc,name,size,quantity,included,price,promo,image\n0001,Whole Milk,1 gal,2,false,3.49,,\n"
    (line,) = client.post("/api/import/parse", json={"text": text}).json()["lines"]
    assert line["name"] == "Whole Milk" and line["quantity"] == 2
    assert line["exact"] == {"upc": "0001", "size": "1 gal", "included": False, "price": 3.49, "promo": None, "image": None}


def test_import_commit_honours_included_and_prices_for_new_items(client):
    body = [{"upc": "0002", "name": "Eggs", "size": "12 ct", "image": None, "quantity": 1,
             "included": False, "price": 4.0, "promo": 3.5}]
    items = client.post("/api/lists/standard/import", json=body).json()["items"]
    assert [(i["upc"], i["included"], i["price"], i["promo"], i["price_checked_at"]) for i in items] == \
        [("0002", False, 4.0, 3.5, None)]


# -- export ------------------------------------------------------------------------------

def test_export_csv_round_trips_through_import(client, storage):
    storage.save_list("standard", "Standard", [
        Item(upc="0001", name="Whole Milk, Vitamin D", size="1 gal", quantity=2, included=True,
             image="https://img/1.jpg", price=3.49, promo=2.99, price_checked_at="2026-10-26T00:00:00+00:00"),
        Item(upc="0002", name="Eggs", size="", quantity=1, included=False),
    ])
    r = client.get("/api/lists/standard/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="standard.csv"'
    text = r.content.decode("utf-8-sig")
    assert text.splitlines() == [
        "upc,name,size,quantity,included,price,promo,image",
        '0001,"Whole Milk, Vitamin D",1 gal,2,true,3.49,2.99,https://img/1.jpg',
        "0002,Eggs,,1,false,,,",
    ]
    # The exported file re-imports into a fresh list exactly, no search needed.
    client.post("/api/lists", json={"name": "Copy"})
    lines = client.post("/api/import/parse", json={"text": r.content.decode("utf-8")}).json()["lines"]
    picks = [{**l["exact"], "name": l["name"], "quantity": l["quantity"]} for l in lines]
    items = client.post("/api/lists/copy/import", json=picks).json()["items"]
    assert [(i["upc"], i["name"], i["size"], i["quantity"], i["included"], i["price"], i["promo"], i["image"])
            for i in items] == [
        ("0001", "Whole Milk, Vitamin D", "1 gal", 2, True, 3.49, 2.99, "https://img/1.jpg"),
        ("0002", "Eggs", "", 1, False, None, None, None),
    ]


def test_export_missing_list_is_404(client):
    assert client.get("/api/lists/nope/export.csv").status_code == 404


def test_export_empty_list_is_header_only(client):
    text = client.get("/api/lists/standard/export.csv").content.decode("utf-8-sig")
    assert text.splitlines() == ["upc,name,size,quantity,included,price,promo,image"]


def test_import_parse_empty_is_400(client):
    assert client.post("/api/import/parse", json={"text": "  "}).status_code == 400


def test_import_resolve_requires_store(client):
    assert client.post("/api/import/resolve", json={"names": ["milk"]}).status_code == 400


def test_import_resolve_returns_up_to_three_candidates_per_name(client, with_store, kroger):
    def boom(term, location_id):
        if term == "unobtainium":
            raise KrogerError(500, "oops")
        return [product(str(i), f"{term} {i}") for i in range(5)]
    kroger.search_products = boom
    r = client.post("/api/import/resolve", json={"names": ["milk", "unobtainium"]})
    matches = r.json()["matches"]
    assert matches[0]["name"] == "milk" and len(matches[0]["candidates"]) == 3
    assert matches[1] == {"name": "unobtainium", "candidates": []}


def test_import_resolve_total_failure_is_502(client, with_store, kroger):
    def boom(term, location_id):
        raise KrogerError(0, "Could not reach Kroger: ConnectError")
    kroger.search_products = boom
    r = client.post("/api/import/resolve", json={"names": ["milk", "eggs"]})
    assert r.status_code == 502
    assert "Could not reach Kroger" in r.json()["error"]


def test_import_commit_merges_and_bumps_quantities(client):
    client.post("/api/lists/standard/items", json={**MILK, "quantity": 1})
    body = [
        {"upc": "0001", "name": "Whole Milk", "size": "1 gal", "image": "https://img/1.jpg", "quantity": 2},
        {"upc": "0002", "name": "Eggs", "size": "12 ct", "image": None, "quantity": 3},
    ]
    r = client.post("/api/lists/standard/import", json=body)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [(i["upc"], i["quantity"], i["included"]) for i in items] == [("0001", 3, True), ("0002", 3, True)]
    assert items[1]["image"] is None
