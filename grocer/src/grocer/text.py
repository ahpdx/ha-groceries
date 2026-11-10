"""Paste parser, item-key normalization and size parsing."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# ------------------------------------------------------------------ diets ---

DIETS = ["vegan", "vegetarian", "dairy_free", "egg_free", "gluten_free", "nut_free", "soy_free"]
DIET_LABELS = {d: d.replace("_", "-") for d in DIETS}

_DIET_WORDS = {
    "vegan": "vegan", "plant based": "vegan", "plant-based": "vegan",
    "vegetarian": "vegetarian", "veggie": "vegetarian",
    "dairy free": "dairy_free", "dairy-free": "dairy_free", "df": "dairy_free", "non dairy": "dairy_free",
    "non-dairy": "dairy_free",
    "egg free": "egg_free", "egg-free": "egg_free",
    "gluten free": "gluten_free", "gluten-free": "gluten_free", "gf": "gluten_free",
    "nut free": "nut_free", "nut-free": "nut_free",
    "soy free": "soy_free", "soy-free": "soy_free",
}


def parse_diets(text: str) -> list[str] | None:
    """'vegan, gluten free' -> ['vegan', 'gluten_free']; None if any part isn't a diet."""
    parts = [p.strip().lower() for p in re.split(r"[,&/+]|\band\b", text) if p.strip()]
    if not parts:
        return None
    out = []
    for p in parts:
        d = _DIET_WORDS.get(p) or _DIET_WORDS.get(p.replace("-", " "))
        if not d:
            return None
        if d not in out:
            out.append(d)
    return out


# ------------------------------------------------------------- list parser ---

@dataclass
class Item:
    raw: str
    text: str
    quantity: int = 1
    diet: list[str] = field(default_factory=list)
    note: str = ""
    merged: int = 1   # how many pasted lines were merged into this one


@dataclass
class ParsedList:
    items: list[Item]
    list_diet: list[str]
    sections: list[str]
    ignored: list[str]


_BULLET = re.compile(
    r"^\s*(?:[-*•◦▪‣·]|\[[ xX✓✔]?\]|☐|☑|☒|✓|✔|◯|○|●|\d+[.)])\s*(?:\[[ xX✓✔]?\]\s*)?")
_QTY_PREFIX = re.compile(r"^(\d{1,2})\s*(?:x|×|\*)?\s+(?=\D)", re.I)
_QTY_PREFIX_X = re.compile(r"^(\d{1,2})\s*(?:x|×)\s*(?=\D)", re.I)
_QTY_SUFFIX = re.compile(r"\s+(?:x|×)\s*(\d{1,2})$|\s+(\d{1,2})\s*(?:x|×)$", re.I)
_PARENS = re.compile(r"\(([^()]*)\)")
_LIST_DIET = re.compile(r"^(?:diet\s*:|everything|all)\s*(.+?)\s*:?$", re.I)


def parse_list(text: str) -> ParsedList:
    lines = [l for l in (text or "").splitlines()]
    nonblank = [l for l in lines if l.strip()]
    # A single comma-separated line is a list of items.
    if len(nonblank) == 1 and "," in nonblank[0] and not _LIST_DIET.match(nonblank[0].strip()):
        lines = nonblank[0].split(",")

    items: list[Item] = []
    list_diet: list[str] = []
    sections: list[str] = []
    ignored: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        m = _LIST_DIET.match(line)
        if m and (d := parse_diets(m.group(1))):
            list_diet += [x for x in d if x not in list_diet]
            continue
        if line.endswith(":") and len(line) > 1:
            sections.append(line[:-1].strip())
            continue
        line = _BULLET.sub("", line).strip()
        if not line or line in ("-", "*"):
            ignored.append(raw)
            continue
        item = _parse_item(line, raw.strip())
        if item:
            items.append(item)
        else:
            ignored.append(raw)

    # Merge exact duplicates (same text, diet and note), adding quantities.
    merged: dict[tuple, Item] = {}
    for it in items:
        k = (normalize_key(it.text), tuple(it.diet), it.note.lower())
        if k in merged:
            merged[k].quantity += it.quantity
            merged[k].merged += 1
        else:
            merged[k] = it
    return ParsedList(list(merged.values()), list_diet, sections, ignored)


def _parse_item(line: str, raw: str) -> Item | None:
    qty = 1
    if m := _QTY_PREFIX_X.match(line) or _QTY_PREFIX.match(line):
        qty = int(m.group(1))
        line = line[m.end():]
    elif m := _QTY_SUFFIX.search(line):
        qty = int(m.group(1) or m.group(2))
        line = line[:m.start()]
    diet: list[str] = []
    notes: list[str] = []
    for inner in _PARENS.findall(line):
        d = parse_diets(inner)
        if d:
            diet += [x for x in d if x not in diet]
        elif inner.strip().isdigit():
            qty = int(inner.strip())
        elif inner.strip():
            notes.append(inner.strip())
    line = _PARENS.sub(" ", line)
    # "bread - gluten free"
    if " - " in line:
        head, tail = line.rsplit(" - ", 1)
        d = parse_diets(tail)
        if d:
            diet += [x for x in d if x not in diet]
            line = head
    text = " ".join(line.split()).strip(" ,;-")
    if not text:
        return None
    return Item(raw=raw, text=text, quantity=max(1, min(qty, 99)), diet=diet, note="; ".join(notes))


# --------------------------------------------------------- key normalizing ---

