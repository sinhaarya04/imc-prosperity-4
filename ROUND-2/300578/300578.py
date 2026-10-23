import json
from datamodel import Order
import typing
import numpy as np
from typing import List, Dict

POSITION_LIMITS = {
    "INTARIAN_PEPPER_ROOT": 80,
    "ASH_COATED_OSMIUM": 80,
}

def clamp_qty(desired_qty, position, limit):
    """
    Clamp desired_qty so that position + qty stays within [-limit, +limit].
    desired_qty > 0 => buy, desired_qty < 0 => sell.
    """
    if desired_qty > 0:
        return min(desired_qty, limit - position)
    elif desired_qty < 0:
        return max(desired_qty, -limit - position)
    return 0

# ======================
# ASH BEGIN
# ======================

class SimpleAshTrader:
    def __init__(self, mm_qty, mm_spread):
        self.product    = "ASH_COATED_OSMIUM"
        self.mm_qty     = mm_qty
        self.mm_spread  = mm_spread

    def __str__(self):
        return (
            "SET("
                f"mm_qty={self.mm_qty},"
                f"mm_spread={self.mm_spread}"
            ")"
        )

    def run(self, state):
        orders              = []
        product             = self.product
        limit               = POSITION_LIMITS[product]
        trader_data         = dict()
        prev_trader_data    = json.loads(state.traderData) if state.traderData else dict()
        prev_trader_data    = prev_trader_data.get(product, {})

        position        = state.position.get(product, 0)
        if product not in state.order_depths:
            return orders, 0, trader_data
        depth           = state.order_depths[product]
        bids            = sorted(depth.buy_orders.items(),
                                 key=lambda x: x[0],
                                 reverse=True)
        asks            = sorted(depth.sell_orders.items(),
                                 key=lambda x: x[0])

        last_best_bid = prev_trader_data.get("last_best_bid")
        last_best_ask = prev_trader_data.get("last_best_ask")
        last_best_bid = int(last_best_bid) if last_best_bid else None
        last_best_ask = int(last_best_ask) if last_best_ask else None

        best_bid = bids[0][0] if bids else (last_best_bid if last_best_bid else 10000)
        best_ask = asks[0][0] if asks else (last_best_ask if last_best_ask else 10000)

        # ── Use best_mid instead of wall_mid ──
        mid = round((best_bid + best_ask) / 2)

        # ── Inventory skew: shift fair value to encourage flattening ──
        skewed_mid = round(mid - 0.1 * position)

        # ── STEP 1: take +EV arb trades ──
        # Sell into bids above mid (+ flatten at mid if long)
        for i, (bid_px, bid_vol) in enumerate(bids):
            if bid_px < mid:
                break
            if bid_px == mid and position <= 0:
                break
            if bid_px > mid:
                sell_qty = clamp_qty(-bid_vol, position, limit)
            else:
                sell_qty = max(-position, -bid_vol)
            if sell_qty != 0:
                orders.append(Order(product, bid_px, sell_qty))
                position += sell_qty
                bids[i] = (bid_px, bid_vol + sell_qty)

        # Buy from asks below mid (+ flatten at mid if short)
        for i, (ask_px, ask_vol) in enumerate(asks):
            if ask_px > mid:
                break
            if ask_px == mid and position >= 0:
                break
            if ask_px < mid:
                buy_qty = clamp_qty(-ask_vol, position, limit)
            else:
                buy_qty = min(-position, -ask_vol)
            if buy_qty != 0:
                orders.append(Order(product, ask_px, buy_qty))
                position += buy_qty
                asks[i] = (ask_px, ask_vol + buy_qty)

        bids = [b for b in bids if b[1] > 0]
        asks = [a for a in asks if a[1] < 0]

        # ── STEP 2: market-make with inventory management ──
        best_bid = bids[0][0] if bids else (last_best_bid if last_best_bid else 10000)
        best_ask = asks[0][0] if asks else (last_best_ask if last_best_ask else 10000)

        # Two-tier inventory threshold:
        #   |position| > 30: halve quote size on building side
        #   |position| > 50: stop quoting building side entirely
        bid_qty_raw = self.mm_qty
        ask_qty_raw = self.mm_qty

        if position > 30:
            bid_qty_raw = bid_qty_raw // 2
        if position > 50:
            bid_qty_raw = 0
        if position < -30:
            ask_qty_raw = ask_qty_raw // 2
        if position < -50:
            ask_qty_raw = 0

        mm_bid_px = min(skewed_mid, best_bid + self.mm_spread)
        bid_qty = clamp_qty(bid_qty_raw, position, limit)
        if bid_qty > 0:
            orders.append(Order(product, mm_bid_px, bid_qty))

        mm_ask_px = max(skewed_mid, best_ask - self.mm_spread)
        ask_qty = clamp_qty(-ask_qty_raw, position, limit)
        if ask_qty < 0:
            orders.append(Order(product, mm_ask_px, ask_qty))

        # result : product -> List[orders]
        result = {product: [] for product in POSITION_LIMITS}
        result[product] = orders
        trader_data["last_best_bid"] = best_bid
        trader_data["last_best_ask"] = best_ask
        return result, 0, json.dumps(trader_data)


# ======================
# ASH END
# ======================

# ======================
# PEPPER BEGIN
# ======================

