"""Runs: trigger -> resolving -> review -> submitting -> submitted | partial; or abandoned."""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import engine
from .kroger import KrogerClient, KrogerError, NotSignedIn
from .store import Store, now
from .text import normalize_key, parse_list

STALE_AFTER_HOURS = 2
WORKERS = 6


class RunError(Exception):
    pass


def fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode()).hexdigest()[:16]


def start_run(store: Store, client: KrogerClient, text: str, *, source: str = "text",
              background: bool = True) -> str:
    if store.open_run():
        raise RunError("A run is already open. Resume it or abandon it first.")
    parsed = parse_list(text)
    if not parsed.items:
        raise RunError("The list is empty.")
    aliases = store.memory()["aliases"]
    location = store.setting("location_id")
    modality = store.setting("modality", "PICKUP")
    run_id = store.new_run_id()
    with store.tx() as c:
        c.execute("INSERT INTO runs(run_id,created_at,source,raw_text,source_fingerprint,location_id,modality,"
                  "list_diet,status) VALUES(?,?,?,?,?,?,?,?, 'resolving')",
                  (run_id, now(), source, text, fingerprint(text), location, modality, json.dumps(parsed.list_diet)))
        for i, it in enumerate(parsed.items):
            diets = list(dict.fromkeys(parsed.list_diet + it.diet))
            c.execute("INSERT INTO run_lines(run_id,line_no,raw,text,key,quantity,diet,note,decision) "
                      "VALUES(?,?,?,?,?,?,?,?, 'pending')",
                      (run_id, i, it.raw, it.text, normalize_key(it.text, aliases), it.quantity, json.dumps(diets), it.note))
    if background:
        threading.Thread(target=_resolve_all, args=(store, client, run_id), daemon=True).start()
    else:
        _resolve_all(store, client, run_id)
    return run_id


def _resolve_line(store: Store, client: KrogerClient, memory: dict, location: str, line: dict) -> None:
    try:
        r = engine.resolve(client, memory, key=line["key"], text=line["text"] or line["key"], note=line["note"] or "",
                           diets=line["diet"], location_id=location, size_hint=line["note"] or None)
        store.cache_products(r.seen)
        store.update_line(line["run_id"], line["line_no"], decision=r.decision, reason=r.reason,
                          candidates=r.candidates, chosen_upc=r.chosen_upc, sale_note=r.sale_note,
                          cheaper_upc=r.cheaper_upc, decided_by="auto" if r.decision == "auto" else None)
    except Exception as e:  # one bad line must not sink the run
        store.update_line(line["run_id"], line["line_no"], decision="not_found",
                          reason=f"lookup failed: {e.__class__.__name__}", candidates=[])


def _resolve_all(store: Store, client: KrogerClient, run_id: str) -> None:
    run = store.run(run_id)
    memory = store.memory()
    lines = store.lines(run_id)
    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(lambda l: _resolve_line(store, client, memory, run["location_id"], l), lines))
    store.transition(run_id, ("resolving",), "review", resolved_at=now())


def abandon(store: Store, run_id: str) -> None:
    store.transition(run_id, ("resolving", "review"), "abandoned")


def _require_review(store: Store, run_id: str) -> dict:
    run = store.run(run_id)
    if not run:
        raise RunError("No such run.")
    if run["status"] != "review":
        raise RunError(f"This run is {run['status']}; it can't be changed.")
    return run


def decide(store: Store, run_id: str, line_no: int, *, action: str, upc: str | None = None,
           quantity: int | None = None, pin: bool = False, reject_upc: str | None = None,
           reject_scope: str = "item", diet_override: tuple[str, str, str] | None = None) -> None:
    _require_review(store, run_id)
    line = store.line(run_id, line_no)
    if not line:
        raise RunError("No such line.")
    if reject_upc:
        store.reject(line["key"] if reject_scope == "item" else "", reject_upc)
        if line["chosen_upc"] == reject_upc:
            store.update_line(run_id, line_no, chosen_upc=None, decision="ask", reason="you rejected the pick")
    if diet_override:
        store.set_override(*diet_override)
    if action == "choose":
        if not upc or upc not in {c["upc"] for c in line["candidates"]}:
            raise RunError("Pick one of the shown products.")
        store.update_line(run_id, line_no, chosen_upc=upc, decision="chosen", decided_by="page", decided_at=now())
        if pin:
            store.set_pin(line["key"], upc, True)
    elif action == "skip":
        store.update_line(run_id, line_no, decision="skipped", decided_by="page", decided_at=now())
    elif action == "unskip":
        store.update_line(run_id, line_no, decision="ask" if not line["chosen_upc"] else "chosen", decided_by="page")
    if quantity is not None:
        store.update_line(run_id, line_no, quantity=max(1, min(int(quantity), 99)))


