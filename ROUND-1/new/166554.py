import json
from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict, Any

POSITION_LIMITS = {
    "INTARIAN_PEPPER_ROOT": 80,
    "ASH_COATED_OSMIUM": 80,
}


def clamp_qty(desired_qty: int, position: int, limit: int) -> int:
    if desired_qty > 0:
        return min(desired_qty, limit - position)
    elif desired_qty < 0:
        return max(desired_qty, -limit - position)
    return 0


# ============================================================
# INTARIAN_PEPPER_ROOT — Aggressive Trend Rider
#
# The data shows a near-perfect linear drift:
#   FV ≈ base_price + timestamp / 1000
# where base_price increases ~1,000 per day.
#
# Strategy: get max long (80 units) ASAP and hold.
# 80 units × 1,000 drift/day = ~80,000 profit/day.
# ============================================================

class PepperTrader:
    PRODUCT = "INTARIAN_PEPPER_ROOT"

    def run(self, state: TradingState, prev_data: dict) -> tuple:
        product = self.PRODUCT
        limit = POSITION_LIMITS[product]
        orders: List[Order] = []
        data = {}

        position = state.position.get(product, 0)
        depth = state.order_depths.get(product)
        if depth is None:
            return orders, data

        bids = sorted(depth.buy_orders.items(), key=lambda x: x[0], reverse=True)
        asks = sorted(depth.sell_orders.items(), key=lambda x: x[0])

        if not bids and not asks:
            return orders, data

        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None

        # Compute mid from available quotes
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2
        elif best_bid is not None:
            mid = best_bid
        else:
            mid = best_ask

        # Estimate the base price for this day from the linear model
        # FV = base + timestamp / 1000
        # base = mid - timestamp / 1000
        ts = state.timestamp
        current_base = mid - ts / 1000.0

        # Use EMA of base estimates for robustness
        prev_base = prev_data.get("base")
        if prev_base is not None:
            alpha = 0.05  # slow adaptation — base should be nearly constant within a day
            base = alpha * current_base + (1 - alpha) * prev_base
        else:
            base = current_base

        data["base"] = base

        # Fair value right now
        fair = base + ts / 1000.0

        # --- STEP 1: Aggressively BUY to get to max long ---
        # Take ANY ask at or below fair + 10 — the spread cost is trivial vs drift
        buy_room = limit - position
        for ask_px, ask_vol in asks:
            if buy_room <= 0:
                break
            if ask_px <= fair + 10:
                qty = min(-ask_vol, buy_room)  # ask_vol is negative
                if qty > 0:
                    orders.append(Order(product, ask_px, qty))
                    position += qty
                    buy_room -= qty

        # Post passive bid for remaining capacity at fair value
        # Penny-jump to maximize fill probability
        if buy_room > 0:
            bid_px = int(round(fair))
            if best_bid is not None:
                bid_px = max(bid_px, best_bid + 1)
                bid_px = min(bid_px, int(round(fair + 2)))
            orders.append(Order(product, bid_px, buy_room))

        # --- STEP 2: MM with spare capacity while filling ---
        # If not at max long, sell at fair + 4 to capture spread, re-buy next tick
        if position < limit:
            mm_sell_qty = min(20, position)
            if mm_sell_qty > 0:
                sell_room = limit + position
                for bid_px, bid_vol in bids:
                    if bid_px <= fair + 4:
                        break
                    if mm_sell_qty <= 0:
                        break
                    if sell_room <= 0:
                        break
                    qty = min(bid_vol, mm_sell_qty, sell_room)
                    if qty > 0:
                        orders.append(Order(product, bid_px, -qty))
                        position -= qty
                        mm_sell_qty -= qty
                        sell_room -= qty
        else:
            # At max long — only sell at extreme premium
            sell_room = limit + position
            for bid_px, bid_vol in bids:
                if bid_px <= fair + 5:
                    break
                if sell_room <= 0:
                    break
                qty = min(bid_vol, sell_room)
                if qty > 0:
                    orders.append(Order(product, bid_px, -qty))
                    position -= qty
                    sell_room -= qty

        return orders, data


# ============================================================
# ASH_COATED_OSMIUM — Mean-Reversion Market Maker
# (Mikhail's approach: dynamic mid, tight spread, flatten at mid)
#
# FV ≈ 10,000 but we use dynamic mid from deepest bid/ask.
# Tight spread (1 tick each side) to maximize fills.
# Flatten inventory at mid price for clean risk management.
# ============================================================