_UNITS = r"(?:oz|fl\.?\s?oz|floz|lb|lbs|g|kg|ml|l|ct|pk|pack|count|gal|qt|pt|in)"
_AMOUNT = re.compile(rf"\b\d+(?:\.\d+)?\s*{_UNITS}\b|\b\d+(?:\.\d+)?\b", re.I)
_NO_SINGULAR = {"hummus", "asparagus", "couscous", "swiss", "molasses", "grits", "oats", "chips", "series",
                "species", "glass", "brussels", "lentils", "peas", "greens", "noodles", "sprinkles", "jeans"}
# Words where the plural is the usual way to write the item; keep plural, but map singular -> plural too.
_PLURAL_FORMS = {w[:-1]: w for w in ("oats", "chips", "lentils", "peas", "greens", "noodles", "sprinkles")}


def singularize(word: str) -> str:
    w = word
    if w in _NO_SINGULAR or len(w) <= 3:
        return w
    if w in _PLURAL_FORMS:
        return _PLURAL_FORMS[w]
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("oes", "ches", "shes", "sses", "xes")):
        return w[:-2]
    if w.endswith("ves") and w not in ("olives", "chives", "cloves"):
        return w[:-3] + "f"
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def tokens(text: str) -> list[str]:
    t = text.lower().replace("&", " and ").replace("’", "'")
    t = re.sub(r"[®™©]", "", t)
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return [singularize(w.strip("'")) for w in t.split() if w.strip("'")]


def normalize_key(text: str, aliases: dict[str, str] | None = None) -> str:
    t = _AMOUNT.sub(" ", text.lower())
    key = " ".join(tokens(t))
    if aliases:
        key = aliases.get(key, key)
    return key


# ------------------------------------------------------------------- sizes ---

@dataclass(frozen=True)
class Size:
    amount: float
    unit: str        # "oz" (weight), "fl oz" (volume), "ct" (count)

    def label(self) -> str:
        return {"oz": "oz", "fl oz": "fl oz", "ct": "each"}[self.unit]


_TO_BASE = {
    "oz": ("oz", 1), "ounce": ("oz", 1), "lb": ("oz", 16), "lbs": ("oz", 16), "pound": ("oz", 16),
    "g": ("oz", 1 / 28.3495), "gram": ("oz", 1 / 28.3495), "kg": ("oz", 35.274),
    "fl oz": ("fl oz", 1), "floz": ("fl oz", 1), "fo": ("fl oz", 1),
    "gal": ("fl oz", 128), "gallon": ("fl oz", 128), "half gallon": ("fl oz", 64), "qt": ("fl oz", 32),
    "quart": ("fl oz", 32), "pt": ("fl oz", 16), "pint": ("fl oz", 16),
    "l": ("fl oz", 33.814), "liter": ("fl oz", 33.814), "litre": ("fl oz", 33.814), "ml": ("fl oz", 0.033814),
    "ct": ("ct", 1), "count": ("ct", 1), "ea": ("ct", 1), "each": ("ct", 1), "pk": ("ct", 1), "pack": ("ct", 1),
    "roll": ("ct", 1), "rolls": ("ct", 1), "bottles": ("ct", 1), "cans": ("ct", 1), "sticks": ("ct", 1),
    "stick": ("ct", 1), "bags": ("ct", 1), "pods": ("ct", 1), "bars": ("ct", 1), "cups": ("ct", 1),
}
_PART = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-z][a-z .]*?)\.?$")


def _clean_size(s: str) -> str:
    s = s.lower().replace("net wt", "").replace("fl. oz", "fl oz").replace("fl.oz", "fl oz")
    s = re.sub(r"(\d)oza\b", r"\1 oz", s)
    s = re.sub(r"(\d)\s*([a-z])", r"\1 \2", s)
    return " ".join(s.split())


def parse_size(text: str | None) -> Size | None:
    """'52 fl oz' -> Size(52, 'fl oz'); '14.5 oz / 4 pk' -> Size(58, 'oz'); '4 sticks / 1 lb' -> Size(16, 'oz')."""
    if not text:
        return None
    parts = [p.strip() for p in _clean_size(text).split("/") if p.strip()]
    counts: list[tuple[float, str]] = []
    measures: list[Size] = []
    for p in parts:
        m = _PART.match(p)
        if not m:
            continue
        amount, unit = float(m.group(1)), m.group(2).strip()
        base = _TO_BASE.get(unit)
        if not base:
            continue
        family, factor = base
        if family == "ct":
            counts.append((amount, unit))
        else:
            measures.append(Size(amount * factor, family))
    if measures:
        size = measures[0]
        # '14.5 oz / 4 pk', '24 bottles / 16.9 fl oz': the measure is per container, so multiply.
        # '24 ct / 6 lb', '4 sticks / 1 lb': the measure is already the total.
        per_container = [n for n, u in counts if u in _PER_CONTAINER]
        if len(measures) == 1 and per_container and per_container[0] > 1:
            return Size(round(size.amount * per_container[0], 3), size.unit)
        return size
    return Size(counts[0][0], "ct") if counts else None


_PER_CONTAINER = {"pk", "pack", "bottles", "cans", "cups", "bags"}


def unit_price(price: float | None, size: Size | None) -> float | None:
    if not price or not size or size.amount <= 0:
        return None
    return price / size.amount


def size_similarity(a: Size | None, b: Size | None) -> float | None:
    if not a or not b or a.unit != b.unit or a.amount <= 0 or b.amount <= 0:
        return None
    return max(0.0, 1 - abs(math.log(a.amount / b.amount)) / math.log(4))
