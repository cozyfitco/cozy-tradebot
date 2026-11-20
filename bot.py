"""
Coinbase 10-minute crypto trading bot.

Strategy (long-only, spot):
  ENTRY  (checked every CHECK_INTERVAL_MIN, on closed candles)
    - fast EMA crosses above slow EMA
    - price is above the long trend EMA (only buy in an uptrend)
    - RSI below RSI_MAX (don't buy when overbought)
  EXIT   (checked every 60 seconds)
    - stop-loss, take-profit, trailing stop once in profit,
      or fast EMA crosses back below slow EMA

Safety:
  - PAPER_TRADING=true by default (fake money, no API keys needed)
  - Live trading needs PAPER_TRADING=false AND LIVE_TRADING_CONFIRM=yes
  - Max daily loss kill switch, max open positions, cooldown after a stop-loss
  - Refuses to start if take-profit can't beat round-trip fees
"""

import csv
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from zoneinfo import ZoneInfo


# --------------------------------------------------------------------------
# Config (all set through environment variables)
# --------------------------------------------------------------------------
def env(name, default):
    return os.getenv(name, str(default)).strip()


def env_bool(name, default):
    return env(name, default).lower() in ("1", "true", "yes", "y", "on")


class Config:
    PAPER_TRADING = env_bool("PAPER_TRADING", True)
    LIVE_TRADING_CONFIRM = env("LIVE_TRADING_CONFIRM", "no").lower() == "yes"
    API_KEY = os.getenv("COINBASE_API_KEY", "")
    API_SECRET = os.getenv("COINBASE_API_SECRET", "").replace("\\n", "\n")

    PRODUCTS = [p.strip().upper() for p in env(
        "PRODUCTS", "BTC-USD,ETH-USD,SOL-USD,XRP-USD,LINK-USD,AVAX-USD,LTC-USD,DOGE-USD,ADA-USD,DOT-USD"
    ).split(",") if p.strip()]
    TRADE_SIZE_USD = float(env("TRADE_SIZE_USD", 25))
    MAX_OPEN_POSITIONS = int(env("MAX_OPEN_POSITIONS", 4))
    MAX_DAILY_LOSS_USD = float(env("MAX_DAILY_LOSS_USD", 5))

    # Exits: hold through normal dips, sell on a big drop; ride winners with a trailing stop
    STOP_LOSS_PCT = float(env("STOP_LOSS_PCT", 15.0))       # safety net only
    TAKE_PROFIT_PCT = float(env("TAKE_PROFIT_PCT", 0))      # 0 = no fixed cap, let the trail run
    TRAIL_ACTIVATE_PCT = float(env("TRAIL_ACTIVATE_PCT", 3.0))
    TRAILING_STOP_PCT = float(env("TRAILING_STOP_PCT", 1.0))
    EXIT_ON_TREND_FLIP = env_bool("EXIT_ON_TREND_FLIP", False)  # off = don't sell on dips
    STALE_HOURS = float(env("STALE_HOURS", 48))             # sell flat positions after this long
    STALE_BAND_PCT = float(env("STALE_BAND_PCT", 1.0))      # "flat" = within +/- this %
    FEE_RATE_PCT = float(env("FEE_RATE_PCT", 0.6))          # Coinbase taker fee at your tier
    COOLDOWN_MIN = int(env("COOLDOWN_MIN", 1440))           # wait 24h after a stop-loss

    # Entry filters
    MARKET_FILTER = env("MARKET_FILTER", "BTC-USD")         # only buy when this is above its trend EMA
    MAX_SPREAD_PCT = float(env("MAX_SPREAD_PCT", 0.3))      # skip coins with a wide bid/ask gap
    USE_LIMIT_ORDERS = env_bool("USE_LIMIT_ORDERS", True)   # post-only limits = lower maker fees
    LIMIT_WAIT_SEC = int(env("LIMIT_WAIT_SEC", 90))         # how long a limit order waits to fill

    CHECK_INTERVAL_MIN = int(env("CHECK_INTERVAL_MIN", 10))
    GRANULARITY = env("CANDLE_GRANULARITY", "FIVE_MINUTE")
    FAST_EMA = int(env("FAST_EMA", 9))
    SLOW_EMA = int(env("SLOW_EMA", 21))
    TREND_EMA = int(env("TREND_EMA", 100))
    RSI_PERIOD = int(env("RSI_PERIOD", 14))
    RSI_MAX = float(env("RSI_MAX", 70))

    PAPER_START_USD = float(env("PAPER_START_USD", 200))
    TIMEZONE = env("TIMEZONE", "America/Los_Angeles")
    DATA_DIR = Path(env("DATA_DIR", "/data" if Path("/data").exists() else "./data"))


