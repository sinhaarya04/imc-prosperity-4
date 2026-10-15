import json
from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict

POSITION_LIMITS = {
    "INTARIAN_PEPPER_ROOT": 80,
    "ASH_COATED_OSMIUM": 80,
}

def clamp_qty(desired_qty, position, limit):
    if desired_qty > 0:
        return min(desired_qty, limit - position)
    elif desired_qty < 0:
        return max(desired_qty, -limit - position)
    return 0

class Trader:
    def run(self, state: TradingState):
        result = {p: [] for p in POSITION_LIMITS}
        product = "INTARIAN_PEPPER_ROOT"
        limit = POSITION_LIMITS[product]
        orders = []

        position = state.position.get(product, 0)
        depth = state.order_depths.get(product)
        if depth is None:
            return result, 0, ""

        bids = sorted(depth.buy_orders.items(), key=lambda x: x[0], reverse=True)
        asks = sorted(depth.sell_orders.items(), key=lambda x: x[0])

        if position < 80:
            # Hard buy all asks
            for ask_px, ask_vol in asks:
                buy_qty = clamp_qty(-ask_vol, position, limit)
                if buy_qty > 0:
                    orders.append(Order(product, ask_px, buy_qty))
                    position += buy_qty
        else:
            # MM: sell 10 at best_ask - 1
            if asks:
                sell_px = asks[0][0] - 1
                sell_qty = clamp_qty(-10, position, limit)
                if sell_qty < 0:
                    orders.append(Order(product, sell_px, sell_qty))

        result[product] = orders
        return result, 0, ""
