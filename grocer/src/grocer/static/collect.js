// Groceries: collect purchase history, one trip at a time.
// Click it on a Kroger-family "Purchase Details" page. It reads the items on that page, adds the trip to a
// collection kept in this browser (on this site only), and offers a download for the Groceries add-on.
// It reads only the page you are on and sends nothing anywhere.
(() => {
  const KEY = "groceriesHistoryV1";
  const load = () => {
    try { return JSON.parse(localStorage.getItem(KEY)) || { trips: {} }; } catch (e) { return { trips: {} }; }
  };
  const save = (d) => localStorage.setItem(KEY, JSON.stringify(d));
  const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");

  const readTrip = () => {
    const m = location.pathname.match(/\/mypurchases\/detail\/([^/?#]+)/);
    if (!m) return { error: "Open a trip's Purchase Details page first (Purchase History, then a trip)." };
    const id = decodeURIComponent(m[1]);
    const dm = id.match(/^(\d{4}-\d{2}-\d{2})/);
    if (!dm) return { error: "This page's address doesn't include a date, so it can't be collected." };
    const parts = id.split("_");
    const store = parts.length >= 3 ? parts[1] + parts[2] : "";
    const heads = [...document.querySelectorAll("h1,h2,h3")].map(text).join(" | ");
    const kind = /delivery order/i.test(heads) ? "delivery" : /pickup order/i.test(heads) ? "pickup"
      : /in-?store/i.test(heads) ? "instore" : null;
    const items = {};
    let rows = 0;
    document.querySelectorAll('li [data-testid^="list-style-product-card-"]').forEach((card) => {
      const li = card.closest("li");
      if (!li || li.dataset.groceriesSeen) return;
      li.dataset.groceriesSeen = "1";
      if (li.querySelector('[data-testid="pla-compliance-tag"]')) return;          // sponsored, not bought
      const link = li.querySelector('a[href*="/p/"]');
      const um = link && link.getAttribute("href").match(/\/(\d{8,14})(?:[/?#]|$)/);
      if (!um) return;
      const qtyEl = li.querySelector(".pb-8 .font-bold");
      const qty = qtyEl ? parseFloat(text(qtyEl).replace(/[^\d.]/g, "")) : NaN;
      if (!qtyEl || isNaN(qty)) return;                                           // no "bought" quantity: not a purchased line
      const name = text(li.querySelector('[data-testid="cart-page-item-description"]')) || card.getAttribute("aria-label") || "";
      const size = text(li.querySelector('[data-testid="product-item-sizing"]'));
      rows++;
      const prev = items[um[1]];
      items[um[1]] = { upc: um[1], name: (prev && prev.name) || name, size: (prev && prev.size) || size,
        qty: (prev ? prev.qty : 0) + qty };
    });
    document.querySelectorAll("[data-groceries-seen]").forEach((li) => delete li.dataset.groceriesSeen);
    if (!rows) return { error: "No items found yet. Wait for the item list to finish loading, then click again." };
    return { trip: { id, date: dm[1], kind, store, items: Object.values(items) } };
  };

  const download = (d) => {
    const trips = Object.values(d.trips).sort((a, b) => a.date.localeCompare(b.date));
    const blob = new Blob([JSON.stringify({ format: "grocer-history", version: 1, trips }, null, 1)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `grocery-history-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  };

  const panel = (msg, d) => {
    document.getElementById("groceries-collect")?.remove();
    const n = Object.keys(d.trips).length;
    const items = Object.values(d.trips).reduce((s, t) => s + t.items.length, 0);
    const box = document.createElement("div");
    box.id = "groceries-collect";
    box.style.cssText = "position:fixed;top:16px;right:16px;z-index:2147483647;max-width:340px;background:#fff;color:#111;" +
      "border:1px solid #8a8a8a;border-radius:6px;box-shadow:0 4px 16px rgba(0,0,0,.15);padding:14px 16px;" +
      "font:15px/1.4 system-ui,-apple-system,'Segoe UI',sans-serif";
    const p = (t, css) => { const e = document.createElement("div"); e.textContent = t; e.style.cssText = css || ""; box.appendChild(e); };
    const btn = (label, primary, fn) => {
      const b = document.createElement("button");
      b.textContent = label;
      b.style.cssText = "margin:10px 8px 0 0;padding:8px 12px;border-radius:4px;font:inherit;cursor:pointer;" +
        (primary ? "background:#111;color:#fff;border:1px solid #111;font-weight:600" : "background:#f3f3f3;color:#111;border:1px solid #8a8a8a");
      b.onclick = fn;
      box.appendChild(b);
    };
    p("Grocery history", "font-weight:700;margin-bottom:4px");
    p(msg);
    p(`${n} trip${n === 1 ? "" : "s"} collected, ${items} items.`, "color:#666;margin-top:6px");
    if (n) btn("Download all", true, () => download(d));
    btn("Close", false, () => box.remove());
    if (n) btn("Start over", false, () => {
      if (confirm("Forget the trips collected in this browser?")) { localStorage.removeItem(KEY); box.remove(); }
    });
    document.body.appendChild(box);
  };

  const d = load();
  const r = readTrip();
  if (r.error) return panel(r.error, d);
  const already = !!d.trips[r.trip.id];
  d.trips[r.trip.id] = r.trip;
  save(d);
  panel(`${already ? "Updated" : "Added"} the ${r.trip.date} trip (${r.trip.items.length} items). ` +
    "Open the next trip and click the bookmark again, or download when you're done.", d);
})();
