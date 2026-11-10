"""Minimal Kroger public API client (stdlib only).

Covers exactly what grocer uses: client-credentials catalog token, product
search and lookup, authorization-code + PKCE sign-in, refresh (Kroger rotates
the refresh token on every refresh), and cart add.

Token handling rules:
- User tokens live in one JSON file, mode 600, written atomically.
- Refreshes happen under an exclusive file lock, and the new refresh token is
  persisted before the access token is used.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

API = "https://api.kroger.com/v1"
TOKEN_URL = f"{API}/connect/oauth2/token"
AUTHORIZE_URL = f"{API}/connect/oauth2/authorize"
USER_SCOPES = "product.compact cart.basic:write"
TIMEOUT = 20


class KrogerError(Exception):
    pass


class NotSignedIn(KrogerError):
    """No usable user token: the person must sign in again."""


@dataclass
class CartResult:
    outcome: str          # "ok" | "failed" | "unknown"
    status: int | None
    detail: str = ""


@dataclass
class Product:
    upc: str
    description: str
    brand: str | None
    size: str | None
    price: float | None
    promo: float | None
    promo_until: str | None
    stock: str | None             # HIGH | LOW | TEMPORARILY_OUT_OF_STOCK | None (unknown)
    sold_by: str | None           # UNIT | WEIGHT
    available: bool               # sold at this location (has a price and some fulfillment)
    categories: list[str] = field(default_factory=list)
    ingredients: str | None = None
    allergens: list[dict] = field(default_factory=list)
    declarations: list[str] = field(default_factory=list)
    image: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def effective_price(self) -> float | None:
        if self.promo and self.price and 0 < self.promo < self.price:
            return self.promo
        return self.price

    def to_cache(self) -> dict:
        d = {k: getattr(self, k) for k in (
            "upc", "description", "brand", "size", "price", "promo", "promo_until", "stock",
            "sold_by", "available", "categories", "ingredients", "allergens", "declarations", "image")}
        return d

    @classmethod
    def from_cache(cls, d: dict) -> "Product":
        return cls(**d)


def parse_product(p: dict) -> Product:
    item = (p.get("items") or [{}])[0]
    price = item.get("price") or {}
    regular = price.get("regular") or None
    promo = price.get("promo") or None
    fulfillment = item.get("fulfillment") or {}
    ingredients = None
    for n in p.get("nutritionInformation") or []:
        if isinstance(n, dict) and n.get("ingredientStatement"):
            ingredients = " ".join(n["ingredientStatement"].split())
            break
    image = None
    imgs = p.get("images") or []
    imgs = sorted(imgs, key=lambda i: (not i.get("featured"), i.get("perspective") != "front"))
    for img in imgs:
        sizes = {s.get("size"): s.get("url") for s in img.get("sizes") or []}
        image = sizes.get("medium") or sizes.get("small") or sizes.get("large") or sizes.get("thumbnail")
        if image:
            break
    return Product(
        upc=p.get("upc") or item.get("itemId") or p.get("productId"),
        description=p.get("description") or "",
        brand=p.get("brand"),
        size=item.get("size"),
        price=regular,
        promo=promo,
        promo_until=((price.get("expirationDate") or {}).get("value") if promo else None),
        stock=(item.get("inventory") or {}).get("stockLevel"),
        sold_by=item.get("soldBy"),
        available=bool(regular) and any(fulfillment.values()),
        categories=p.get("categories") or [],
        ingredients=ingredients,
        allergens=[a for a in p.get("allergens") or [] if isinstance(a, dict)],
        declarations=p.get("manufacturerDeclarations") or [],
        image=image,
        raw=p,
    )


def _http(method: str, url: str, headers: dict, data: bytes | None = None, timeout: float = TIMEOUT):
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body) if body else None
        except json.JSONDecodeError:
            return e.code, body.decode("utf-8", "replace")[:500]


class KrogerClient:
    def __init__(self, client_id: str, client_secret: str, token_path: Path, redirect_uri: str):
        self.client_id = client_id
        self._secret = client_secret
        self.token_path = Path(token_path)
        self.redirect_uri = redirect_uri
        self._catalog: tuple[str, float] | None = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self._secret)

    # ---------------------------------------------------------------- tokens
    def _basic(self) -> str:
        return "Basic " + base64.b64encode(f"{self.client_id}:{self._secret}".encode()).decode()

    def _token_request(self, form: dict):
        return _http("POST", TOKEN_URL, {
            "Authorization": self._basic(),
            "Content-Type": "application/x-www-form-urlencoded",
        }, urllib.parse.urlencode(form).encode())

    def catalog_token(self) -> str:
        with self._lock:
            if self._catalog and time.time() < self._catalog[1] - 60:
                return self._catalog[0]
            status, body = self._token_request({"grant_type": "client_credentials", "scope": "product.compact"})
            if status != 200:
                raise KrogerError(f"catalog token failed: HTTP {status}")
            self._catalog = (body["access_token"], time.time() + int(body.get("expires_in", 1800)))
            return self._catalog[0]

    # --- user sign-in (authorization code + PKCE)
    @staticmethod
    def new_pkce() -> tuple[str, str, str]:
        """Returns (state, verifier, challenge)."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return secrets.token_urlsafe(24), verifier, challenge

    def authorize_url(self, state: str, challenge: str) -> str:
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "scope": USER_SCOPES, "response_type": "code", "client_id": self.client_id,
            "redirect_uri": self.redirect_uri, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })

    def exchange_code(self, code: str, verifier: str) -> None:
        status, body = self._token_request({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": self.redirect_uri, "code_verifier": verifier,
        })
        if status != 200 or not isinstance(body, dict) or "access_token" not in body:
            raise KrogerError(f"sign-in failed: HTTP {status} {(body or {}).get('error_description', '') if isinstance(body, dict) else ''}")
        self._write_tokens({**body, "obtained_at": time.time()})

    def _write_tokens(self, tok: dict) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(tok, f)
        os.replace(tmp, self.token_path)

    def _read_tokens(self) -> dict | None:
        try:
            return json.loads(self.token_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def sign_in_status(self) -> dict:
        tok = self._read_tokens()
        if not tok:
            return {"signed_in": False}
        return {"signed_in": "refresh_token" in tok or "access_token" in tok,
                "since": tok.get("first_obtained_at", tok.get("obtained_at"))}

    def sign_out(self) -> None:
        self.token_path.unlink(missing_ok=True)

    def user_token(self) -> str:
        tok = self._read_tokens()
        if not tok:
            raise NotSignedIn("not signed in")
        if time.time() < tok.get("obtained_at", 0) + int(tok.get("expires_in", 0)) - 120:
            return tok["access_token"]
        lock_path = self.token_path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            tok = self._read_tokens()  # someone may have refreshed while we waited
            if tok and time.time() < tok.get("obtained_at", 0) + int(tok.get("expires_in", 0)) - 120:
                return tok["access_token"]
            if not tok or "refresh_token" not in tok:
                raise NotSignedIn("no refresh token")
            status, body = self._token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
            if status in (400, 401):
                raise NotSignedIn("refresh token rejected")
            if status != 200 or not isinstance(body, dict):
                raise KrogerError(f"refresh failed: HTTP {status}")
            new = {**tok, **body, "obtained_at": time.time(),
                   "first_obtained_at": tok.get("first_obtained_at", tok.get("obtained_at"))}
            self._write_tokens(new)
            return new["access_token"]

    # ---------------------------------------------------------------- catalog
    def _get(self, path: str, params: dict) -> dict:
        url = f"{API}{path}?" + urllib.parse.urlencode(params)
        status, body = _http("GET", url, {"Authorization": f"Bearer {self.catalog_token()}", "Accept": "application/json"})
        if status == 401:
            self._catalog = None
            status, body = _http("GET", url, {"Authorization": f"Bearer {self.catalog_token()}", "Accept": "application/json"})
        if status == 404:
            return {"data": None}
        if status != 200:
            raise KrogerError(f"GET {path}: HTTP {status}")
        return body

    def search(self, term: str, location_id: str, limit: int = 25, brand: str | None = None) -> list[Product]:
        term = " ".join(term.split()[:8])
        params = {"filter.term": term, "filter.locationId": location_id, "filter.limit": limit}
        if brand:
            params["filter.brand"] = brand
        return [parse_product(p) for p in self._get("/products", params).get("data") or []]

    def locations(self, zip_code: str, limit: int = 10) -> list[dict]:
        data = self._get("/locations", {"filter.zipCode.near": zip_code, "filter.limit": limit}).get("data") or []
        out = []
        for s in data:
            a = s.get("address") or {}
            out.append({"id": s.get("locationId"), "name": s.get("name"), "chain": s.get("chain"),
                        "address": ", ".join(x for x in [a.get("addressLine1"), a.get("city"), a.get("state")] if x)})
        return out

    def product(self, upc: str, location_id: str) -> Product | None:
        data = self._get(f"/products/{upc}", {"filter.locationId": location_id}).get("data")
        return parse_product(data) if data else None

    # ---------------------------------------------------------------- cart
    def cart_add(self, items: list[dict]) -> CartResult:
        """items: [{upc, quantity, modality}]. Never retried: a timeout or 5xx is 'unknown'."""
        try:
            token = self.user_token()
        except NotSignedIn:
            raise
        except KrogerError as e:
            return CartResult("failed", None, str(e))
        try:
            status, body = _http("PUT", f"{API}/cart/add", {
                "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json",
            }, json.dumps({"items": items}).encode(), timeout=30)
        except (TimeoutError, OSError) as e:
            return CartResult("unknown", None, f"no response: {e.__class__.__name__}")
        if 200 <= status < 300:
            return CartResult("ok", status)
        if status >= 500:
            return CartResult("unknown", status, "server error after sending")
        detail = ""
        if isinstance(body, dict):
            detail = (body.get("errors") or {}).get("reason") if isinstance(body.get("errors"), dict) else str(body)[:300]
        return CartResult("failed", status, detail or "")
