from datamodel import OrderDepth, TradingState, Order
from typing import List
import json


class Trader:
    ACO_LIMIT = 50   # ASH_COATED_OSMIUM position limit
    IPR_LIMIT = 80   # INTARIAN_PEPPER_ROOT position limit

    def run(self, state: TradingState):
        result = {}

        try:
            data = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        for product, order_depth in state.order_depths.items():
            if product == "ASH_COATED_OSMIUM":
                result[product] = self.trade_aco(order_depth, state.position.get(product, 0), data)
            elif product == "INTARIAN_PEPPER_ROOT":
                result[product] = self.trade_ipr(order_depth, state.position.get(product, 0))

        return result, 0, json.dumps(data)

    def trade_aco(self, order_depth: OrderDepth, pos: int, data: dict) -> List[Order]:
        """
        ASH_COATED_OSMIUM: mean-reverts tightly around 10000 (stdev ~5).

        Strategy:
        - EWMA fair (alpha=0.05, clamped ±7): shifts quotes with short-term drift,
          selling into peaks and buying into troughs.
        - Take threshold ±2 from dynamic fair (neutral) or ±1 (rebalancing when |pos|>1).
        - Passive: best_bid+1 / best_ask-1, capped at fair±1.
        """
        orders: List[Order] = []
        STATIC_FAIR = 10000
        limit = self.ACO_LIMIT
        INVENTORY_BIAS = 1

        best_bid = max(order_depth.buy_orders) if order_depth.buy_orders else STATIC_FAIR - 8
        best_ask = min(order_depth.sell_orders) if order_depth.sell_orders else STATIC_FAIR + 8

        # EWMA fair value
        mid = (best_bid + best_ask) / 2
        ewma = data.get("aco_ewma", STATIC_FAIR)
        ewma = 0.05 * mid + 0.95 * ewma
        fair = max(STATIC_FAIR - 7, min(STATIC_FAIR + 7, round(ewma)))
        data["aco_ewma"] = ewma

        take_buy  = fair - 1 if pos < -INVENTORY_BIAS else fair - 2
        take_sell = fair + 1 if pos >  INVENTORY_BIAS else fair + 2

        for ask in sorted(order_depth.sell_orders):
            if ask > take_buy:
                break
            vol = min(-order_depth.sell_orders[ask], limit - pos)
            if vol > 0:
                orders.append(Order("ASH_COATED_OSMIUM", ask, vol))
                pos += vol

        for bid in sorted(order_depth.buy_orders, reverse=True):
            if bid < take_sell:
                break
            vol = min(order_depth.buy_orders[bid], limit + pos)
            if vol > 0:
                orders.append(Order("ASH_COATED_OSMIUM", bid, -vol))
                pos -= vol

        buy_price = min(best_bid + 1, fair - 1)
        sell_price = max(best_ask - 1, fair + 1)

        if buy_price < sell_price:
            buy_qty = limit - pos
            sell_qty = limit + pos
            if buy_qty > 0:
                orders.append(Order("ASH_COATED_OSMIUM", buy_price, buy_qty))
            if sell_qty > 0:
                orders.append(Order("ASH_COATED_OSMIUM", sell_price, -sell_qty))

        return orders

    def trade_ipr(self, order_depth: OrderDepth, pos: int) -> List[Order]:
        """
        INTARIAN_PEPPER_ROOT: perfectly linear uptrend, +0.001 per timestamp.
        Strategy: always hold maximum long position.
        """
        orders: List[Order] = []
        limit = self.IPR_LIMIT

        for ask in sorted(order_depth.sell_orders):
            remaining = limit - pos
            if remaining <= 0:
                break
            vol = min(-order_depth.sell_orders[ask], remaining)
            if vol > 0:
                orders.append(Order("INTARIAN_PEPPER_ROOT", ask, vol))
                pos += vol

        return orders