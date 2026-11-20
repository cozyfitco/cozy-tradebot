"""
Minimal Coinbase Advanced Trade client.
Works with BOTH Coinbase API key types:
  - Ed25519 (short base64 private key)  -> signs with EdDSA
  - ECDSA   (-----BEGIN EC PRIVATE KEY-----) -> signs with ES256
"""

import base64
import secrets
import time

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HOST = "api.coinbase.com"
BASE = f"https://{HOST}"


def _load_key(secret):
    secret = (secret or "").strip().strip('"').replace("\\n", "\n")
    if "BEGIN" in secret:
        return secret, "ES256"
    raw = base64.b64decode(secret)
    return Ed25519PrivateKey.from_private_bytes(raw[:32]), "EdDSA"


class CoinbaseClient:
    def __init__(self, api_key=None, api_secret=None, timeout=15):
        self.api_key = (api_key or "").strip().strip('"')
        self.timeout = timeout
        self.session = requests.Session()
        self._key, self._alg = (None, None)
        if api_secret:
            self._key, self._alg = _load_key(api_secret)

    # ---------- auth ----------
    def _jwt(self, method, path):
        now = int(time.time())
        payload = {"sub": self.api_key, "iss": "cdp", "nbf": now, "exp": now + 120,
                   "uri": f"{method} {HOST}{path}"}
        headers = {"kid": self.api_key, "nonce": secrets.token_hex(16)}
        return jwt.encode(payload, self._key, algorithm=self._alg, headers=headers)

    def _request(self, method, path, params=None, body=None, auth=True):
        headers = {"Content-Type": "application/json"}
        if auth:
            if not self._key:
                raise RuntimeError("API key/secret missing for a private endpoint")
            headers["Authorization"] = f"Bearer {self._jwt(method, path)}"
        r = self.session.request(method, BASE + path, params=params, json=body,
                                 headers=headers, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    # ---------- public market data ----------
    def get_public_candles(self, product_id, start, end, granularity):
        return self._request("GET", f"/api/v3/brokerage/market/products/{product_id}/candles",
                             params={"start": start, "end": end, "granularity": granularity},
                             auth=False)

    def get_public_product(self, product_id):
        return self._request("GET", f"/api/v3/brokerage/market/products/{product_id}", auth=False)

    # ---------- private market data (not CDN-cached; use in live mode) ----------
    def get_candles(self, product_id, start, end, granularity):
        return self._request("GET", f"/api/v3/brokerage/products/{product_id}/candles",
                             params={"start": start, "end": end, "granularity": granularity})

    def get_product(self, product_id):
        return self._request("GET", f"/api/v3/brokerage/products/{product_id}")

    # ---------- private ----------
    def get_accounts(self):
        return self._request("GET", "/api/v3/brokerage/accounts")

    def get_order(self, order_id):
        return self._request("GET", f"/api/v3/brokerage/orders/historical/{order_id}")

    def _market(self, client_order_id, product_id, side, size_field, size):
        body = {"client_order_id": client_order_id, "product_id": product_id, "side": side,
                "order_configuration": {"market_market_ioc": {size_field: str(size)}}}
        return self._request("POST", "/api/v3/brokerage/orders", body=body)

    def best_bid_ask(self, product_id):
        r = self._request("GET", "/api/v3/brokerage/best_bid_ask", params={"product_ids": product_id})
        book = r["pricebooks"][0]
        return float(book["bids"][0]["price"]), float(book["asks"][0]["price"])

    def limit_order_post_only(self, client_order_id, product_id, side, base_size, limit_price):
        body = {"client_order_id": client_order_id, "product_id": product_id, "side": side,
                "order_configuration": {"limit_limit_gtc": {
                    "base_size": str(base_size), "limit_price": str(limit_price), "post_only": True}}}
        return self._request("POST", "/api/v3/brokerage/orders", body=body)

    def cancel_orders(self, order_ids):
        return self._request("POST", "/api/v3/brokerage/orders/batch_cancel", body={"order_ids": order_ids})

    def market_order_buy(self, client_order_id, product_id, quote_size):
        return self._market(client_order_id, product_id, "BUY", "quote_size", quote_size)

    def market_order_sell(self, client_order_id, product_id, base_size):
        return self._market(client_order_id, product_id, "SELL", "base_size", base_size)
