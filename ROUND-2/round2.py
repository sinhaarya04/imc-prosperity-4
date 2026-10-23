import json
from datamodel import Order
import numpy as np
from typing import List, Dict

POSITION_LIMITS = {
    "INTARIAN_PEPPER_ROOT": 80,
    "ASH_COATED_OSMIUM": 80,
}

# ======================
# ASH — Hybrid MM
# ======================
# Combines:
# - Shrey's EWMA fair + clamp (stable FV)
# - Shrey's penny-jump capped at fair±1 (guaranteed edge)
# - Shrey's inventory-aware take thresholds
# - Full remaining capacity on passive quotes
# - Self-imposed ±60 position limit

class AshTrader:
    PRODUCT = "ASH_COATED_OSMIUM"
    STATIC_FAIR = 10000
    SOFT_LIMIT = 50           # self-imposed position cap (60 was too high, bigger losses)
    INVENTORY_BIAS = 1        # threshold for tighter takes

    def run(self, state):
        product = self.PRODUCT
        limit = self.SOFT_LIMIT
        orders = []

        prev_data = json.loads(state.traderData) if state.traderData else {}
        prev_data = prev_data.get(product, {})

        pos = state.position.get(product, 0)
        if product not in state.order_depths:
            return {p: [] for p in POSITION_LIMITS}, 0, json.dumps({})
        depth = state.order_depths[product]

        best_bid = max(depth.buy_orders) if depth.buy_orders else self.STATIC_FAIR - 8
        best_ask = min(depth.sell_orders) if depth.sell_orders else self.STATIC_FAIR + 8

        # ── EWMA fair value, clamped to ±7 from 10000 ──
        mid = (best_bid + best_ask) / 2
        ewma = prev_data.get("ewma", self.STATIC_FAIR)
        ewma = 0.05 * mid + 0.95 * ewma
        fair = max(self.STATIC_FAIR - 7, min(self.STATIC_FAIR + 7, round(ewma)))

        # ── STEP 1: Aggressive takes — inventory-aware thresholds ──
        # When offside (|pos| > 1), tighten the take threshold to flatten faster
        take_buy  = fair - 1 if pos < -self.INVENTORY_BIAS else fair - 2
        take_sell = fair + 1 if pos >  self.INVENTORY_BIAS else fair + 2

        for ask_px in sorted(depth.sell_orders):
            if ask_px > take_buy:
                break
            vol = min(-depth.sell_orders[ask_px], limit - pos)
            if vol > 0:
                orders.append(Order(product, ask_px, vol))
                pos += vol

        for bid_px in sorted(depth.buy_orders, reverse=True):
            if bid_px < take_sell:
                break
            vol = min(depth.buy_orders[bid_px], limit + pos)
            if vol > 0:
                orders.append(Order(product, bid_px, -vol))
                pos -= vol

        # ── STEP 2: Passive MM — penny-jump capped at fair±1 ──
        # Guaranteed minimum 2-tick edge per round trip
        buy_price = min(best_bid + 1, fair - 1)
        sell_price = max(best_ask - 1, fair + 1)

        if buy_price < sell_price:
            buy_qty = limit - pos
            sell_qty = limit + pos
            if buy_qty > 0:
                orders.append(Order(product, buy_price, buy_qty))
            if sell_qty > 0:
                orders.append(Order(product, sell_price, -sell_qty))

        result = {p: [] for p in POSITION_LIMITS}
        result[product] = orders
        trader_data = {"ewma": ewma}
        return result, 0, json.dumps(trader_data)


# ======================
# PEPPER — unchanged from Round 1 (99.7% optimal)
# ======================

