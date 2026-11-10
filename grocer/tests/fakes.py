"""A fake Kroger client built from real-shaped product records."""

from grocer.kroger import CartResult, NotSignedIn, Product
from grocer.engine import token_hit
from grocer.text import tokens


def prod(upc, description, *, brand=None, size="1 ct", price=1.0, promo=None, stock="HIGH", sold_by="UNIT",
         available=True, ingredients=None, allergens=(), declarations=(), categories=()):
    return Product(upc=upc, description=description, brand=brand, size=size, price=price, promo=promo,
                   promo_until=None, stock=stock, sold_by=sold_by, available=available,
                   categories=list(categories), ingredients=ingredients,
                   allergens=[{"name": n, "levelOfContainmentName": lvl} for lvl, n in allergens],
                   declarations=list(declarations))


class FakeKroger:
    def __init__(self, products, cart_outcome="ok", signed_in=True):
        self.products = {p.upc: p for p in products}
        self.cart_outcome = cart_outcome
        self.signed_in = signed_in
        self.cart_calls = []
        self.searches = []
        self.configured = True

    def search(self, term, location_id, limit=25, brand=None):
        self.searches.append((term, brand))
        q = set(tokens(term))
        out = []
        for p in self.products.values():
            if brand and p.brand != brand:
                continue
            if any(token_hit(x, set(tokens(f"{p.description} {p.brand or ''}"))) for x in q):
                out.append(p)
        return out[:limit]

    def product(self, upc, location_id):
        return self.products.get(upc)

    def cart_add(self, items):
        if not self.signed_in:
            raise NotSignedIn("no")
        self.cart_calls.append(items)
        return CartResult(self.cart_outcome, 204 if self.cart_outcome == "ok" else (400 if self.cart_outcome == "failed" else 503),
                          "bad upc" if self.cart_outcome == "failed" else "")
