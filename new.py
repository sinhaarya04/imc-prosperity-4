import json
from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict

LIMITS = {"INTARIAN_PEPPER_ROOT": 80, "ASH_COATED_OSMIUM": 80}


def cap(qty, pos, lim):
    if qty > 0:
        return min(qty, lim - pos)
    if qty < 0:
        return max(qty, -lim - pos)
    return 0


class Trader:
    def run(self, state: TradingState):
        orders = {p: [] for p in LIMITS}
        prev = json.loads(state.traderData) if state.traderData else {}

        orders["INTARIAN_PEPPER_ROOT"] = self.trade_pepper(state, prev)
        orders["ASH_COATED_OSMIUM"], ash_state = self.trade_ash(state, prev)

        save = {"pepper_base": prev.get("pepper_base"), "ash": ash_state}

        p = "INTARIAN_PEPPER_ROOT"
        depth = state.order_depths.get(p)
        if depth:
            bk_bids = sorted(depth.buy_orders.items(), reverse=True)
            bk_asks = sorted(depth.sell_orders.items())
            if bk_bids and bk_asks:
                m = (bk_bids[0][0] + bk_asks[0][0]) / 2
                t = state.timestamp
                raw_base = m - t / 1000.0
                old = prev.get("pepper_base")
                save["pepper_base"] = 0.04 * raw_base + 0.96 * old if old else raw_base

        return orders, 0, json.dumps(save)

    def trade_pepper(self, state, prev):
        p = "INTARIAN_PEPPER_ROOT"
        lim = LIMITS[p]
        result = []
        pos = state.position.get(p, 0)
        depth = state.order_depths.get(p)
        if not depth:
            return result

        sells = sorted(depth.sell_orders.items())
        buys = sorted(depth.buy_orders.items(), reverse=True)

        if not sells and not buys:
            return result

        top_buy = buys[0][0] if buys else None
        low_sell = sells[0][0] if sells else None
        if top_buy and low_sell:
            ref = (top_buy + low_sell) / 2
        else:
            ref = top_buy or low_sell

        t = state.timestamp
        base = prev.get("pepper_base")
        if base is None:
            base = ref - t / 1000.0
        fv = base + t / 1000.0

        # Sweep all sells within threshold
        space = lim - pos
        for px, vol in sells:
            if space <= 0 or px > fv + 12:
                break
            take = min(-vol, space)
            if take > 0:
                result.append(Order(p, px, take))
                pos += take
                space -= take

        # Passive accumulation
        space = lim - pos
        if space > 0:
            price = int(round(fv))
            if top_buy:
                price = max(price, top_buy + 1)
                price = min(price, int(round(fv + 3)))
            result.append(Order(p, price, space))

        return result

    def trade_ash(self, state, prev):
        p = "ASH_COATED_OSMIUM"
        lim = LIMITS[p]
        result = []
        pos = state.position.get(p, 0)
        depth = state.order_depths.get(p)
        if not depth:
            return result, {}

        buys = sorted(depth.buy_orders.items(), reverse=True)
        sells = sorted(depth.sell_orders.items())

        ash_prev = prev.get("ash", {})
        old_bb = ash_prev.get("bb")
        old_ba = ash_prev.get("ba")
        old_db = ash_prev.get("db")
        old_da = ash_prev.get("da")
        old_mid = ash_prev.get("m")

        bb = buys[0][0] if buys else (old_bb or 10000)
        ba = sells[0][0] if sells else (old_ba or 10000)
        db = buys[-1][0] if buys and sells else (old_db or 9990)
        da = sells[-1][0] if buys and sells else (old_da or 10010)
        mid = round((db + da) / 2)

        # Reversal signal
        rev = 0
        if old_mid is not None:
            rev = -(mid - old_mid)

        new_state = {"bb": bb, "ba": ba, "db": db, "da": da, "m": mid}

        # Arb + flatten
        for i, (px, vol) in enumerate(buys):
            if px < mid:
                break
            if px == mid and pos <= 0:
                break
            sq = cap(-vol, pos, lim) if px > mid else max(-pos, -vol)
            if sq != 0:
                result.append(Order(p, px, sq))
                pos += sq
                buys[i] = (px, vol + sq)

        for i, (px, vol) in enumerate(sells):
            if px > mid:
                break
            if px == mid and pos >= 0:
                break
            bq = cap(-vol, pos, lim) if px < mid else min(-pos, -vol)
            if bq != 0:
                result.append(Order(p, px, bq))
                pos += bq
                sells[i] = (px, vol + bq)

        buys = [b for b in buys if b[1] > 0]
        sells = [s for s in sells if s[1] < 0]
        bb = buys[0][0] if buys else (old_bb or 10000)
        ba = sells[0][0] if sells else (old_ba or 10000)

        # Quoting with reversal bias
        b_adj = 1 if rev > 2 else 0
        a_adj = -1 if rev < -2 else 0

        bid_px = min(mid + b_adj, bb + 1)
        bid_q = cap(15, pos, lim)
        if bid_q > 0:
            result.append(Order(p, bid_px, bid_q))

        ask_px = max(mid + a_adj, ba - 1)
        ask_q = cap(-15, pos, lim)
        if ask_q < 0:
            result.append(Order(p, ask_px, ask_q))

        # Strong reversal directional bet
        if rev > 3 and sells:
            if sells[0][0] <= mid + 2:
                q = min(cap(10, pos, lim), -sells[0][1])
                if q > 0:
                    result.append(Order(p, sells[0][0], q))
        elif rev < -3 and buys:
            if buys[0][0] >= mid - 2:
                q = max(cap(-10, pos, lim), -buys[0][1])
                if q < 0:
                    result.append(Order(p, buys[0][0], q))

        return result, new_state
