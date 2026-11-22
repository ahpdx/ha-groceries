import json
import re

import pytest
from starlette.testclient import TestClient

from fakes import FakeKroger
from grocer import history, runs
from grocer.config import Config
from grocer.engine import resolve
from grocer.store import Store
from grocer.web import build_app
from test_engine_runs import ALL, LOC, OATLY, PLANET, STORE_BRAND


def payload(trips):
    return json.dumps({"format": "grocer-history", "version": 1, "trips": trips})


def trip(tid, date, *items):
    return {"id": tid, "date": date, "kind": "pickup", "items": [{"upc": u, "name": n, "qty": 1} for u, n in items]}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "g.db")
    s.set_setting("location_id", LOC)
    return s


def test_parse_validates_and_normalizes():
    trips = history.parse(payload([
        {"id": "t1", "date": "2026-10-01", "kind": "weird",
         "items": [{"upc": "/p/planet-oat/0004410015618", "name": "Planet  Oat\nOat Milk"}, {"upc": "abc"}, {"upc": "00044100156182"}]},
        {"id": "t2", "date": "Oct 1", "items": [{"upc": "1"}]},
    ]))
    assert len(trips) == 1 and trips[0]["kind"] is None
    assert [i["upc"] for i in trips[0]["items"]] == ["0004410015618"]   # 14-digit dropped its check digit; dupes merged
    assert trips[0]["items"][0]["name"] == "Planet Oat Oat Milk"
    for bad in ["nope", json.dumps({"format": "other"}), payload([])]:
        with pytest.raises(history.ImportError_):
            history.parse(bad)


def test_import_is_idempotent(store):
    t = history.parse(payload([trip("t1", "2026-10-01", (PLANET.upc, "Planet Oat Oat Milk"))]))
    assert store.import_trips(t) == {"added": 1, "skipped": 0, "lines": 1}
    assert store.import_trips(t)["skipped"] == 1
    assert store.memory()["purchases"][PLANET.upc]["times"] == 1


def test_bought_product_is_preselected_as_confirm(store):
    store.import_trips(history.parse(payload([
        trip("t1", "2026-10-01", (STORE_BRAND.upc, "Simple Truth Original Oatmilk")),
        trip("t2", "2026-10-15", (STORE_BRAND.upc, "Simple Truth Original Oatmilk")),
        trip("t3", "2026-11-01", (STORE_BRAND.upc, "Simple Truth Original Oatmilk")),
    ])))
    r = resolve(FakeKroger(ALL), store.memory(), key="oat milk", text="oat milk", note="", diets=[], location_id=LOC)
    assert r.decision == "confirm" and r.chosen_upc == STORE_BRAND.upc and "bought this 3×" in r.reason


def test_purchase_found_even_if_search_misses_it(store):
    # Search for "oat milk" won't match "Oatly Original Oatmilk" in the fake; history still brings it in.
    hidden = FakeKroger([p for p in ALL if p.upc != OATLY.upc])
    hidden.product = lambda upc, loc: OATLY if upc == OATLY.upc else None
    store.import_trips(history.parse(payload([trip("t1", "2026-10-01", (OATLY.upc, "Oatly Oat Milk Original"))])))
    r = resolve(hidden, store.memory(), key="oat milk", text="oat milk", note="", diets=[], location_id=LOC)
    assert OATLY.upc in [c["upc"] for c in r.candidates]


def test_confirming_a_regular_purchase_makes_it_usual(store):
    store.import_trips(history.parse(payload([trip(f"t{i}", f"2026-10-0{i}", (STORE_BRAND.upc, "Simple Truth Oatmilk"))
                                              for i in range(1, 5)])))
    client = FakeKroger(ALL)
    rid = runs.start_run(store, client, "oat milk", background=False)
    runs.accept_confirms(store, rid)
    runs.submit(store, client, rid, version=store.run(rid)["version"])
    c = next(c for c in store.memory()["choices"] if c["key"] == "oat milk")
    assert c["upc"] == STORE_BRAND.upc and c["times_chosen"] == 3
    rid = runs.start_run(store, client, "oat milk", background=False)
    assert store.lines(rid)[0]["decision"] == "auto"


def test_upload_through_page(tmp_path):
    cfg = Config("id", "secret", LOC, tmp_path, "http://localhost:8000/callback", None, "127.0.0.1", 0)
    store = Store(tmp_path / "g.db")
    c = TestClient(build_app(cfg, store, FakeKroger(ALL)))
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/saved?tab=history").text).group(1)
    body = payload([trip("t1", "2026-10-01", (PLANET.upc, "Planet Oat Oat Milk"))])
    page = c.post("/import", data={"csrf": tok}, files={"file": ("h.json", body, "application/json")})
    assert "Imported 1 trip(s), 1 item(s)." in page.text and "Most bought" in page.text
    page = c.post("/import", data={"csrf": tok, "text": body})
    assert "already imported" in page.text
    page = c.post("/import/clear", data={"csrf": tok})
    assert store.purchase_summary()["trips"] == 0


def test_accepts_bookmarklet_output_verbatim():
    # Exactly what the collect bookmarklet produced on a page built from a real trip page's markup.
    raw = ('{"format":"grocer-history","version":1,"trips":[{"id":"2026-09-28_100_00001_0001_000001",'
           '"date":"2026-09-28","kind":"delivery","store":"12345678","items":[{"upc":"0086047900152",'
           '"name":"Catalina Crunch Cinnamon Toast Protein Cereal","size":"9 oz","qty":2},'
           '{"upc":"0000000004011","name":"Fresh Bunch of Bananas – 5-7 Bananas","size":"1 lb","qty":2.13}]}]}')
    (t,) = history.parse(raw.encode())
    assert t["kind"] == "delivery" and [i["upc"] for i in t["items"]] == ["0086047900152", "0000000004011"]
    assert t["items"][1]["qty"] == 2.13
