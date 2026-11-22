"""Candidate retrieval, scoring and the auto/confirm/ask decision for one list line."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import diet as dietmod
from .kroger import KrogerClient, Product
from .text import Size, parse_size, size_similarity, tokens, unit_price

W_HISTORY, W_BRAND, W_RELEVANCE, W_SIZE, W_STOCK, W_PRICE = 0.45, 0.15, 0.20, 0.10, 0.05, 0.05
UNKNOWN_DIET_PENALTY = 0.15
HALF_LIFE_DAYS = 120
AUTO_MIN_TIMES = 2
AUTO_MAX_AGE_DAYS = 183
STOCK_SCORE = {"HIGH": 1.0, "LOW": 0.6, None: 0.8, "TEMPORARILY_OUT_OF_STOCK": 0.0}  # unknown is mostly produce
CHEAPER_MARGIN = 0.95          # suggest only when at least 5% cheaper per unit
_STOP = {"and", "the", "a", "of", "with", "for", "in", "or"}


@dataclass
class Resolution:
    decision: str                       # auto | confirm | ask | not_found
    reason: str
    chosen_upc: str | None
    candidates: list[dict]
    sale_note: str | None = None
    cheaper_upc: str | None = None
    seen: list[Product] = field(default_factory=list)


def _days_since(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - t).total_seconds() / 86400)


PURCHASE_WEIGHT = 0.7          # imported store history counts, but less than picks made for this wording
PURCHASE_CANDIDATES = 3


def _purchase_score(b: dict | None) -> float:
    if not b or not b.get("times"):
        return 0.0
    age = _days_since(b.get("last")) or 0.0
    return PURCHASE_WEIGHT * min(1.0, math.log1p(b["times"]) / math.log1p(8)) * 0.5 ** (age / HALF_LIFE_DAYS)


def _history_score(c: dict | None) -> float:
    if not c or not c.get("times_chosen"):
        return 0.0
    age = _days_since(c.get("last_chosen_at")) or 0.0
    return min(1.0, math.log1p(c["times_chosen"]) / math.log1p(8)) * 0.5 ** (age / HALF_LIFE_DAYS)


def token_hit(q: str, desc: set[str]) -> bool:
    """Exact token, or part of a run-together word ("oat" and "milk" both hit "oatmilk")."""
    if q in desc:
        return True
    return len(q) >= 3 and any(len(d) > len(q) and (d.startswith(q) or d.endswith(q)) for d in desc)


def _relevance(query: set[str], p: Product) -> float:
    if not query:
        return 0.0
    desc = set(tokens(f"{p.description} {p.brand or ''}"))
    return sum(token_hit(q, desc) for q in query) / len(query)


def describe(p: Product) -> str:
    return " ".join(x for x in [p.description, p.size] if x)


def candidate_dict(p: Product, *, diets: list[str], overrides: dict, choice: dict | None,
                   score: float = 0.0, why: list[str] | None = None, bought: dict | None = None) -> dict:
    size = parse_size(p.size)
    up = unit_price(p.effective_price, size)
    d = p.to_cache()
    d.update({
        "unit_price": round(up, 4) if up else None,
        "unit_label": size.label() if size else None,
        "diet": {x: dict(zip(("status", "reasons"), dietmod.check(p, x, overrides.get((p.upc, x))))) for x in diets},
        "score": round(score, 3),
        "why": why or [],
        "times_chosen": (choice or {}).get("times_chosen", 0),
        "last_chosen_at": (choice or {}).get("last_chosen_at"),
        "pinned": bool((choice or {}).get("pinned")),
        "bought_times": (bought or {}).get("times", 0),
        "bought_last": (bought or {}).get("last"),
    })
    return d


def resolve(client: KrogerClient, memory: dict, *, key: str, text: str, note: str, diets: list[str],
            location_id: str, size_hint: str | None = None) -> Resolution:
    by_upc = {c["upc"]: c for c in memory["choices"] if c["key"] == key}
    remembered = sorted(by_upc.values(), key=lambda c: (-c["pinned"], -c["times_chosen"]))[:5]
    rejected = {u for (k, u) in memory["rejections"] if k in (key, "")}
    overrides = memory["overrides"]
    purchases = memory.get("purchases", {})

    # --- retrieval
    seen: dict[str, Product] = {}
    for c in remembered:
        p = client.product(c["upc"], location_id)
        if p:
            seen[p.upc] = p
    # Search with the words as written ("bananas"), not the normalized key ("banana"): Kroger ranks them differently.
    query = " ".join(x for x in [text or key, note] if x)
    rank: dict[str, int] = {}     # Kroger's own search order is a useful relevance signal
    searches = [query]
    if diets:
        # "bread (gluten free)": a plain "bread" search returns only wheat breads, so ask for the diet too.
        searches.insert(0, " ".join(d.replace("_", " ") for d in diets) + " " + query)
    for q in searches:
        for i, p in enumerate(client.search(q, location_id, limit=25)):
            seen.setdefault(p.upc, p)
            rank[p.upc] = min(rank.get(p.upc, i), i)
    top_brand = next((c.get("brand") for c in remembered if c.get("brand") and c["times_chosen"] >= 2), None)
    if top_brand:
        for p in client.search(key, location_id, limit=10, brand=top_brand):
            seen.setdefault(p.upc, p)
    # Products from your imported store history whose names match this line ("oat milk" -> "Planet Oat ... Oat Milk").
    qtok_all = {t for t in tokens(query) if t not in _STOP}
    if purchases and qtok_all:
        matches = [(info["times"], upc) for upc, info in purchases.items()
                   if upc not in seen and all(token_hit(q, set(tokens(info.get("name") or ""))) for q in qtok_all)]
        for _, upc in sorted(matches, reverse=True)[:PURCHASE_CANDIDATES]:
            p = client.product(upc, location_id)
            if p:
                seen[p.upc] = p
    all_seen = list(seen.values())

    # --- hard filters
    pool: list[Product] = []
    for p in all_seen:
        if p.upc in rejected or not p.available:
            continue
        if any(dietmod.check(p, d, overrides.get((p.upc, d)))[0] == "fails" for d in diets):
            continue
        pool.append(p)

    # --- scoring
    qtok = {t for t in tokens(query) if t not in _STOP}
    hint = parse_size(size_hint) if size_hint else None
    hist_sizes = [s for s in (parse_size(c.get("size")) for c in remembered if c["times_chosen"]) if s]
    brand_counts: dict[str, int] = {}
    for c in memory["choices"]:
        if c["key"] != key and c.get("brand") and c["times_chosen"]:
            brand_counts[c["brand"]] = brand_counts.get(c["brand"], 0) + 1
    units: dict[str, list[float]] = {}
    for p in pool:
        s = parse_size(p.size)
        up = unit_price(p.effective_price, s)
        if s and up:
            units.setdefault(s.unit, []).append(up)

    scored: list[tuple[float, Product, list[str]]] = []
    for p in pool:
        c = by_upc.get(p.upc)
        why = []
        h = _history_score(c)
        if c and c["times_chosen"]:
            why.append(f"history:{c['times_chosen']}")
        ph = _purchase_score(purchases.get(p.upc)) if _relevance(qtok, p) >= 0.5 else 0.0
        if ph:
            why.append(f"bought:{purchases[p.upc]['times']}")
        h = max(h, ph)
        b = min(1.0, brand_counts.get(p.brand or "", 0) / 3)
        if b:
            why.append("brand_affinity")
        rel = 0.7 * _relevance(qtok, p) + 0.3 * (1 - min(rank[p.upc], 24) / 24 if p.upc in rank else 0.5)
        size = parse_size(p.size)
        sims = [x for x in ([size_similarity(size, hint)] if hint else [size_similarity(size, hs) for hs in hist_sizes]) if x is not None]
        sz = max(sims) if sims else 0.5
        if sims and sz > 0.8:
            why.append("size_match")
        st = STOCK_SCORE.get(p.stock, 0.5)
        up = unit_price(p.effective_price, size)
        pr = 0.5
        if size and up and len(units.get(size.unit, [])) > 1:
            vals = sorted(units[size.unit])
            pr = 1 - vals.index(up) / (len(vals) - 1)
        score = W_HISTORY * h + W_BRAND * b + W_RELEVANCE * rel + W_SIZE * sz + W_STOCK * st + W_PRICE * pr
        if any(dietmod.check(p, d, overrides.get((p.upc, d)))[0] == "unknown" for d in diets):
            score -= UNKNOWN_DIET_PENALTY
            why.append("diet_unknown")
        if c and c.get("pinned"):
            score += 1.0
            why.append("pinned")
        scored.append((score, p, why))
    scored.sort(key=lambda t: -t[0])

    def cand(p: Product, score: float = 0.0, why=None) -> dict:
        return candidate_dict(p, diets=diets, overrides=overrides, choice=by_upc.get(p.upc), score=score, why=why,
                              bought=purchases.get(p.upc))

    if not scored:
        return Resolution("not_found", "nothing matched at this store", None, [], seen=all_seen)

    in_stock = [t for t in scored if t[1].stock != "TEMPORARILY_OUT_OF_STOCK"]
    usual_c = next((c for c in sorted(by_upc.values(), key=lambda c: -c["times_chosen"]) if c["times_chosen"]), None)
    usual = seen.get(usual_c["upc"]) if usual_c else None

    def diet_ok(p: Product) -> bool:
        return all(dietmod.check(p, d, overrides.get((p.upc, d)))[0] in ("likely", "ok_override") for d in diets)

    def top3(exclude: set[str] = frozenset()) -> list[dict]:
        return [cand(p, s, w) for s, p, w in scored if p.upc not in exclude][:3]

    # Pinned
    pinned = next((p for s, p, w in in_stock if by_upc.get(p.upc, {}).get("pinned")), None)
    if pinned and diet_ok(pinned):
        res = Resolution("auto", "pinned", pinned.upc, [cand(pinned, *_sw(scored, pinned))], seen=all_seen)
        return _with_cheaper(res, pinned, scored, qtok, cand, diet_ok)

    # Usual is out of stock / gone
    if usual and usual.upc in rejected:
        usual = None
    if usual_c and (usual is None or not usual.available or usual.stock == "TEMPORARILY_OUT_OF_STOCK"):
        alts = top3({usual_c["upc"]})
        shown = ([cand(usual)] if usual else []) + alts
        why = "your usual is out of stock" if usual and usual.available else "your usual isn't sold here now"
        return Resolution("ask", why, alts[0]["upc"] if alts else None, shown, seen=all_seen)

    if not in_stock:
        return Resolution("ask", "everything that matched is out of stock", None, top3(), seen=all_seen)

    s0, top, w0 = in_stock[0]
    c0 = by_upc.get(top.upc)
    if any(dietmod.check(top, d, overrides.get((top.upc, d)))[0] == "unknown" for d in diets):
        return Resolution("ask", "no ingredient info to check " + ", ".join(d.replace("_", "-") for d in diets),
                          top.upc, top3(), seen=all_seen)
    if c0 and c0["times_chosen"] >= AUTO_MIN_TIMES and (_days_since(c0.get("last_chosen_at")) or 0) <= AUTO_MAX_AGE_DAYS:
        stock = "stock unknown" if top.stock is None else "in stock"
        res = Resolution("auto", f"your usual (chosen {c0['times_chosen']}×, last {(c0.get('last_chosen_at') or '')[:10]}); {stock}",
                         top.upc, [cand(top, s0, w0)] + top3({top.upc})[:2], seen=all_seen)
        return _with_cheaper(res, top, scored, qtok, cand, diet_ok)
    if c0 and c0["times_chosen"] >= 1:
        res = Resolution("confirm", f"chosen once before ({(c0.get('last_chosen_at') or '')[:10]})", top.upc,
                         [cand(top, s0, w0)] + top3({top.upc})[:2], seen=all_seen)
        return _with_cheaper(res, top, scored, qtok, cand, diet_ok)
    b0 = purchases.get(top.upc)
    if b0 and "bought:" in " ".join(w0):
        res = Resolution("confirm", f"you've bought this {b0['times']}× (last {(b0.get('last') or '')[:10]})", top.upc,
                         [cand(top, s0, w0)] + top3({top.upc})[:2], seen=all_seen)
        return _with_cheaper(res, top, scored, qtok, cand, diet_ok)
    return Resolution("ask", "new item: pick one", top.upc, top3(), seen=all_seen)


def _sw(scored, p):
    for s, q, w in scored:
        if q.upc == p.upc:
            return s, w
    return 0.0, []


def _with_cheaper(res: Resolution, chosen: Product, scored, qtok, cand, diet_ok) -> Resolution:
    """Note a cheaper option that meets the same constraints. Never switches."""
    cs = parse_size(chosen.size)
    cu = unit_price(chosen.effective_price, cs)
    best: tuple[float, Product, str] | None = None
    for s, p, w in scored:
        if p.upc == chosen.upc or p.stock == "TEMPORARILY_OUT_OF_STOCK" or not diet_ok(p):
            continue
        if _relevance(qtok, p) < 1.0:
            continue
        ps = parse_size(p.size)
        pu = unit_price(p.effective_price, ps)
        if cu and pu and cs and ps and cs.unit == ps.unit:
            if pu < cu * CHEAPER_MARGIN and (best is None or pu < best[0]):
                best = (pu, p, f"{round((1 - pu / cu) * 100)}% less per {ps.label()}")
        elif p.size == chosen.size and p.effective_price and chosen.effective_price \
                and p.effective_price < chosen.effective_price * CHEAPER_MARGIN:
            if best is None:
                best = (p.effective_price, p, f"${chosen.effective_price - p.effective_price:.2f} less")
    if best:
        _, p, how = best
        sale = " on sale" if p.promo and p.price and p.promo < p.price else ""
        res.sale_note = f"{describe(p)}{sale} ${p.effective_price:.2f}: {how}"
        res.cheaper_upc = p.upc
        if p.upc not in {c["upc"] for c in res.candidates}:
            res.candidates.append(cand(p, *_sw(scored, p)))
    return res