class PepperTrader:
    PRODUCT = "INTARIAN_PEPPER_ROOT"

    def run(self, state):
        product = self.PRODUCT
        limit = POSITION_LIMITS[product]
        orders = []
        trader_data = {}
        prev_data = json.loads(state.traderData) if state.traderData else {}
        prev_data = prev_data.get(product, {})

        pos = state.position.get(product, 0)
        if product not in state.order_depths:
            return {p: [] for p in POSITION_LIMITS}, 0, json.dumps({})
        depth = state.order_depths[product]

        bids = sorted(depth.buy_orders.items(), key=lambda x: x[0], reverse=True)
        asks = sorted(depth.sell_orders.items(), key=lambda x: x[0])

        prices = prev_data.get("prices", [])

        deepest_bid = bids[-1][0] if bids and asks else prev_data.get("last_deepest_bid", 10000)
        deepest_ask = asks[-1][0] if asks and bids else prev_data.get("last_deepest_ask", 10000)
        wall_mid = (deepest_bid + deepest_ask) / 2

        best_bid = bids[0][0] if bids else prev_data.get("last_best_bid")
        best_ask = asks[0][0] if asks else prev_data.get("last_best_ask")

        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2
        elif best_bid is not None:
            mid = best_bid
        else:
            mid = best_ask

        ts = state.timestamp
        positive = True

        if ts >= 5000:
            last_prices = prices[-50:]
            if len(last_prices) >= 10:
                x = np.arange(len(last_prices))
                slope, intercept = np.polyfit(x, last_prices, 1)
                predicted = slope * x + intercept
                ss_res = np.sum((last_prices - predicted) ** 2)
                ss_tot = np.sum((last_prices - np.mean(last_prices)) ** 2)
                r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0
                if r_squared >= 0.2:
                    positive = (slope >= 0)
                else:
                    positive = None

        base = None
        if positive is not None and positive:
            current_base = mid - (ts / 1000)
            prev_base = prev_data.get("base")
            if prev_base is not None:
                base = 0.05 * current_base + 0.95 * prev_base
            else:
                base = current_base

            fair = base + ts / 1000.0
            spread = 6 + (ts / 100.0)

            buy_room = limit - pos
            for ask_px, ask_vol in asks:
                if buy_room <= 0:
                    break
                if ask_px <= fair + spread:
                    qty = min(-ask_vol, buy_room)
                    if qty > 0:
                        orders.append(Order(product, ask_px, qty))
                        pos += qty
                        buy_room -= qty

            if buy_room > 0:
                bid_px = int(round(fair))
                orders.append(Order(product, bid_px, buy_room))

        elif positive is not None and not positive:
            current_base = mid + (ts / 1000)
            prev_base = prev_data.get("base")
            if prev_base is not None:
                base = 0.05 * current_base + 0.95 * prev_base
            else:
                base = current_base

            fair = base - ts / 1000.0
            sell_room = limit + pos
            for buy_px, buy_vol in bids:
                if sell_room <= 0:
                    break
                if buy_px >= fair - 10:
                    qty = -min(buy_vol, sell_room)
                    if qty < 0:
                        orders.append(Order(product, buy_px, qty))
                        pos += qty
                        sell_room -= qty

            if sell_room > 0:
                ask_px = int(round(fair))
                orders.append(Order(product, ask_px, sell_room))
        else:
            if pos != 0:
                orders.append(Order(product, int(round(mid)), -pos))

        result = {p: [] for p in POSITION_LIMITS}
        result[product] = orders
        trader_data["last_best_bid"] = best_bid
        trader_data["last_best_ask"] = best_ask
        trader_data["last_deepest_bid"] = deepest_bid
        trader_data["last_deepest_ask"] = deepest_ask
        trader_data["base"] = base if base else prev_data.get("base")
        prices.append(wall_mid)
        trader_data["prices"] = prices[-50:]

        return result, 0, json.dumps(trader_data)


class Trader:
    def __init__(self):
        self.ash = AshTrader()
        self.pepper = PepperTrader()

    def bid(self):
        return 3500

    def run(self, state):
        result = {p: [] for p in POSITION_LIMITS}
        trader_data = {}

        prev = json.loads(state.traderData) if state.traderData else {}

        # Ash
        ash_res, _, ash_data = self.ash.run(state)
        ash_data = json.loads(ash_data)
        for p, o in ash_res.items():
            result[p].extend(o)
        trader_data[self.ash.PRODUCT] = ash_data

        # Pepper
        pepper_res, _, pepper_data = self.pepper.run(state)
        pepper_data = json.loads(pepper_data)
        for p, o in pepper_res.items():
            result[p].extend(o)
        trader_data[self.pepper.PRODUCT] = pepper_data

        return result, 0, json.dumps(trader_data)
