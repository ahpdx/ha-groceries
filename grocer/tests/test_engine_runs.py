from datetime import datetime, timedelta, timezone

import pytest

from fakes import FakeKroger, prod
from grocer import runs
from grocer.engine import resolve
from grocer.store import Store

LOC = "12345678"
OATLY = prod("0019064664101", "Oatly Original Oatmilk", brand="Oatly", size="64 fl oz", price=6.29,
             ingredients="oat base (water, oats), rapeseed oil", declarations=["Vegan"])
PLANET = prod("0004410015618", "Planet Oat Original Oat Milk", brand="Planet Oat", size="52 fl oz", price=3.69,
              promo=2.99, ingredients="oatmilk (water, oats)", declarations=["Vegan"])
STORE_BRAND = prod("0001111003850", "Simple Truth Original Oatmilk", brand="Simple Truth", size="32 oz", price=1.99,
                   ingredients="oatmilk")
DAIRY = prod("0001111000001", "Kroger 2% Milk", brand="Kroger", size="1 gal", price=3.49,
             allergens=[("Contains", "Milk and its derivates")], ingredients="milk, vitamin a")
BANANAS = prod("0000000004011", "Fresh Bananas", size="1 lb", price=0.59, stock=None, sold_by="WEIGHT",
               categories=["Produce"])
SOAP = prod("0003077201158", "Dawn Ultra Dish Soap", brand="Dawn", size="38 fl oz", price=5.99)
SOAP_GONE = prod("0003077299999", "Old Dish Soap", size="20 fl oz", price=1.0, available=False)
ALL = [OATLY, PLANET, STORE_BRAND, DAIRY, BANANAS, SOAP, SOAP_GONE]


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "g.db")
    s.set_setting("location_id", LOC)
    return s


def mem(store):
    return store.memory()


def test_new_item_asks_with_top3(store):
    r = resolve(FakeKroger(ALL), mem(store), key="oat milk", text="oat milk", note="", diets=[], location_id=LOC)
    assert r.decision == "ask" and len(r.candidates) == 3


def test_usual_auto_and_cheaper_note(store):
    store.cache_products([OATLY])
    for _ in range(3):
        store.record_choice("oat milk", OATLY.upc, when=ago(10))
    r = resolve(FakeKroger(ALL), mem(store), key="oat milk", text="oat milk", note="", diets=[], location_id=LOC)
    assert r.decision == "auto" and r.chosen_upc == OATLY.upc
    assert r.cheaper_upc == PLANET.upc and "on sale" in r.sale_note and "less per fl oz" in r.sale_note


def test_once_is_confirm_and_old_history_is_not_auto(store):
    store.record_choice("oat milk", OATLY.upc, when=ago(5))
    r = resolve(FakeKroger(ALL), mem(store), key="oat milk", text="", note="", diets=[], location_id=LOC)
    assert r.decision == "confirm" and r.chosen_upc == OATLY.upc
    store.record_choice("oat milk", OATLY.upc, when=ago(400))
    store.db.execute("UPDATE choices SET last_chosen_at=?", (ago(400),))
    r = resolve(FakeKroger(ALL), mem(store), key="oat milk", text="", note="", diets=[], location_id=LOC)
    assert r.decision != "auto"


def test_usual_out_of_stock_asks(store):
    oos = prod(OATLY.upc, OATLY.description, brand="Oatly", size="64 fl oz", price=6.29, stock="TEMPORARILY_OUT_OF_STOCK",
               declarations=["Vegan"], ingredients="oats")
    for _ in range(2):
        store.record_choice("oat milk", OATLY.upc)
    r = resolve(FakeKroger([oos, PLANET, STORE_BRAND]), mem(store), key="oat milk", text="", note="", diets=[], location_id=LOC)
    assert r.decision == "ask" and "out of stock" in r.reason
    assert r.candidates[0]["upc"] == OATLY.upc and r.chosen_upc != OATLY.upc


def test_unknown_stock_still_auto(store):
    for _ in range(2):
        store.record_choice("banana", BANANAS.upc)
    r = resolve(FakeKroger(ALL), mem(store), key="banana", text="", note="", diets=[], location_id=LOC)
    assert r.decision == "auto" and "stock unknown" in r.reason