class AshTrader:
    PRODUCT = "ASH_COATED_OSMIUM"
    MM_QTY = 25
    MM_SPREAD = 1

    def run(self, state: TradingState, prev_data: dict) -> tuple:
        product = self.PRODUCT
        limit = POSITION_LIMITS[product]
        orders: List[Order] = []
        data = {}

        position = state.position.get(product, 0)
        depth = state.order_depths.get(product)
        if depth is None:
            return orders, data

        bids = sorted(depth.buy_orders.items(), key=lambda x: x[0], reverse=True)
        asks = sorted(depth.sell_orders.items(), key=lambda x: x[0])

        # Use previous tick's book data as fallback
        last_best_bid = prev_data.get("last_best_bid")
        last_best_ask = prev_data.get("last_best_ask")
        last_deepest_bid = prev_data.get("last_deepest_bid")
        last_deepest_ask = prev_data.get("last_deepest_ask")

        best_bid = bids[0][0] if bids else (last_best_bid if last_best_bid else 10000)
        best_ask = asks[0][0] if asks else (last_best_ask if last_best_ask else 10000)

        # Dynamic mid from deepest bid/ask — captures the NPC's "true" range
        deepest_bid = bids[-1][0] if bids and asks else (last_deepest_bid if last_deepest_bid else 9990)
        deepest_ask = asks[-1][0] if asks and bids else (last_deepest_ask if last_deepest_ask else 10010)
        mid = round((deepest_bid + deepest_ask) / 2)

        # Save book state for next tick
        data["last_best_bid"] = best_bid
        data["last_best_ask"] = best_ask
        data["last_deepest_bid"] = deepest_bid
        data["last_deepest_ask"] = deepest_ask

        # --- STEP 1: Arb layer — take mispriced orders ---

        # Sell into bids above mid (+ flatten at mid if long)
        for bid_px, bid_vol in bids:
            if bid_px < mid:
                break
            if bid_px == mid and position <= 0:
                break
            if bid_px > mid:
                sell_qty = clamp_qty(-bid_vol, position, limit)
            else:
                # At mid: only sell enough to flatten toward 0
                sell_qty = max(-position, -bid_vol)
            if sell_qty != 0:
                orders.append(Order(product, bid_px, sell_qty))
                position += sell_qty

        # Buy from asks below mid (+ flatten at mid if short)
        for ask_px, ask_vol in asks:
            if ask_px > mid:
                break
            if ask_px == mid and position >= 0:
                break
            if ask_px < mid:
                buy_qty = clamp_qty(-ask_vol, position, limit)
            else:
                # At mid: only buy enough to flatten toward 0
                buy_qty = min(-position, -ask_vol)
            if buy_qty != 0:
                orders.append(Order(product, ask_px, buy_qty))
                position += buy_qty

        # --- STEP 2: Market-make with tight spread ---
        mm_bid_px = min(mid - self.MM_SPREAD, best_bid + self.MM_SPREAD)
        bid_qty = clamp_qty(self.MM_QTY, position, limit)
        if bid_qty > 0:
            orders.append(Order(product, mm_bid_px, bid_qty))

        mm_ask_px = max(mid + self.MM_SPREAD, best_ask - self.MM_SPREAD)
        ask_qty = clamp_qty(-self.MM_QTY, position, limit)
        if ask_qty < 0:
            orders.append(Order(product, mm_ask_px, ask_qty))

        return orders, data


# ============================================================
# Orchestrator
# ============================================================

class Trader:
    def __init__(self):
        self.pepper = PepperTrader()
        self.ash = AshTrader()

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {p: [] for p in POSITION_LIMITS}

        # Load persistent state
        prev = json.loads(state.traderData) if state.traderData else {}

        # Run each sub-trader
        pepper_orders, pepper_data = self.pepper.run(state, prev.get("pepper", {}))
        ash_orders, ash_data = self.ash.run(state, prev.get("ash", {}))

        result["INTARIAN_PEPPER_ROOT"] = pepper_orders
        result["ASH_COATED_OSMIUM"] = ash_orders

        # Persist state
        trader_data = json.dumps({"pepper": pepper_data, "ash": ash_data})

        return result, 0, trader_data