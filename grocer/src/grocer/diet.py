"""Dietary checks, applied only to diets the person asked for.

Status per (product, diet): "likely" | "unknown" | "fails" | "ok_override".
A pass means "likely meets", never a certification.
"""

from __future__ import annotations

import re

from .kroger import Product

# Allergen groups, matched as substrings of Kroger's long-form allergen names
# ("Milk and its derivates", "Almond And Almond Products", ...).
ALLERGEN_GROUPS = {
    "milk": ["milk", "lactose"],
    "egg": ["egg"],
    "fish": ["fish", "crustacean", "mollusc", "shellfish"],
    "wheat": ["wheat", "gluten"],
    "tree_nut": ["tree nut", "almond", "cashew", "hazelnut", "macadamia", "pistachio", "pecan", "walnut",
                 "brazil nut", "pine nut"],
    "peanut": ["peanut"],
    "soy": ["soy"],
}

DIET_ALLERGENS = {
    "vegan": ["milk", "egg", "fish"],
    "vegetarian": ["fish"],
    "dairy_free": ["milk"],
    "egg_free": ["egg"],
    "gluten_free": ["wheat"],
    "nut_free": ["tree_nut", "peanut"],
    "soy_free": ["soy"],
}

# Diets fully decided by regulated allergens: a "Free from" on every relevant group counts as likely.
ALLERGEN_ONLY = {"dairy_free", "egg_free", "nut_free", "soy_free"}

# Manufacturer claims (manufacturerDeclarations) that support each diet.
CLAIMS = {
    "vegan": [{"Vegan"}],
    "vegetarian": [{"Vegetarian"}, {"Vegan"}],
    "dairy_free": [{"Dairy Free"}, {"Vegan"}],
    "egg_free": [{"Egg Free"}, {"Vegan"}],
    "gluten_free": [{"Gluten Free"}],
    "nut_free": [{"Nut Free"}, {"Tree Nuts Free", "Free from Peanuts"}],
    "soy_free": [{"Soy Free"}],
}

_DAIRY = ["milk", "butter", "cream", "cheese", "whey", "casein", "caseinate", "lactose", "yogurt", "ghee",
          "buttermilk", "milkfat", "curd"]
_EGG = ["egg", "eggs", "albumin", "albumen", "egg white", "egg yolk"]
_MEAT_FISH = ["chicken", "beef", "pork", "turkey", "bacon", "ham", "lard", "tallow", "gelatin", "gelatine",
              "anchovy", "anchovies", "fish", "fish sauce", "shrimp", "crab", "oyster sauce", "rennet",
              "bone broth", "collagen"]
DENY = {
    "vegan": _DAIRY + _EGG + _MEAT_FISH + ["honey", "carmine", "cochineal", "shellac", "beeswax", "lanolin"],
    "vegetarian": _MEAT_FISH,
    "dairy_free": _DAIRY,
    "egg_free": _EGG,
    "gluten_free": ["wheat", "barley", "rye", "malt", "brewer's yeast", "spelt", "triticale", "semolina",
                    "durum", "farro", "kamut", "seitan"],
    "nut_free": ["almond", "almonds", "cashew", "cashews", "hazelnut", "hazelnuts", "macadamia", "pistachio",
                 "pistachios", "pecan", "pecans", "walnut", "walnuts", "peanut", "peanuts", "tree nut", "tree nuts"],
    "soy_free": ["soy", "soya", "soybean", "soybeans", "tofu", "edamame", "tamari", "miso"],
}
# Removed from the ingredient text before denylist matching.
FALSE_FRIENDS = ["cocoa butter", "peanut butter", "almond butter", "nut butter", "sunflower butter",
                 "seed butter", "shea butter", "apple butter", "coconut milk", "coconut cream", "oat milk",
                 "oatmilk", "almond milk", "soy milk", "soymilk", "rice milk", "cashew milk", "milk thistle",
                 "cream of tartar", "buckwheat", "eggplant", "butternut", "nutmeg", "coconut",
                 "plant-based butter", "plant butter", "vegan butter", "vegan cheese", "dairy-free cheese",
                 "non-dairy creamer", "cream of coconut", "milkweed"]
AMBIGUOUS = {
    "vegan": ["natural flavor", "natural flavors", "natural flavour", "vitamin d3", "mono- and diglycerides",
              "monoglycerides", "diglycerides", "cane sugar", "sugar", "l-cysteine", "lactic acid", "glycerin",
              "stearic acid"],
    "vegetarian": ["natural flavor", "natural flavors", "enzymes", "l-cysteine"],
    "gluten_free": ["oats", "oat", "modified food starch", "dextrin"],
}

PRODUCE_SAFE = {"vegan", "vegetarian", "dairy_free", "egg_free"}


def _words(text: str, terms: list[str]) -> list[str]:
    found = []
    for t in terms:
        if re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", text):
            found.append(t)
    return found


def _groups_in(allergens: list[dict], level: str) -> set[str]:
    out = set()
    for a in allergens:
        if (a.get("levelOfContainmentName") or "").lower() != level:
            continue
        name = (a.get("name") or "").lower()
        for g, needles in ALLERGEN_GROUPS.items():
            if any(n in name for n in needles):
                out.add(g)
    return out


def check(product: Product, diet: str, override: str | None = None) -> tuple[str, list[str]]:
    """Returns (status, reasons)."""
    if override == "ok":
        return "ok_override", ["you marked this OK"]
    if override == "fails":
        return "fails", ["you marked this as not OK"]

    reasons: list[str] = []
    groups = DIET_ALLERGENS[diet]
    contains = _groups_in(product.allergens, "contains")
    bad = [g for g in groups if g in contains]
    if bad:
        return "fails", [f"allergen: contains {g.replace('_', ' ')}" for g in bad]

    text = (product.ingredients or "").lower()
    if text:
        cleaned = text
        for ff in FALSE_FRIENDS:
            cleaned = cleaned.replace(ff, " ")
        hits = _words(cleaned, DENY[diet])
        if hits:
            return "fails", [f"ingredient: {h}" for h in hits[:3]]

    may = [g for g in groups if g in _groups_in(product.allergens, "may contain")]
    reasons += [f"may contain {g.replace('_', ' ')}" for g in may]
    if text:
        amb = _words(text, AMBIGUOUS.get(diet, []))
        reasons += [f"check: {a}" for a in amb[:3]]

    claims = set(product.declarations or [])
    if any(req <= claims for req in CLAIMS[diet]):
        return "likely", [f"labeled {', '.join(sorted(next(r for r in CLAIMS[diet] if r <= claims)))}"] + reasons
    if text:
        return "likely", reasons
    if diet in ALLERGEN_ONLY:
        free = _groups_in(product.allergens, "free from")
        if all(g in free for g in groups):
            return "likely", ["allergens: free from " + ", ".join(g.replace("_", " ") for g in groups)] + reasons
    if diet in PRODUCE_SAFE and any("produce" in c.lower() for c in product.categories):
        return "likely", ["fresh produce"] + reasons
    return "unknown", ["no ingredient information"] + reasons