class SimplePepperTrader:
    def __init__(self, mm_qty, mm_long_spread, mm_short_spread, mm_long_qty):
        self.product        = "INTARIAN_PEPPER_ROOT"
        self.mm_qty         = mm_qty
        self.mm_long_spread = mm_long_spread
        self.mm_short_spread = mm_short_spread
        self.mm_long_qty    = mm_long_qty

    def __str__(self):
        return (
            "STT("
                f"mm_qty={self.mm_qty},"
                f"mm_long_spread={self.mm_long_spread}"
                f"mm_short_spread={self.mm_short_spread}"
            ")"
        )

    def run(self, state):
        positive = True
        orders              = []
        product             = self.product
        limit               = POSITION_LIMITS[product]
        trader_data         = dict()
        prev_trader_data    = json.loads(state.traderData) if state.traderData else dict()
        prev_trader_data    = prev_trader_data.get(product, {})

        position        = state.position.get(product, 0)
        if product not in state.order_depths:
            return orders, 0, trader_data
        depth           = state.order_depths[product]
        bids            = sorted(depth.buy_orders.items(),
                                 key=lambda x: x[0],
                                 reverse=True)
        asks            = sorted(depth.sell_orders.items(),
                                 key=lambda x: x[0])

        prices = prev_trader_data.get("prices", [])
        last_best_bid = prev_trader_data.get("last_best_bid")
        last_best_ask = prev_trader_data.get("last_best_ask")
        last_deepest_bid = prev_trader_data.get("last_deepest_bid")
        last_deepest_ask = prev_trader_data.get("last_deepest_ask")

        last_best_bid = int(last_best_bid) if last_best_bid else None
        last_best_ask = int(last_best_ask) if last_best_ask else None
        last_deepest_bid = int(last_deepest_bid) if last_deepest_bid else None
        last_deepest_ask = int(last_deepest_ask) if last_deepest_ask else None

        deepest_bid = bids[-1][0] if bids and asks else (last_deepest_bid if last_deepest_bid else 10000)
        deepest_ask = asks[-1][0] if asks and bids else (last_deepest_ask if last_deepest_ask else 10000)
        wall_mid = (deepest_bid + deepest_ask) / 2

        best_bid = bids[0][0] if bids else last_best_bid
        best_ask = asks[0][0] if asks else last_best_ask

        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2
        elif best_bid is not None:
            mid = best_bid
        else:
            mid = best_ask

        ts = state.timestamp
        if (ts >= 50 * 100):
            last_prices = prices[-50:]
            x = np.arange(len(last_prices))
            slope, intercept = np.polyfit(x, last_prices, 1)
            predicted = slope * x + intercept
            ss_res = np.sum((last_prices - predicted) ** 2)
            ss_tot = np.sum((last_prices - np.mean(last_prices)) ** 2)
            r_squared = 1 - ss_res / ss_tot
            if (r_squared >= 0.2):
                positive = (slope >= 0)
            else:
                positive = None

        base = None
        if ((positive != None) and  positive):
            current_base = mid - (ts / 1000)

            prev_base = prev_trader_data.get("current_base")
            if prev_base is not None:
                alpha = 0.05
                base = alpha * current_base + (1 - alpha) * prev_base
            else:
                base = current_base

            fair = base + ts / 1000.0
            spread = 6 + (ts / 100.0)

            buy_room = self.mm_long_qty - position
            for ask_px, ask_vol in asks:
                if buy_room <= 0:
                    break
                if ask_px <= fair + spread:
                    qty = min(-ask_vol, buy_room)
                    if qty > 0:
                        orders.append(Order(product, ask_px, qty))
                        position += qty
                        buy_room -= qty

            if buy_room > 0:
                bid_px = int(round(fair))
                orders.append(Order(product, bid_px, buy_room))

        elif ((positive != None) and not positive):
            current_base = mid + (ts / 1000)

            prev_base = prev_trader_data.get("current_base")
            if prev_base is not None:
                alpha = 0.05
                base = alpha * current_base + (1 - alpha) * prev_base
            else:
                base = current_base

            fair = base - ts / 1000.0

            sell_room = self.mm_long_qty + position
            for buy_px, buy_vol in bids:
                if sell_room <= 0:
                    break
                if buy_px >= fair - 10:
                    qty = -min(buy_vol, sell_room)
                    if qty < 0:
                        orders.append(Order(product, buy_px, qty))
                        position += qty
                        sell_room -= qty

            if sell_room > 0:
                ask_px = int(round(fair))
                orders.append(Order(product, ask_px, sell_room))

        else:
            orders.append(Order(product, mid, -position))

        # result : product -> List[orders]
        result = {product: [] for product in POSITION_LIMITS}
        result[product] = orders
        trader_data["last_best_bid"] = best_bid
        trader_data["last_best_ask"] = best_ask
        trader_data['last_deepest_bid'] = deepest_bid
        trader_data['last_deepest_ask'] = deepest_ask
        trader_data["base"] = base if base else prev_trader_data.get("base")
        prices.append(wall_mid)
        trader_data["prices"] = prices[-50:]

        return result, 0, json.dumps(trader_data)



# ======================
# PEPPER END
# ======================


class Trader:
    def bid(self):
        return 3500

    def run(self, state):
        result      = {product: [] for product in POSITION_LIMITS}
        trader_data = ""
        traders     = [
            SimpleAshTrader(mm_qty=20, mm_spread=1),
            SimplePepperTrader(mm_qty=10, mm_long_spread=1, mm_short_spread=1, mm_long_qty=80),
        ]
        algo_fingerprint = ""
        trader_data = dict()
        for trader in traders:
            algo_fingerprint += str(trader)
            res, _, data = trader.run(state)
            for product, orders in res.items():
                result[product].extend(orders)
            data = json.loads(data)
            trader_data[trader.product] = data

        return result, 0, json.dumps(trader_data)