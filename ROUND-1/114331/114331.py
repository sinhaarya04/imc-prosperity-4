import json
from datamodel import Order

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
    def __init__(self, mm_qty, mm_spread, inv_thresh):
        self.product    = "ASH_COATED_OSMIUM"
        self.mm_qty     = mm_qty
        self.mm_spread  = mm_spread
        self.inv_thresh = inv_thresh

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
        deepest_bid = bids[-1][0] if bids else (last_deepest_bid if last_deepest_bid else 9990.0)
        deepest_ask = asks[-1][0] if asks else (last_deepest_ask if last_deepest_ask else 10010.0)
        mid = round((deepest_bid + deepest_ask) / 2)

        for px, qty in bids:
            if px <= mid and not (px == mid and position > 0): #sell at fair value when we have a long position
                break
            if (px != mid): sell_qty = clamp_qty(-qty, position, limit)
            else : sell_qty = max(-position, -qty)  #make position -> 0
            if sell_qty != 0:
                orders.append(Order(product, px, sell_qty))
                position += sell_qty

        # Buy from any asks below theo
        for px, qty in asks:
            if px >= mid and not (px == mid and position < 0): #buy at fair value when we have a short position
                break
            if (px != mid): buy_qty = clamp_qty(-qty, position, limit)  # qty is negative in sell_orders
            else : buy_qty = min(-position, -qty) #make position -> 0
            if buy_qty != 0:
                orders.append(Order(product, px, buy_qty))
                position += buy_qty


        # ── STEP 2: market-make ──
        mm_bid_px = None
        mm_ask_px = None
        if best_bid is not None:
            mm_bid_px = min(mid - self.mm_spread, best_bid + self.mm_spread)
            #mm_bid_px = min(mid - 1, best_bid)
            bid_qty = clamp_qty(self.mm_qty, position, limit)
            if bid_qty > 0:
                orders.append(Order(product, mm_bid_px, bid_qty))
                position += bid_qty

        if best_ask is not None:
            mm_ask_px = max(mid + self.mm_spread, best_ask - self.mm_spread)
            #mm_ask_px = max(mid + 1, best_ask)
            ask_qty = clamp_qty(-self.mm_qty, position, limit)
            if ask_qty < 0:
                orders.append(Order(product, mm_ask_px, ask_qty))
                position += ask_qty

        # ── STEP 3: clear inventory ──
        did_clear_inventory = False
        """
        if abs(position) > self.inv_thresh:
            orders.append(Order(product, mid, -position))
            did_clear_inventory = True
        """

        # ── LOGGING ──────────────────────────────────────────────
        # own_trades: product -> List[Trade] of trades that filled THIS tick
        # We count LAST TICK's fills
        mm_buy_filled  = 0
        mm_sell_filled = 0
        arb_filled     = 0

        if last_best_bid and last_best_ask:  # have to have OB last tick to count last tick's fills
            for trade in state.own_trades.get(product, []):
                last_mid = (last_best_bid + last_best_ask) // 2
                if last_mid:
                    if trade.buyer == "SUBMISSION":  # we bought
                        if trade.price < last_mid:  # arb buy (ask was below theo)
                            arb_filled += trade.quantity
                        else:  # MM bid filled
                            mm_buy_filled += trade.quantity
                    else:  # we sold (quantity is negative)
                        if trade.price > last_mid:  # arb sell (bid was above theo)
                            arb_filled += abs(trade.quantity)
                        else:  # MM ask filled
                            mm_sell_filled += abs(trade.quantity)

        # our tools parse this into csv, so make sure there's no commas in the
        # printed values
        # (col, type, value)
        vars = [
            ("t",                   "int", state.timestamp),
            ("product",             "str", product),
            ("pos",                 "int", state.position.get(product, 0)),
            ("arb_filled",          "int", arb_filled),
            ("mm_buy_filled",       "int", mm_buy_filled),
            ("mm_sell_filled",      "int", mm_sell_filled),
            ("mm_bid_px",           "int", mm_bid_px),
            ("mm_ask_px",           "int", mm_ask_px),
            ("did_clear_inventory", "int", int(did_clear_inventory)),
        ]
        types_str = ",".join(f'{col}={ty}' for col, ty, _ in vars)
        print(f"types->{types_str}")
        values_str = ",".join(f'{col}={value}' for col, _, value in vars)
        print(f"log_csv->{values_str}")

        # result : product -> List[orders]
        result = {product: [] for product in POSITION_LIMITS}
        result[product] = orders
        trader_data["last_best_bid"] = best_bid
        trader_data["last_best_ask"] = best_ask 
        trader_data["last_deepest_bid"] = deepest_bid
        trader_data["last_deepest_ask"] = deepest_ask 
        return result, 0, json.dumps(trader_data)


# ======================
# ASH END
# ======================

# ======================
# PEPPER BEGIN
# ======================