GRANULARITY_SECONDS = {
    "ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900,
    "THIRTY_MINUTE": 1800, "ONE_HOUR": 3600, "TWO_HOUR": 7200,
}

log = logging.getLogger("bot")


# --------------------------------------------------------------------------
# Indicators
# --------------------------------------------------------------------------
def ema(values, period):
    """Return EMA series (same length as values; None until enough data)."""
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values, period=14):
    """Wilder's RSI of the last value."""
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def signals(closes, cfg=Config):
    """Return dict with buy/sell signals computed on closed candles."""
    fast, slow, trend = ema(closes, cfg.FAST_EMA), ema(closes, cfg.SLOW_EMA), ema(closes, cfg.TREND_EMA)
    if None in (fast[-2], slow[-2], trend[-1]):
        return {"ready": False, "buy": False, "sell": False}
    r = rsi(closes, cfg.RSI_PERIOD)
    cross_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
    cross_down = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
    buy = cross_up and closes[-1] > trend[-1] and r is not None and r < cfg.RSI_MAX
    return {"ready": True, "buy": buy, "sell": cross_down, "rsi": r,
            "fast": fast[-1], "slow": slow[-1], "trend": trend[-1]}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def as_dict(resp):
    if isinstance(resp, dict):
        return resp
    if hasattr(resp, "to_dict"):
        return resp.to_dict()
    return dict(resp)


def round_down(value, increment):
    inc = Decimal(str(increment))
    return str((Decimal(str(value)) / inc).to_integral_value(ROUND_DOWN) * inc)