def search_line(store: Store, client: KrogerClient, run_id: str, line_no: int, query: str) -> None:
    run = _require_review(store, run_id)
    line = store.line(run_id, line_no)
    memory = store.memory()
    products = [p for p in client.search(query, run["location_id"], limit=10) if p.available]
    store.cache_products(products)
    by_upc = {c["upc"]: c for c in memory["choices"] if c["key"] == line["key"]}
    cands = [engine.candidate_dict(p, diets=line["diet"], overrides=memory["overrides"], choice=by_upc.get(p.upc))
             for p in products]
    keep = [c for c in line["candidates"] if c["upc"] == line["chosen_upc"]]
    store.update_line(run_id, line_no, candidates=keep + [c for c in cands if c["upc"] != line["chosen_upc"]],
                      reason=f"search results for “{query}”" if cands else f"nothing found for “{query}”")
    # Learn the rewording: searching "oatly" for key "oat milk" links them.
    q = normalize_key(query)
    if q and q != line["key"]:
        store.set_alias(q, line["key"])


def blockers(lines: list[dict]) -> list[dict]:
    return [l for l in lines if l["decision"] in ("ask", "not_found", "pending", "confirm") and l["outcome"] == "pending"]


def accept_confirms(store: Store, run_id: str) -> None:
    """'Accept all suggestions': confirm rows and ask rows that have a preselected pick."""
    _require_review(store, run_id)
    for l in store.lines(run_id):
        if l["decision"] in ("confirm", "ask") and l["chosen_upc"]:
            store.update_line(run_id, l["line_no"], decision="chosen", decided_by="page", decided_at=now())


def _hours_since(ts: str | None) -> float:
    if not ts:
        return 0
    return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600


def submit(store: Store, client: KrogerClient, run_id: str, *, version: int) -> dict:
    run = _require_review(store, run_id)
    if run["version"] != version:
        raise RunError("This run changed since you loaded it. Review it again.")
    lines = store.lines(run_id)
    if blockers(lines):
        raise RunError("Some lines still need a decision (pick or skip).")
    to_send = [l for l in lines if l["decision"] in ("auto", "chosen") and l["chosen_upc"] and l["outcome"] == "pending"]
    if not to_send:
        raise RunError("Nothing to add.")

    if not store.transition(run_id, ("review",), "submitting"):
        raise RunError("This run is already being submitted.")

    # Stale check: re-read price/stock for picks if the run sat in review a while.
    if _hours_since(run["resolved_at"]) > STALE_AFTER_HOURS:
        changed = []
        for l in to_send:
            try:
                p = client.product(l["chosen_upc"], run["location_id"])
            except KrogerError:
                p = None
            old = next((c for c in l["candidates"] if c["upc"] == l["chosen_upc"]), {})
            old_price = old.get("promo") if old.get("promo") and old.get("price") and old["promo"] < old["price"] \
                else old.get("price")
            if not p or not p.available or p.stock == "TEMPORARILY_OUT_OF_STOCK" or p.effective_price != old_price:
                changed.append(l["line_no"])
                if p:
                    store.update_line(run_id, l["line_no"], reason="price or stock changed since review")
        store.transition(run_id, ("submitting",), "review", resolved_at=now(),
                         message=(f"Prices or stock changed for {len(changed)} item(s); check them and add again."
                                  if changed else "Prices re-checked; nothing changed. Add again to send."))
        return {"status": "review", "rechecked": True, "changed": changed}

    items = [{"upc": l["chosen_upc"], "quantity": l["quantity"], "modality": run["modality"] or "PICKUP"} for l in to_send]
    for l in to_send:
        store.update_line(run_id, l["line_no"], sent_at=now())
    try:
        result = client.cart_add(items)
    except NotSignedIn:
        store.transition(run_id, ("submitting",), "review", message="Sign in to Kroger first (Settings).")
        for l in to_send:
            store.update_line(run_id, l["line_no"], sent_at=None)
        raise RunError("Sign in to Kroger first (Settings).")

    if result.outcome == "failed":
        # A refused request added nothing, so it's safe to fix and send again.
        for l in to_send:
            store.update_line(run_id, l["line_no"], sent_at=None, outcome_detail=result.detail or None)
        msg = f"Kroger refused the add (HTTP {result.status}): {result.detail or 'no detail'}. Nothing was added."
        store.transition(run_id, ("submitting",), "review", message=msg)
        return {"status": "review", "outcome": "failed", "sent": 0, "message": msg}

    memory = store.memory()
    known = {(c["key"], c["upc"]) for c in memory["choices"] if c["times_chosen"]}
    for l in to_send:
        store.update_line(run_id, l["line_no"], outcome=result.outcome, outcome_detail=result.detail or None)
        if result.outcome in ("ok", "unknown"):
            # First time this product is used for this wording: if store history shows you buy it
            # regularly, start it as a usual (auto next time) rather than from one pick.
            bought = memory["purchases"].get(l["chosen_upc"], {}).get("times", 0)
            seed = min(3, bought) if (l["key"], l["chosen_upc"]) not in known and bought >= 2 else 1
            store.record_choice(l["key"], l["chosen_upc"], times=seed)
    for l in lines:
        if l["decision"] == "skipped":
            store.update_line(run_id, l["line_no"], outcome="skipped")
    final = "submitted" if result.outcome == "ok" else "partial"
    msg = {"ok": f"Added {len(to_send)} item(s) to your cart.",
           "unknown": "Kroger didn't confirm the add. Check your cart in the app before adding anything again."}[result.outcome]
    store.transition(run_id, ("submitting",), final, submitted_at=now(), message=msg)
    return {"status": final, "outcome": result.outcome, "sent": len(to_send)}
