import json
from datamodel import Order
import typing
import numpy as np
from typing import List, Dict

POSITION_LIMITS = {
    "HYDROGEL_PACK": 200,
    "VELVETFRUIT_EXTRACT": 200,
    "VEV_4000": 300,
    "VEV_4500": 300,
    "VEV_5000": 300,
    "VEV_5100": 300,
    "VEV_5200": 300,
    "VEV_5300": 300,
    "VEV_5400": 300,
    "VEV_5500": 300,
    "VEV_6000": 300,
    "VEV_6500": 300,
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


class HydrogelTrader:
    def __init__(self, mm_qty, mm_spread, inv_thresh, skew_factor=1.0):
        self.product    = "HYDROGEL_PACK"
        self.mm_qty     = mm_qty
        self.mm_spread  = mm_spread
        self.inv_thresh = inv_thresh
        self.skew_factor = skew_factor

    def __str__(self):
        return (
            "SET("
                f"mm_qty={self.mm_qty},"
                f"mm_spread={self.mm_spread},"
                f"inv_thresh={self.inv_thresh}"
            ")"
        )

    def run(self, state):
        orders              = []
        product             = self.product
        limit               = POSITION_LIMITS[product]
        trader_data         = dict()  # this gets string'd at the end
        prev_trader_data    = json.loads(state.traderData) if state.traderData else dict()
        prev_trader_data    = prev_trader_data.get(product, {})  # get the inner dict for this product, default empty dict

        # current position (default 0 if we haven't traded yet)
        position        = state.position.get(product, 0)
        if product not in state.order_depths:
            return orders, 0, trader_data
        depth           = state.order_depths[product]
        # buy_orders  : price -> qty  (positive qty)
        # sell_orders : price -> qty  (negative qty)
        bids            = sorted(depth.buy_orders.items(),
                                 key=lambda x: x[0],
                                 reverse=True)
        asks            = sorted(depth.sell_orders.items(),
                                 key=lambda x: x[0])
        
        last_best_bid = prev_trader_data.get("last_best_bid")
        last_best_ask = prev_trader_data.get("last_best_ask")
        last_deepest_bid = prev_trader_data.get("last_deepest_bid")
        last_deepest_ask = prev_trader_data.get("last_deepest_ask")
        last_best_bid = int(last_best_bid) if last_best_bid else None
        last_best_ask = int(last_best_ask) if last_best_ask else None
        last_deepest_bid = int(last_deepest_bid) if last_deepest_bid else None
        last_deepest_ask = int(last_deepest_ask) if last_deepest_ask else None

        best_bid = bids[0][0] if bids else (last_best_bid if last_best_bid else 10000)
        best_ask = asks[0][0] if asks else (last_best_ask if last_best_ask else 10000)
        bid_vol = abs(bids[0][1]) if bids else 1
        ask_vol = abs(asks[0][1]) if asks else 1
        
        # ── STEP 1: take +EV arb trades ──
        # Sell into any bids above theo
        #mid = round((best_bid + best_ask) / 2)
        
        #better mid calculation:
        deepest_bid = bids[-1][0] if bids and asks else (last_deepest_bid if last_deepest_bid else 9990.0)
        deepest_ask = asks[-1][0] if asks and bids else (last_deepest_ask if last_deepest_ask else 10010.0)
        #mid = round((deepest_bid + deepest_ask) / 2)
        mid = round(best_bid * ask_vol + best_ask * bid_vol / (ask_vol + bid_vol))

        # Sell into bids above mid (+ flatten at mid if long)
        for i, (bid_px, bid_vol) in enumerate(bids):
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
                bids[i] = (bid_px, bid_vol + sell_qty)  # sell_qty is negative

        # Buy from asks below mid (+ flatten at mid if short)
        for i, (ask_px, ask_vol) in enumerate(asks):
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
                asks[i] = (ask_px, ask_vol + buy_qty)  # buy_qty is positive, ask_vol is negative
        
        bids = [b for b in bids if b[1] > 0]
        asks = [a for a in asks if a[1] < 0]

        # ── STEP 2: market-make ──
        best_bid = bids[0][0] if bids else (last_best_bid if last_best_bid else 10000)
        best_ask = asks[0][0] if asks else (last_best_ask if last_best_ask else 10000)
        mm_bid_px = None
        mm_ask_px = None
        bid_qty = 0

        #bigger when short, ie -pos big number
        bid_spread = self.mm_spread + ( -position / self.inv_thresh) * self.skew_factor

        mm_bid_px = min(mid - 1, best_bid + round(bid_spread))
        bid_qty = clamp_qty(self.mm_qty, position, limit)
        if bid_qty > 0:
            orders.append(Order(product, mm_bid_px, bid_qty))

        #bigger when long, ie pos big number
        ask_spread = self.mm_spread - ( position / self.inv_thresh) * self.skew_factor

        mm_ask_px = max(mid + 1, best_ask - round(ask_spread))
        ask_qty = clamp_qty(-self.mm_qty, position, limit)
        if ask_qty < 0:
            orders.append(Order(product, mm_ask_px, ask_qty))


        # result : product -> List[orders]
        result = {product: [] for product in POSITION_LIMITS}
        result[product] = orders
        trader_data["last_best_bid"] = best_bid
        trader_data["last_best_ask"] = best_ask 
        trader_data["last_deepest_bid"] = deepest_bid
        trader_data["last_deepest_ask"] = deepest_ask 
        return result, 0, json.dumps(trader_data)
    

class VelvetFruitExtractTrader:
    def __init__(self, mm_qty, mm_spread, inv_thresh, skew_factor=1.0):
        self.product    = "VELVETFRUIT_EXTRACT"
        self.mm_qty     = mm_qty
        self.mm_spread  = mm_spread
        self.inv_thresh = inv_thresh
        self.skew_factor = skew_factor

    def __str__(self):
        return (
            "SET("
                f"mm_qty={self.mm_qty},"
                f"mm_spread={self.mm_spread},"
                f"inv_thresh={self.inv_thresh}"
            ")"
        )

    def run(self, state):
        orders              = []
        product             = self.product
        limit               = POSITION_LIMITS[product]
        trader_data         = dict()  # this gets string'd at the end
        prev_trader_data    = json.loads(state.traderData) if state.traderData else dict()
        prev_trader_data    = prev_trader_data.get(product, {})  # get the inner dict for this product, default empty dict

        # current position (default 0 if we haven't traded yet)
        position        = state.position.get(product, 0)
        if product not in state.order_depths:
            return orders, 0, trader_data
        depth           = state.order_depths[product]
        # buy_orders  : price -> qty  (positive qty)
        # sell_orders : price -> qty  (negative qty)
        bids            = sorted(depth.buy_orders.items(),
                                 key=lambda x: x[0],
                                 reverse=True)
        asks            = sorted(depth.sell_orders.items(),
                                 key=lambda x: x[0])
        
        last_best_bid = prev_trader_data.get("last_best_bid")
        last_best_ask = prev_trader_data.get("last_best_ask")
        last_deepest_bid = prev_trader_data.get("last_deepest_bid")
        last_deepest_ask = prev_trader_data.get("last_deepest_ask")
        last_best_bid = int(last_best_bid) if last_best_bid else None
        last_best_ask = int(last_best_ask) if last_best_ask else None
        last_deepest_bid = int(last_deepest_bid) if last_deepest_bid else None
        last_deepest_ask = int(last_deepest_ask) if last_deepest_ask else None

        best_bid = bids[0][0] if bids else (last_best_bid if last_best_bid else 10000)
        best_ask = asks[0][0] if asks else (last_best_ask if last_best_ask else 10000)
        
        # ── STEP 1: take +EV arb trades ──
        # Sell into any bids above theo
        #mid = round((best_bid + best_ask) / 2)
        #better mid calculation:
        deepest_bid = bids[-1][0] if bids and asks else (last_deepest_bid if last_deepest_bid else 9990.0)
        deepest_ask = asks[-1][0] if asks and bids else (last_deepest_ask if last_deepest_ask else 10010.0)
        mid = round((deepest_bid + deepest_ask) / 2)

        # Sell into bids above mid (+ flatten at mid if long)
        for i, (bid_px, bid_vol) in enumerate(bids):
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
                bids[i] = (bid_px, bid_vol + sell_qty)  # sell_qty is negative

        # Buy from asks below mid (+ flatten at mid if short)
        for i, (ask_px, ask_vol) in enumerate(asks):
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
                asks[i] = (ask_px, ask_vol + buy_qty)  # buy_qty is positive, ask_vol is negative
        
        bids = [b for b in bids if b[1] > 0]
        asks = [a for a in asks if a[1] < 0]

        # ── STEP 2: market-make ──
        best_bid = bids[0][0] if bids else (last_best_bid if last_best_bid else 10000)
        best_ask = asks[0][0] if asks else (last_best_ask if last_best_ask else 10000)
        mm_bid_px = None
        mm_ask_px = None
        bid_qty = 0

        #bigger when short, ie -pos big number
        bid_spread = self.mm_spread + ( -position / self.inv_thresh) * self.skew_factor

        mm_bid_px = min(mid - 1, best_bid + round(bid_spread))
        bid_qty = clamp_qty(self.mm_qty, position, self.inv_thresh)
        if bid_qty > 0:
            orders.append(Order(product, mm_bid_px, bid_qty))

        #bigger when long, ie pos big number
        ask_spread = self.mm_spread - ( position / self.inv_thresh) * self.skew_factor

        mm_ask_px = max(mid + 1, best_ask - round(ask_spread))
        ask_qty = clamp_qty(-self.mm_qty, position, self.inv_thresh)
        if ask_qty < 0:
            orders.append(Order(product, mm_ask_px, ask_qty))


        # result : product -> List[orders]
        result = {product: [] for product in POSITION_LIMITS}
        result[product] = orders
        trader_data["last_best_bid"] = best_bid
        trader_data["last_best_ask"] = best_ask 
        trader_data["last_deepest_bid"] = deepest_bid
        trader_data["last_deepest_ask"] = deepest_ask 
        return result, 0, json.dumps(trader_data)



class Trader:
    def run(self, state):
        # result : product -> List[orders]
        result      = {product: [] for product in POSITION_LIMITS}
        trader_data = ""
        traders     = [
            HydrogelTrader(mm_qty=20, mm_spread=1, inv_thresh=50, skew_factor=2.0)
            #VelvetFruitExtractTrader(mm_qty=20, mm_spread=1, inv_thresh=100, skew_factor=2.0)
        ]
        algo_fingerprint = ""
        trader_data = dict()  # product -> dict of whatever we want to carry forward; gets string'd at the end
        for trader in traders:
            algo_fingerprint += str(trader)  # for log identification
            res, _, data = trader.run(state)
            for product, orders in res.items():
                result[product].extend(orders)
            data = json.loads(data)
            trader_data[trader.product] = data  # store each trader's data under its product key

        #print(f"fingerprint->{algo_fingerprint}")

        return result, 0, json.dumps(trader_data)