# --------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------
class Bot:
    def __init__(self, client, cfg=Config):
        self.client = client
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.TIMEZONE)
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.state_path = cfg.DATA_DIR / "state.json"
        self.trades_path = cfg.DATA_DIR / "trades.csv"
        self.state = self._load_state()
        self.last_entry_check = 0.0
        self.product_info = {}

    # ---------- state ----------
    def _load_state(self):
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"positions": {}, "cooldown": {}, "daily": {"date": "", "pnl": 0.0},
                "paper_usd": self.cfg.PAPER_START_USD, "total_pnl": 0.0, "wins": 0, "losses": 0}

    def _save_state(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.state_path)

    def _today(self, now):
        return datetime.fromtimestamp(now, self.tz).strftime("%Y-%m-%d")

    def _roll_day(self, now):
        today = self._today(now)
        if self.state["daily"]["date"] != today:
            self.state["daily"] = {"date": today, "pnl": 0.0}

    def daily_limit_hit(self):
        return self.state["daily"]["pnl"] <= -abs(self.cfg.MAX_DAILY_LOSS_USD)

    # ---------- market data (public endpoints, no keys needed) ----------
    def closed_candles(self, product_id, now):
        gran = GRANULARITY_SECONDS[self.cfg.GRANULARITY]
        n = min(300, self.cfg.TREND_EMA + 60)
        end = int(now)
        start = end - gran * n
        resp = as_dict(self.client.get_public_candles(
            product_id=product_id, start=str(start), end=str(end), granularity=self.cfg.GRANULARITY))
        candles = sorted(resp.get("candles", []), key=lambda c: int(c["start"]))
        # drop the candle that's still forming
        candles = [c for c in candles if int(c["start"]) + gran <= end]
        return [float(c["close"]) for c in candles]

    def price(self, product_id):
        info = as_dict(self.client.get_public_product(product_id=product_id))
        self.product_info[product_id] = info
        return float(info["price"])

    # ---------- orders ----------
    def _wait_fill(self, order_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            o = as_dict(self.client.get_order(order_id=order_id)).get("order", {})
            if o.get("status") == "FILLED":
                return o
            if o.get("status") in ("CANCELLED", "EXPIRED", "FAILED"):
                raise RuntimeError(f"order {order_id} {o.get('status')}")
            time.sleep(1)
        raise RuntimeError(f"order {order_id} not filled in {timeout}s")

    def _info(self, product_id):
        if product_id not in self.product_info:
            self.product_info[product_id] = as_dict(self.client.get_public_product(product_id=product_id))
        return self.product_info[product_id]

    def _poll(self, order_id, timeout):
        """Wait until order is done or timeout. Returns latest order dict."""
        deadline = time.time() + timeout
        o = {}
        while True:
            o = as_dict(self.client.get_order(order_id=order_id)).get("order", {})
            if o.get("status") in ("FILLED", "CANCELLED", "EXPIRED", "FAILED"):
                return o
            if time.time() >= deadline:
                return o
            time.sleep(2)

    def _limit(self, product_id, side, size, price):
        """Post-only limit order. Waits LIMIT_WAIT_SEC, cancels leftover. Returns final order or None."""
        resp = as_dict(self.client.limit_order_post_only(
            client_order_id=str(uuid.uuid4()), product_id=product_id, side=side,
            base_size=size, limit_price=price))
        if not resp.get("success"):
            log.info("%s limit %s not placed: %s", side, product_id, resp.get("error_response"))
            return None
        oid = resp["success_response"]["order_id"]
        o = self._poll(oid, self.cfg.LIMIT_WAIT_SEC)
        if o.get("status") != "FILLED":
            try:
                self.client.cancel_orders(order_ids=[oid])
            except Exception as e:
                log.warning("cancel %s failed: %s", oid, e)
            o = self._poll(oid, 20)
        return o

    @staticmethod
    def _filled(o):
        size = float(o.get("filled_size") or 0)
        avg = float(o.get("average_filled_price") or 0)
        fees = float(o.get("total_fees") or 0)
        return size, avg, fees

    def buy(self, product_id, px):
        usd = self.cfg.TRADE_SIZE_USD
        fee_rate = self.cfg.FEE_RATE_PCT / 100
        if self.cfg.PAPER_TRADING:
            if self.state["paper_usd"] < usd:
                log.warning("Paper balance too low for %s", product_id)
                return None
            fee = usd * fee_rate
            size = (usd - fee) / px
            self.state["paper_usd"] -= usd
            return {"size": size, "entry_price": px, "cost": usd}

        if self.cfg.USE_LIMIT_ORDERS:
            info = self._info(product_id)
            bid, _ = self.client.best_bid_ask(product_id)
            price = round_down(bid, info.get("quote_increment", "0.0001"))
            size = round_down(usd / float(price) / (1 + fee_rate), info.get("base_increment", "0.00000001"))
            o = self._limit(product_id, "BUY", size, price)
            if not o:
                return None
            filled, avg, fees = self._filled(o)
            if filled <= 0:
                log.info("BUY limit %s didn't fill, skipped", product_id)
                return None
            return {"size": filled, "entry_price": avg, "cost": filled * avg + fees}

        resp = as_dict(self.client.market_order_buy(
            client_order_id=str(uuid.uuid4()), product_id=product_id, quote_size=f"{usd:.2f}"))
        if not resp.get("success"):
            log.error("BUY rejected %s: %s", product_id, resp.get("error_response"))
            return None
        o = self._wait_fill(resp["success_response"]["order_id"])
        size, avg, fees = self._filled(o)
        cost = float(o.get("total_value_after_fees") or (size * avg + fees))
        return {"size": size, "entry_price": avg, "cost": cost}

    def _market_sell(self, product_id, size):
        resp = as_dict(self.client.market_order_sell(
            client_order_id=str(uuid.uuid4()), product_id=product_id, base_size=size))
        if not resp.get("success"):
            log.error("SELL rejected %s: %s", product_id, resp.get("error_response"))
            return None
        return self._wait_fill(resp["success_response"]["order_id"])

    def sell(self, product_id, pos, px, reason="exit"):
        fee_rate = self.cfg.FEE_RATE_PCT / 100
        if self.cfg.PAPER_TRADING:
            gross = pos["size"] * px
            proceeds = gross - gross * fee_rate
            self.state["paper_usd"] += proceeds
            return {"exit_price": px, "proceeds": proceeds}

        info = self._info(product_id)
        inc = info.get("base_increment", "0.00000001")
        remaining = float(round_down(pos["size"], inc))
        sold, value, fees = 0.0, 0.0, 0.0

        # stop-loss always sells at market so it exits immediately
        if self.cfg.USE_LIMIT_ORDERS and reason != "stop_loss":
            _, ask = self.client.best_bid_ask(product_id)
            price = round_down(ask, info.get("quote_increment", "0.0001"))
            o = self._limit(product_id, "SELL", round_down(remaining, inc), price)
            if o:
                f, avg, fe = self._filled(o)
                sold += f; value += f * avg; fees += fe
                remaining = float(round_down(remaining - f, inc))

        min_size = float(info.get("base_min_size") or 0)
        if remaining > 0 and remaining >= min_size:
            o = self._market_sell(product_id, round_down(remaining, inc))
            if o:
                f, avg, fe = self._filled(o)
                sold += f; value += f * avg; fees += fe
        if sold <= 0:
            return None
        return {"exit_price": value / sold, "proceeds": value - fees}

    # ---------- logging trades ----------
    def _record(self, product_id, pos, result, reason, now):
        pnl = result["proceeds"] - pos["cost"]
        self.state["daily"]["pnl"] += pnl
        self.state["total_pnl"] += pnl
        self.state["wins" if pnl > 0 else "losses"] += 1
        new = not self.trades_path.exists()
        with self.trades_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["closed_at", "mode", "product", "entry_price", "exit_price",
                            "cost_usd", "proceeds_usd", "pnl_usd", "reason"])
            w.writerow([datetime.fromtimestamp(now, self.tz).isoformat(timespec="seconds"),
                        "paper" if self.cfg.PAPER_TRADING else "LIVE", product_id,
                        f"{pos['entry_price']:.6f}", f"{result['exit_price']:.6f}",
                        f"{pos['cost']:.2f}", f"{result['proceeds']:.2f}", f"{pnl:.2f}", reason])
        log.info("CLOSED %s %s  pnl=$%.2f  | today=$%.2f  total=$%.2f  W/L=%d/%d",
                 product_id, reason, pnl, self.state["daily"]["pnl"], self.state["total_pnl"],
                 self.state["wins"], self.state["losses"])

    # ---------- core loop pieces ----------
    def check_exits(self, now):
        for pid, pos in list(self.state["positions"].items()):
            px = self.price(pid)
            pos["peak"] = max(pos.get("peak", pos["entry_price"]), px)
            change = (px / pos["entry_price"] - 1) * 100
            held_h = (now - pos.get("opened_at", now)) / 3600
            reason = None
            if change <= -self.cfg.STOP_LOSS_PCT:
                reason = "stop_loss"
            elif self.cfg.TAKE_PROFIT_PCT > 0 and change >= self.cfg.TAKE_PROFIT_PCT:
                reason = "take_profit"
            elif (pos["peak"] / pos["entry_price"] - 1) * 100 >= self.cfg.TRAIL_ACTIVATE_PCT and \
                    (px / pos["peak"] - 1) * 100 <= -self.cfg.TRAILING_STOP_PCT:
                reason = "trailing_stop"
            elif self.cfg.EXIT_ON_TREND_FLIP and pos.get("exit_signal"):
                reason = "trend_reversal"
            elif held_h >= self.cfg.STALE_HOURS and abs(change) <= self.cfg.STALE_BAND_PCT:
                reason = "stale_exit"
            if reason:
                result = self.sell(pid, pos, px, reason)
                if result:
                    self._record(pid, pos, result, reason, now)
                    del self.state["positions"][pid]
                    if reason == "stop_loss":
                        self.state["cooldown"][pid] = now + self.cfg.COOLDOWN_MIN * 60
        self._save_state()

    def market_ok(self, now):
        """True when the market-filter coin (BTC) is above its trend EMA."""
        if not self.cfg.MARKET_FILTER:
            return True
        try:
            closes = self.closed_candles(self.cfg.MARKET_FILTER, now)
            trend = ema(closes, self.cfg.TREND_EMA)[-1]
        except Exception as e:
            log.warning("market filter data error: %s", e)
            return False
        if trend is None:
            return False
        ok = closes[-1] > trend
        if not ok:
            log.info("%s below trend (%.2f < %.2f): no new buys this round",
                     self.cfg.MARKET_FILTER, closes[-1], trend)
        return ok

    def spread_ok(self, pid):
        if self.cfg.PAPER_TRADING or self.cfg.MAX_SPREAD_PCT <= 0:
            return True
        try:
            bid, ask = self.client.best_bid_ask(pid)
        except Exception as e:
            log.warning("spread check failed %s: %s", pid, e)
            return False
        spread = (ask - bid) / ((ask + bid) / 2) * 100
        if spread > self.cfg.MAX_SPREAD_PCT:
            log.info("%s spread %.2f%% too wide, skipped", pid, spread)
            return False
        return True

    def check_entries(self, now):
        can_buy = self.market_ok(now)
        for pid in self.cfg.PRODUCTS:
            try:
                closes = self.closed_candles(pid, now)
                sig = signals(closes, self.cfg)
            except Exception as e:
                log.warning("data error %s: %s", pid, e)
                continue
            if not sig["ready"]:
                log.info("%s: not enough candle history yet", pid)
                continue
            log.info("%s close=%.4f fast=%.4f slow=%.4f trend=%.4f rsi=%.1f buy=%s",
                     pid, closes[-1], sig["fast"], sig["slow"], sig["trend"], sig["rsi"] or 0, sig["buy"])

            if pid in self.state["positions"]:
                if sig["sell"]:
                    self.state["positions"][pid]["exit_signal"] = True
                continue
            if not sig["buy"] or not can_buy:
                continue
            if self.daily_limit_hit():
                log.warning("Daily loss limit hit ($%.2f). No new trades until tomorrow.",
                            self.state["daily"]["pnl"])
                return
            if len(self.state["positions"]) >= self.cfg.MAX_OPEN_POSITIONS:
                continue
            if self.state["cooldown"].get(pid, 0) > now:
                continue
            if not self.spread_ok(pid):
                continue
            px = self.price(pid)
            fill = self.buy(pid, px)
            if fill:
                fill["peak"] = fill["entry_price"]
                fill["opened_at"] = now
                self.state["positions"][pid] = fill
                log.info("OPENED %s $%.2f @ %.4f (%s)", pid, fill["cost"], fill["entry_price"],
                         "paper" if self.cfg.PAPER_TRADING else "LIVE")
        self._save_state()
        # any trend_reversal flags get acted on right away
        self.check_exits(now)

    def tick(self, now=None):
        now = now or time.time()
        self._roll_day(now)
        self.check_exits(now)
        if now - self.last_entry_check >= self.cfg.CHECK_INTERVAL_MIN * 60:
            self.last_entry_check = now
            self.check_entries(now)


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------
def preflight(cfg=Config):
    round_trip_fee = 2 * cfg.FEE_RATE_PCT
    min_win = cfg.TRAIL_ACTIVATE_PCT - cfg.TRAILING_STOP_PCT  # worst case once the trail kicks in
    if cfg.TAKE_PROFIT_PCT > 0 and cfg.TAKE_PROFIT_PCT <= round_trip_fee + 0.5:
        sys.exit(f"TAKE_PROFIT_PCT {cfg.TAKE_PROFIT_PCT}% is too close to round-trip fees "
                 f"({round_trip_fee:.2f}%). Raise it.")
    if min_win <= round_trip_fee * 0.5:
        sys.exit(f"Trailing exit locks in only {min_win:.2f}%, too close to fees. "
                 f"Raise TRAIL_ACTIVATE_PCT or lower TRAILING_STOP_PCT.")
    if cfg.TRADE_SIZE_USD < 5:
        sys.exit("TRADE_SIZE_USD must be at least $5.")
    if not cfg.PAPER_TRADING:
        if not cfg.LIVE_TRADING_CONFIRM:
            sys.exit("PAPER_TRADING=false but LIVE_TRADING_CONFIRM is not 'yes'. Refusing to trade real money.")
        if not (cfg.API_KEY and cfg.API_SECRET):
            sys.exit("Live mode needs COINBASE_API_KEY and COINBASE_API_SECRET.")


def make_client(cfg=Config):
    from coinbase.rest import RESTClient
    if cfg.PAPER_TRADING:
        return RESTClient()  # public market data only
    return RESTClient(api_key=cfg.API_KEY, api_secret=cfg.API_SECRET)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    preflight()
    mode = "PAPER (fake money)" if Config.PAPER_TRADING else "LIVE (REAL MONEY)"
    log.info("Starting bot in %s mode | products=%s | $%.0f/trade | max %d open | SL %.1f%% | "
             "trail %.1f%% after +%.1f%% | stale %.0fh | daily max loss $%.0f",
             mode, Config.PRODUCTS, Config.TRADE_SIZE_USD, Config.MAX_OPEN_POSITIONS,
             Config.STOP_LOSS_PCT, Config.TRAILING_STOP_PCT, Config.TRAIL_ACTIVATE_PCT,
             Config.STALE_HOURS, Config.MAX_DAILY_LOSS_USD)
    bot = Bot(make_client())
    while True:
        try:
            bot.tick()
        except Exception as e:
            log.exception("tick failed: %s", e)
        time.sleep(60)


if __name__ == "__main__":
    main()