def test_diet_filters_and_unknown_asks(store):
    r = resolve(FakeKroger(ALL), mem(store), key="milk", text="", note="", diets=["vegan"], location_id=LOC)
    assert DAIRY.upc not in [c["upc"] for c in r.candidates]
    for _ in range(3):
        store.record_choice("oat milk", STORE_BRAND.upc)
    r = resolve(FakeKroger(ALL), mem(store), key="oat milk", text="", note="", diets=["vegan"], location_id=LOC)
    # Simple Truth has ingredients but no claim -> likely; still auto
    assert r.decision == "auto"
    no_ing = prod(STORE_BRAND.upc, STORE_BRAND.description, size="32 oz", price=1.99)
    r = resolve(FakeKroger([no_ing, PLANET]), mem(store), key="oat milk", text="", note="", diets=["vegan"], location_id=LOC)
    assert r.decision == "ask" and "no ingredient info" in r.reason


def test_rejected_and_unavailable_dropped(store):
    store.reject("dish soap", SOAP.upc)
    r = resolve(FakeKroger(ALL), mem(store), key="dish soap", text="", note="", diets=[], location_id=LOC)
    assert r.decision == "not_found"


def test_pinned_wins(store):
    store.set_pin("oat milk", STORE_BRAND.upc, True)
    r = resolve(FakeKroger(ALL), mem(store), key="oat milk", text="", note="", diets=[], location_id=LOC)
    assert r.decision == "auto" and r.chosen_upc == STORE_BRAND.upc and r.reason == "pinned"


# ------------------------------------------------------------------ runs

def _run(store, client, text):
    rid = runs.start_run(store, client, text, background=False)
    assert store.run(rid)["status"] == "review"
    return rid


def test_run_lifecycle_and_no_double_send(store):
    client = FakeKroger(ALL)
    for _ in range(2):
        store.record_choice("banana", BANANAS.upc)
    rid = _run(store, client, "bananas\noat milk\ndish soap")
    lines = {l["key"]: l for l in store.lines(rid)}
    assert lines["banana"]["decision"] == "auto"
    v = store.run(rid)["version"]
    with pytest.raises(runs.RunError, match="need a decision"):
        runs.submit(store, client, rid, version=v)
    runs.decide(store, rid, lines["oat milk"]["line_no"], action="choose", upc=PLANET.upc, pin=False)
    runs.decide(store, rid, lines["dish soap"]["line_no"], action="skip")
    res = runs.submit(store, client, rid, version=v)
    assert res["status"] == "submitted" and len(client.cart_calls) == 1
    assert {i["upc"] for i in client.cart_calls[0]} == {BANANAS.upc, PLANET.upc}
    with pytest.raises(runs.RunError):
        runs.submit(store, client, rid, version=store.run(rid)["version"])
    assert len(client.cart_calls) == 1
    # the pick was learned
    assert any(c["key"] == "oat milk" and c["upc"] == PLANET.upc for c in store.memory()["choices"])


def test_one_open_run(store):
    client = FakeKroger(ALL)
    _run(store, client, "bananas")
    with pytest.raises(runs.RunError, match="already open"):
        runs.start_run(store, client, "soap", background=False)


def test_unknown_outcome_is_partial_and_not_retried(store):
    client = FakeKroger(ALL, cart_outcome="unknown")
    store.set_pin("banana", BANANAS.upc, True)
    rid = _run(store, client, "bananas")
    res = runs.submit(store, client, rid, version=store.run(rid)["version"])
    assert res["status"] == "partial"
    assert store.lines(rid)[0]["outcome"] == "unknown"
    assert store.open_run() is None


def test_refused_add_returns_to_review(store):
    client = FakeKroger(ALL, cart_outcome="failed")
    store.set_pin("banana", BANANAS.upc, True)
    rid = _run(store, client, "bananas")
    res = runs.submit(store, client, rid, version=store.run(rid)["version"])
    assert res["status"] == "review" and "Nothing was added" in store.run(rid)["message"]
    client.cart_outcome = "ok"
    assert runs.submit(store, client, rid, version=store.run(rid)["version"])["status"] == "submitted"


def test_stale_run_rechecks_first(store):
    client = FakeKroger(ALL)
    store.set_pin("banana", BANANAS.upc, True)
    rid = _run(store, client, "bananas")
    store.db.execute("UPDATE runs SET resolved_at=? WHERE run_id=?", (ago(1), rid))
    res = runs.submit(store, client, rid, version=store.run(rid)["version"])
    assert res["rechecked"] and client.cart_calls == []
    assert runs.submit(store, client, rid, version=store.run(rid)["version"])["status"] == "submitted"


def test_search_instead_learns_alias(store):
    client = FakeKroger(ALL)
    rid = _run(store, client, "oat milk")
    runs.search_line(store, client, rid, 0, "oatly")
    line = store.line(rid, 0)
    assert OATLY.upc in [c["upc"] for c in line["candidates"]]
    assert store.memory()["aliases"] == {"oatly": "oat milk"}
