"""The page: Starlette + Jinja2, plain HTML forms, works under HA Ingress."""

from __future__ import annotations

import hmac
import json
import time
import urllib.parse
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from . import history as historymod
from . import runs
from .config import Config
from .kroger import KrogerClient, KrogerError
from .store import Store
from .text import DIET_LABELS, parse_list

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"


def bookmarklet(name: str) -> str:
    """A javascript: URL built from static/<name> (comment lines dropped, everything else encoded)."""
    code = "\n".join(l for l in (STATIC / name).read_text().splitlines() if not l.strip().startswith("//"))
    return "javascript:" + urllib.parse.quote(code, safe="(){}[];,:=>!'*.+-/$?&|\"<>`@~^ ")
AUTH_TTL = 15 * 60


def money(v):
    return "" if v is None else f"${v:,.2f}"


def local_time(iso: str | None) -> str:
    """Stored times are UTC; show them in the add-on's local time zone (HA passes TZ)."""
    from datetime import datetime
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return iso
    return t.strftime("%a %b %-d, %-I:%M %p")


def build_app(config: Config, store: Store | None = None, client: KrogerClient | None = None) -> Starlette:
    store = store or Store(config.data_dir / "grocer.db")
    client = client or KrogerClient(config.client_id, config.client_secret, config.data_dir / "kroger_tokens.json",
                                    config.redirect_uri)
    if not store.setting("location_id") and config.location_id:
        store.set_setting("location_id", config.location_id)
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    env.filters["money"] = money
    env.filters["ts"] = lambda v: time.strftime("%Y-%m-%d", time.localtime(float(v))) if v else ""
    env.filters["when"] = local_time
    env.globals["diet_labels"] = DIET_LABELS

    def base(request: Request) -> str:
        return (request.headers.get("x-ingress-path") or "").rstrip("/")

    def render(request: Request, name: str, http_status: int = 200, **ctx) -> HTMLResponse:
        open_run = store.open_run()
        html = env.get_template(name).render(
            base=base(request), csrf=store.form_secret(), open_run=open_run, path=request.url.path,
            signed_in=client.sign_in_status()["signed_in"], configured=client.configured, **ctx)
        return HTMLResponse(html, status_code=http_status)

    def go(request: Request, path: str, msg: str | None = None) -> RedirectResponse:
        url = base(request) + path
        if msg:
            sep = "&" if "?" in url else "?"
            frag = ""
            if "#" in url:
                url, frag = url.split("#", 1)
                frag = "#" + frag
            url = f"{url}{sep}msg={urllib.parse.quote(msg)}{frag}"
        return RedirectResponse(url, status_code=303)

    async def form(request: Request):
        f = await request.form()
        if not hmac.compare_digest(str(f.get("csrf", "")), store.form_secret()):
            raise PermissionError("bad form token")
        return f

    # ------------------------------------------------------------- list
    async def list_page(request: Request):
        text = store.draft()
        return render(request, "list.html", text=text, parsed=parse_list(text), msg=request.query_params.get("msg"))

    async def draft(request: Request):
        f = await form(request)
        text = str(f.get("text", ""))
        store.save_draft(text)
        return render(request, "_preview.html", parsed=parse_list(text))

    async def start(request: Request):
        f = await form(request)
        text = str(f.get("text", store.draft()))
        store.save_draft(text)
        if not client.configured:
            return go(request, "/settings", "Add your Kroger app keys in the add-on configuration first.")
        if not store.setting("location_id"):
            return go(request, "/settings", "Choose your store first.")
        try:
            run_id = runs.start_run(store, client, text)
        except runs.RunError as e:
            return go(request, "/", str(e))
        return go(request, f"/runs/{run_id}")

    # ------------------------------------------------------------- runs
    async def run_page(request: Request):
        run_id = request.path_params["run_id"]
        run = store.run(run_id)
        if not run:
            return PlainTextResponse("No such run", status_code=404)
        lines = store.lines(run_id)
        order = {"pending": 0, "not_found": 1, "ask": 2, "confirm": 3, "chosen": 4, "auto": 5, "skipped": 6}
        if run["status"] in ("review", "resolving"):
            lines.sort(key=lambda l: (order.get(l["decision"], 9), l["line_no"]))
        chosen = [l for l in lines if l["decision"] in ("auto", "chosen", "confirm", "ask") and l["chosen_upc"]]
        est, approx = 0.0, False
        for l in chosen:
            c = next((c for c in l["candidates"] if c["upc"] == l["chosen_upc"]), None)
            if c:
                price = c["promo"] if c.get("promo") and c.get("price") and c["promo"] < c["price"] else c.get("price")
                est += (price or 0) * l["quantity"]
                approx = approx or c.get("sold_by") == "WEIGHT"
        n_send = len([l for l in lines if l["decision"] in ("auto", "chosen") and l["chosen_upc"] and l["outcome"] == "pending"])
        stale = store.draft().strip() and runs.fingerprint(store.draft()) != run["source_fingerprint"]
        return render(request, "run.html", run=run, lines=lines, blockers=runs.blockers(lines), estimate=est,
                      approx=approx, n_send=n_send, n_suggest=len([l for l in lines if l["decision"] in ("confirm", "ask") and l["chosen_upc"]]),
                      list_changed=stale and run["status"] == "review", msg=request.query_params.get("msg"),
                      list_diet=json.loads(run["list_diet"] or "[]"))

    async def line_action(request: Request):
        run_id, n = request.path_params["run_id"], int(request.path_params["n"])
        f = await form(request)
        action = str(f.get("action", ""))
        try:
            if action == "search":
                q = str(f.get("q", "")).strip()
                if q:
                    runs.search_line(store, client, run_id, n, q)
            else:
                qty = f.get("quantity")
                override = None
                if f.get("override"):
                    o_upc, o_diet, o_status = (str(f["override"]).split("|") + ["", "", "ok"])[:3]
                    override = (o_upc, o_diet, o_status if o_status in ("ok", "fails") else "ok")
                upc = str(f.get("upc") or "") or None
                if f.get("switch_upc"):
                    action, upc = "choose", str(f["switch_upc"])
                runs.decide(store, run_id, n, action=action if action in ("choose", "skip", "unskip") else "",
                            upc=upc,
                            quantity=int(qty) if qty and str(qty).isdigit() else None,
                            pin=f.get("pin") == "1",
                            reject_upc=str(f.get("reject_upc") or "") or None,
                            reject_scope=str(f.get("reject_scope", "item")), diet_override=override)
                if override:
                    _refresh_line_diets(store, run_id, n)
        except (runs.RunError, KrogerError) as e:
            return go(request, f"/runs/{run_id}#line-{n}", str(e))
        return go(request, f"/runs/{run_id}#line-{n}")

    async def accept(request: Request):
        run_id = request.path_params["run_id"]
        await form(request)
        try:
            runs.accept_confirms(store, run_id)
        except runs.RunError as e:
            return go(request, f"/runs/{run_id}", str(e))
        return go(request, f"/runs/{run_id}")

    async def submit(request: Request):
        run_id = request.path_params["run_id"]
        f = await form(request)
        try:
            runs.submit(store, client, run_id, version=int(f.get("version", -1)))
        except runs.RunError as e:
            return go(request, f"/runs/{run_id}", str(e))
        return go(request, f"/runs/{run_id}")

    async def abandon(request: Request):
        run_id = request.path_params["run_id"]
        await form(request)
        runs.abandon(store, run_id)
        return go(request, "/", "Run abandoned.")

    async def rerun(request: Request):
        run_id = request.path_params["run_id"]
        await form(request)
        run = store.run(run_id)
        if run:
            store.save_draft(run["raw_text"])
        return go(request, "/", "List loaded from that run. Check it, then Run.")

    # ------------------------------------------------------------- saved / history
    async def saved(request: Request):
        items = store.saved_items()
        groups: dict[str, list] = {}
        for it in items:
            groups.setdefault(it["key"], []).append(it)
        return render(request, "saved.html", groups=groups, rejected=store.rejected_items(),
                      purchases=store.purchase_summary(), top_purchases=store.top_purchases(),
                      collect_href=bookmarklet("collect.js"),
                      overrides=store.override_items(), aliases=store.alias_items(),
                      tab=request.query_params.get("tab", "saved"), msg=request.query_params.get("msg"))

    async def saved_action(request: Request):
        f = await form(request)
        op, key, upc = str(f.get("op", "")), str(f.get("key", "")), str(f.get("upc", ""))
        tab = "saved"
        if op == "pin":
            store.set_pin(key, upc, True)
        elif op == "unpin":
            store.set_pin(key, upc, False)
        elif op == "forget":
            store.forget(key, upc or None)
        elif op == "unreject":
            store.unreject(key, upc)
            tab = "rejected"
        elif op == "unoverride":
            store.set_override(upc, str(f.get("diet", "")), None)
            tab = "diet"
        elif op == "alias":
            from .text import normalize_key
            store.set_alias(normalize_key(str(f.get("alias", ""))), normalize_key(str(f.get("target", ""))))
            tab = "aliases"
        elif op == "unalias":
            store.remove_alias(str(f.get("alias", "")))
            tab = "aliases"
        return go(request, f"/saved?tab={tab}")

    async def import_history(request: Request):
        f = await form(request)
        upload = f.get("file")
        raw = b""
        if upload is not None and hasattr(upload, "read"):
            raw = await upload.read(historymod.MAX_BYTES + 1)
        if not raw.strip():
            raw = str(f.get("text", "")).encode()
        if not raw.strip():
            return go(request, "/saved?tab=history", "Choose the file the bookmark downloaded, or paste its contents.")
        try:
            trips = historymod.parse(raw)
        except historymod.ImportError_ as e:
            return go(request, "/saved?tab=history", str(e))
        r = store.import_trips(trips)
        msg = f"Imported {r['added']} trip(s), {r['lines']} item(s)."
        if r["skipped"]:
            msg += f" {r['skipped']} trip(s) were already imported."
        return go(request, "/saved?tab=history", msg)

    async def clear_history(request: Request):
        await form(request)
        store.clear_purchases()
        return go(request, "/saved?tab=history", "Imported purchase history removed. Your picks are unchanged.")

    async def history(request: Request):
        return render(request, "history.html", runs=store.runs())

    # ------------------------------------------------------------- settings / sign-in
    async def settings(request: Request):
        zip_code = request.query_params.get("zip", "").strip()
        stores, error = [], None
        if zip_code:
            try:
                stores = client.locations(zip_code) if client.configured else []
            except KrogerError as e:
                error = str(e)
        return render(request, "settings.html", location_id=store.setting("location_id"),
                      location_name=store.setting("location_name"), zip=zip_code, stores=stores, error=error,
                      redirect_uri=config.redirect_uri, msg=request.query_params.get("msg"),
                      status=client.sign_in_status(), auth_url=_pending_auth_url())

    async def settings_save(request: Request):
        f = await form(request)
        loc = str(f.get("location_id", "")).strip()
        if loc.isdigit():
            store.set_setting("location_id", loc)
            store.set_setting("location_name", str(f.get("location_name", "")).strip()[:120])
        return go(request, "/settings", "Saved.")

    def _pending_auth_url() -> str | None:
        pending = json.loads(store.setting("pending_auth") or "null")
        if pending and pending.get("url") and time.time() - pending["at"] <= AUTH_TTL:
            return pending["url"]
        return None

    async def auth_start(request: Request):
        # Stays inside the add-on page. The page then offers a plain link to kroger.com that opens in a new tab:
        # opening the add-on's own (Ingress) address in a new tab fails, so only Kroger's page opens there.
        await form(request)
        if not client.configured:
            return go(request, "/settings", "Add your Kroger app keys in the add-on configuration first.")
        state, verifier, challenge = KrogerClient.new_pkce()
        store.set_setting("pending_auth", json.dumps({"state": state, "verifier": verifier, "at": time.time(),
                                                      "url": client.authorize_url(state, challenge)}))
        return go(request, "/settings#signin")

    async def auth_finish(request: Request):
        f = await form(request)
        pasted = str(f.get("url", "")).strip()
        pending = json.loads(store.setting("pending_auth") or "null")
        if not pending or time.time() - pending["at"] > AUTH_TTL:
            return go(request, "/settings", "That sign-in attempt expired. Start again.")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
        if q.get("error"):
            return go(request, "/settings", f"Kroger said: {q.get('error_description', q['error'])[0]}")
        if q.get("state", [""])[0] != pending["state"] or not q.get("code"):
            return go(request, "/settings", "That address doesn't match this sign-in attempt. Paste the whole address.")
        try:
            client.exchange_code(q["code"][0], pending["verifier"])
        except KrogerError as e:
            return go(request, "/settings", str(e))
        finally:
            store.set_setting("pending_auth", "")
        return go(request, "/settings", "Signed in to Kroger.")

    async def sign_out(request: Request):
        await form(request)
        client.sign_out()
        return go(request, "/settings", "Signed out.")

    async def health(request: Request):
        return PlainTextResponse("ok")

    class Guard(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if config.trusted_proxy and request.url.path != "/healthz":
                ip = request.client.host if request.client else ""
                if ip != config.trusted_proxy:
                    return PlainTextResponse("Forbidden", status_code=403)
            resp: Response = await call_next(request)
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["X-Content-Type-Options"] = "nosniff"
            resp.headers["Referrer-Policy"] = "no-referrer"
            return resp

    routes = [
        Route("/", list_page), Route("/draft", draft, methods=["POST"]), Route("/runs", start, methods=["POST"]),
        Route("/runs/{run_id}", run_page), Route("/runs/{run_id}/line/{n:int}", line_action, methods=["POST"]),
        Route("/runs/{run_id}/accept", accept, methods=["POST"]), Route("/runs/{run_id}/submit", submit, methods=["POST"]),
        Route("/runs/{run_id}/abandon", abandon, methods=["POST"]), Route("/runs/{run_id}/rerun", rerun, methods=["POST"]),
        Route("/saved", saved), Route("/saved", saved_action, methods=["POST"]), Route("/history", history),
        Route("/import", import_history, methods=["POST"]), Route("/import/clear", clear_history, methods=["POST"]),
        Route("/settings", settings), Route("/settings", settings_save, methods=["POST"]),
        Route("/auth/start", auth_start, methods=["POST"]), Route("/auth/finish", auth_finish, methods=["POST"]),
        Route("/auth/signout", sign_out, methods=["POST"]), Route("/healthz", health),
    ]
    async def bad_form(request, exc):
        return PlainTextResponse("This form is out of date. Go back and reload the page.", status_code=400)

    app = Starlette(routes=routes, middleware=[Middleware(Guard)], exception_handlers={PermissionError: bad_form})
    app.state.store, app.state.client = store, client
    return app


def _refresh_line_diets(store: Store, run_id: str, n: int) -> None:
    """Re-evaluate diet badges on a line after an override, using cached product data."""
    from .diet import check
    from .kroger import Product
    line = store.line(run_id, n)
    ov = store.memory()["overrides"]
    cands = []
    for c in line["candidates"]:
        info = store.product_info(c["upc"])
        if info:
            p = Product.from_cache(info)
            c["diet"] = {d: dict(zip(("status", "reasons"), check(p, d, ov.get((p.upc, d))))) for d in line["diet"]}
        cands.append(c)
    store.update_line(run_id, n, candidates=cands)
