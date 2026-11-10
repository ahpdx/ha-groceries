import re

import pytest
from starlette.testclient import TestClient

from fakes import FakeKroger
from grocer.config import Config
from grocer.store import Store
from grocer.web import build_app
from test_engine_runs import ALL, BANANAS, PLANET


@pytest.fixture
def app(tmp_path):
    cfg = Config("id", "secret", "12345678", tmp_path, "http://localhost:8000/callback", None, "127.0.0.1", 0)
    store = Store(tmp_path / "g.db")
    kroger = FakeKroger(ALL)
    kroger.sign_in_status = lambda: {"signed_in": True}
    return TestClient(build_app(cfg, store, kroger), follow_redirects=True), store, kroger


def csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_full_flow_under_ingress_prefix(app):
    c, store, kroger = app
    h = {"X-Ingress-Path": "/api/hassio_ingress/abc"}
    page = c.get("/", headers=h)
    assert page.status_code == 200 and 'action="/api/hassio_ingress/abc/runs"' in page.text
    tok = csrf(page.text)
    prev = c.post("/draft", data={"csrf": tok, "text": "2 oat milk\nbananas"}, headers=h)
    assert "2 items" in prev.text
    store.set_pin("banana", BANANAS.upc, True)
    r = c.post("/runs", data={"csrf": tok, "text": "2 oat milk\nbananas"}, headers=h, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/api/hassio_ingress/abc/runs/")
    run_id = r.headers["location"].rsplit("/", 1)[1]
    import time
    for _ in range(50):
        if store.run(run_id)["status"] == "review":
            break
        time.sleep(0.05)
    page = c.get(f"/runs/{run_id}", headers=h)
    assert "pick one" in page.text and "Add 1 to cart" in page.text
    h = {}  # HA strips the prefix before forwarding, so follow-up requests go to bare paths
    oat = next(l for l in store.lines(run_id) if l["key"] == "oat milk")
    c.post(f"/runs/{run_id}/line/{oat['line_no']}", data={"csrf": tok, "action": "choose", "upc": PLANET.upc, "quantity": "2"}, headers=h)
    page = c.get(f"/runs/{run_id}", headers=h)
    assert "Add 2 to cart" in page.text
    v = store.run(run_id)["version"]
    page = c.post(f"/runs/{run_id}/submit", data={"csrf": tok, "version": str(v)}, headers=h)
    assert "Added 2 item(s)" in page.text
    assert {(i["upc"], i["quantity"]) for i in kroger.cart_calls[0]} == {(BANANAS.upc, 1), (PLANET.upc, 2)}
    for path in ("/saved", "/saved?tab=rejected", "/saved?tab=diet", "/saved?tab=aliases", "/history", "/settings"):
        assert c.get(path, headers=h).status_code == 200, path


def test_bad_csrf_refused(app):
    c, store, kroger = app
    r = c.post("/runs", data={"csrf": "nope", "text": "milk"})
    assert r.status_code == 400 and store.open_run() is None


def test_trusted_proxy_guard(tmp_path):
    cfg = Config("id", "secret", "12345678", tmp_path, "http://localhost:8000/callback", "172.30.32.2", "127.0.0.1", 0)
    c = TestClient(build_app(cfg, Store(tmp_path / "g.db"), FakeKroger(ALL)))
    assert c.get("/").status_code == 403
    assert c.get("/healthz").status_code == 200


def test_sign_in_paste_flow(tmp_path):
    cfg = Config("id", "secret", "12345678", tmp_path, "http://localhost:8000/callback", None, "127.0.0.1", 0)
    store = Store(tmp_path / "g.db")
    from grocer.kroger import KrogerClient
    kc = KrogerClient("id", "secret", tmp_path / "tok.json", cfg.redirect_uri)
    got = {}
    kc.exchange_code = lambda code, verifier: got.update(code=code, verifier=verifier)
    c = TestClient(build_app(cfg, store, kc), follow_redirects=False)
    tok = csrf(c.get("/settings").text)
    r = c.post("/auth/start", data={"csrf": tok})
    assert r.status_code == 303 and r.headers["location"].startswith("https://api.kroger.com/v1/connect/oauth2/authorize?")
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    r = c.post("/auth/finish", data={"csrf": tok, "url": "http://localhost:8000/callback?code=X&state=wrong"})
    assert "doesn" in r.headers["location"] and not got
    r = c.post("/auth/finish", data={"csrf": tok, "url": f"http://localhost:8000/callback?code=ABC&state={state}"})
    assert got["code"] == "ABC" and "Signed" in r.headers["location"]
