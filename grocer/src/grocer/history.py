"""Purchase-history import: validates the file the collect bookmarklet downloads.

Format (version 1):
    {"format": "grocer-history", "version": 1,
     "trips": [{"id": "...", "date": "YYYY-MM-DD", "kind": "pickup|delivery|instore",
                "store": "...", "items": [{"upc": "0001111003850", "name": "...", "size": "...", "qty": 1}]}]}
"""

from __future__ import annotations

import json
import re

MAX_BYTES = 5_000_000
MAX_TRIPS = 1000
MAX_ITEMS = 500


class ImportError_(ValueError):
    pass


def normalize_upc(raw) -> str | None:
    """Kroger product IDs are 13 digits without a check digit (verified in the spike)."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits or len(digits) > 14:
        return None
    if len(digits) == 14:          # GTIN-14 with check digit -> drop it, keep 13
        digits = digits[:-1]
    return digits.zfill(13)


def _text(v, limit: int) -> str | None:
    if v is None:
        return None
    return " ".join(str(v).split())[:limit] or None


def parse(payload: bytes | str) -> list[dict]:
    if isinstance(payload, bytes):
        if len(payload) > MAX_BYTES:
            raise ImportError_("That file is too big.")
        payload = payload.decode("utf-8", "replace")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise ImportError_("That isn't a history file from the bookmark (not JSON).")
    if not isinstance(data, dict) or data.get("format") != "grocer-history":
        raise ImportError_("That isn't a history file from the bookmark.")
    if data.get("version") != 1:
        raise ImportError_("This history file is from a newer version. Update the add-on.")
    trips_in = data.get("trips")
    if not isinstance(trips_in, list) or not trips_in:
        raise ImportError_("The file has no trips in it.")
    trips = []
    for t in trips_in[:MAX_TRIPS]:
        if not isinstance(t, dict):
            continue
        tid = _text(t.get("id"), 200)
        date = str(t.get("date") or "")
        if not tid or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            continue
        items = {}
        for it in (t.get("items") or [])[:MAX_ITEMS]:
            if not isinstance(it, dict):
                continue
            upc = normalize_upc(it.get("upc"))
            if not upc:
                continue
            qty = it.get("qty")
            try:
                qty = float(qty) if qty is not None else None
            except (TypeError, ValueError):
                qty = None
            prev = items.get(upc, {})
            items[upc] = {"upc": upc, "name": prev.get("name") or _text(it.get("name"), 200),
                          "size": prev.get("size") or _text(it.get("size"), 60),
                          "qty": (prev.get("qty") or 0) + qty if prev and qty is not None else (qty if qty is not None else prev.get("qty"))}
        if items:
            kind = _text(t.get("kind"), 20)
            trips.append({"id": tid, "date": date, "kind": kind if kind in ("pickup", "delivery", "instore") else None,
                          "store": _text(t.get("store"), 120), "items": list(items.values())})
    if not trips:
        raise ImportError_("No trips with products were found in the file.")
    return trips
