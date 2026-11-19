"""Local stand-in for the coinbase SDK: supports Ed25519 and ECDSA keys."""
import logging

from cb_client import CoinbaseClient

log = logging.getLogger("bot")


class RESTClient(CoinbaseClient):
    def __init__(self, api_key=None, api_secret=None, **kw):
        super().__init__(api_key=api_key, api_secret=api_secret)
        if api_secret:
            accts = self.get_accounts().get("accounts", [])
            usd = sum(float(a.get("available_balance", {}).get("value", 0))
                      for a in accts if a.get("currency") == "USD")
            log.info("Coinbase key OK. Available USD: $%.2f", usd)