class SimplePepperTrader:
    def __init__(self, mm_qty, mm_spread):
        self.product        = "INTARIAN_PEPPER_ROOT"
        self.mm_qty         = mm_qty
        self.mm_spread      = mm_spread

    def __str__(self):
        return (
            "STT("
                f"mm_qty={self.mm_qty},"
                f"mm_spread={self.mm_spread}"
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
        last_best_bid = int(last_best_bid) if last_best_bid else None
        last_best_ask = int(last_best_ask) if last_best_ask else None

        best_bid = bids[0][0] if bids else (last_best_bid if last_best_bid else 10000)
        best_ask = asks[0][0] if asks else (last_best_ask if last_best_ask else 10000)

        mid = round((best_bid + best_ask) / 2)

        # ── STEP 1: take +EV arb trades ──
        # Sell into any bids above theo
        for px, qty in bids:
            if px <= mid:
                break
            sell_qty = clamp_qty(-qty, position, limit)
            if sell_qty != 0:
                orders.append(Order(product, px, sell_qty))
                position += sell_qty

        # Buy from any asks below theo
        for px, qty in asks:
            if px >= mid:
                break
            buy_qty = clamp_qty(-qty, position, limit)  # qty is negative in sell_orders
            if buy_qty != 0:
                orders.append(Order(product, px, buy_qty))
                position += buy_qty

        # ── STEP 2: market-make ──
        mm_bid_px = None
        mm_ask_px = None
        mm_bid_px = min(mid - self.mm_spread, best_bid + self.mm_spread)
        bid_qty = clamp_qty(self.mm_qty, position, limit)
        if bid_qty > 0:
            orders.append(Order(product, mm_bid_px, bid_qty))
            position += bid_qty

        if position > 60:
            mm_ask_px = max(mid + self.mm_spread, best_ask - self.mm_spread)
            ask_qty = clamp_qty(-self.mm_qty, position, limit)
            if ask_qty < 0:
                orders.append(Order(product, mm_ask_px, ask_qty))
                position += ask_qty

        # ── STEP 3: clear inventory ──
        """
        did_clear_inventory = False
        if not symmetric and abs(position) > 0 and last_best_bid and last_best_ask:
            clear_qty = clamp_qty(-position, position, limit)
            orders.append(Order(product, mid, clear_qty))
            did_clear_inventory = True
        """

        # ── LOGGING ──────────────────────────────────────────────
        # own_trades: product -> List[Trade] of trades that filled THIS tick
        # We count LAST TICK's fills
        mm_buy_filled  = 0
        mm_sell_filled = 0
        arb_filled     = 0

        if last_best_bid and last_best_ask:  # have to have OB last tick to count last tick's fills
            for trade in state.own_trades.get(product, []):
                last_mid = (last_best_bid + last_best_ask) // 2
                if last_mid:
                    if trade.buyer == "SUBMISSION":  # we bought
                        if trade.price < last_mid:  # arb buy (ask was below theo)
                            arb_filled += trade.quantity
                        else:  # MM bid filled
                            mm_buy_filled += trade.quantity
                    else:  # we sold (quantity is negative)
                        if trade.price > last_mid:  # arb sell (bid was above theo)
                            arb_filled += abs(trade.quantity)
                        else:  # MM ask filled
                            mm_sell_filled += abs(trade.quantity)

        # our tools parse this into csv, so make sure there's no commas in the
        # printed values
        # (col, type, value)
        vars = [
            ("t",                   "int", state.timestamp),
            ("product",             "str", product),
            ("pos",                 "int", state.position.get(product, 0)),
            ("arb_filled",          "int", arb_filled),
            ("mm_buy_filled",       "int", mm_buy_filled),
            ("mm_sell_filled",      "int", mm_sell_filled),
            ("mm_bid_px",           "int", mm_bid_px),
            ("mm_ask_px",           "int", mm_ask_px),
        ]
        types_str = ",".join(f'{col}={ty}' for col, ty, _ in vars)
        print(f"types->{types_str}")
        values_str = ",".join(f'{col}={value}' for col, _, value in vars)
        print(f"log_csv->{values_str}")

        # result : product -> List[orders]
        result = {product: [] for product in POSITION_LIMITS}
        result[product] = orders
        trader_data["last_best_bid"] = best_bid
        trader_data["last_best_ask"] = best_ask
        return result, 0, json.dumps(trader_data)


# ======================
# PEPPER END
# ======================


class Trader:
    def run(self, state):
        # result : product -> List[orders]
        result      = {product: [] for product in POSITION_LIMITS}
        trader_data = ""
        traders     = [
            SimpleAshTrader(mm_qty=20, mm_spread=1, inv_thresh=60),
            SimplePepperTrader(mm_qty=10, mm_spread=1),
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

        print(f"fingerprint->{algo_fingerprint}")

        return result, 0, json.dumps(trader_data)