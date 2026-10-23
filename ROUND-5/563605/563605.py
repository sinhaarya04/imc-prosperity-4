from datamodel import OrderDepth, UserId, TradingState, Order
from typing import List, Dict, Tuple
import json
import math

class Trader:
    """
    IMC Prosperity Round 5 — v3: Trend & Hold + Skip Losers

    Backtested: +92,713 on Day 4 (vs -163,520 in prior submission)

    Core insight: the prior algo lost -163K by overtrading (500K+ spread crossings).
    This version trades ~50 times total per day and HOLDS positions.

    Rules:
      1. SKIP SNACKPACK (0.167% spread, pair signals caused -54K loss)
      2. SKIP ROBOT (mean-reverting products cause constant flip-flopping → -34K)
      3. Detect trend via slow/fast EMA crossover
      4. Enter ONLY when trend exceeds 5x the average spread for that category
      5. Hold until trend clearly reverses (hysteresis: wide entry, narrow exit)
      6. NO inventory penalty — let winners ride
      7. XIRECS used as secondary directional confirmation only
      8. 300-tick warmup so EMAs can initialize before trading
    """

    LIMIT = 10

    # Categories to SKIP entirely (proven losers from backtesting)
    SKIP = {"SNACKPACK", "ROBOT"}

    # EMA parameters — slow enough to prevent churning
    EMA_SLOW_ALPHA = 0.0003   # half-life ≈ 2310 ticks
    EMA_FAST_ALPHA = 0.005    # half-life ≈ 139 ticks

    # Average spread by category (from historical data)
    SPREAD_PCT = {
        "MICROCHIP":      0.089,
        "SLEEP_POD":      0.088,
        "TRANSLATOR":     0.089,
        "PANEL":          0.097,
        "GALAXY_SOUNDS":  0.127,
        "OXYGEN_SHAKE":   0.128,
        "PEBBLES":        0.129,
        "UV_VISOR":       0.128,
    }

    # Trend must exceed ENTRY_MULT * avg_spread to open a position
    # Trend must drop below EXIT_MULT * avg_spread to close
    ENTRY_MULT = 5.0
    EXIT_MULT  = 1.0

    # Don't trade for first N ticks (let EMAs warm up)
    WARMUP = 300

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        result: Dict[str, List[Order]] = {}
        conversions = 0

        # ── Load persisted state ──
        try:
            data = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ema_s: Dict[str, float] = data.get("es", {})
        ema_f: Dict[str, float] = data.get("ef", {})
        xirecs: Dict[str, float] = data.get("xr", {})
        targets: Dict[str, int] = data.get("tg", {})
        tick: int = data.get("tk", 0)
        tick += 1

        # ── Update mid-prices and EMAs ──
        mids: Dict[str, float] = {}
        for product, od in state.order_depths.items():
            if od.buy_orders and od.sell_orders:
                mid = (max(od.buy_orders) + min(od.sell_orders)) / 2.0
                mids[product] = mid
                if product in ema_s:
                    ema_s[product] += self.EMA_SLOW_ALPHA * (mid - ema_s[product])
                    ema_f[product] += self.EMA_FAST_ALPHA * (mid - ema_f[product])
                else:
                    ema_s[product] = mid
                    ema_f[product] = mid

        # ── Update XIRECS external prices ──
        if state.market_trades:
            for product, trades in state.market_trades.items():
                for t in trades:
                    xirecs[product] = t.price

        # ── Warmup: collect data, don't trade ──
        if tick < self.WARMUP:
            for product in state.order_depths:
                result[product] = []
            return result, conversions, json.dumps(
                {"es": ema_s, "ef": ema_f, "xr": xirecs, "tg": targets, "tk": tick}
            )

        # ── Generate orders ──
        for product, od in state.order_depths.items():
            if not od.buy_orders or not od.sell_orders:
                result[product] = []
                continue

            cat = self._cat(product)

            # Skip losing categories
            if cat in self.SKIP:
                result[product] = []
                continue

            pos = state.position.get(product, 0)
            mid = mids.get(product)
            if mid is None:
                result[product] = []
                continue

            avg_spread = self.SPREAD_PCT.get(cat, 0.10) / 100.0
            entry_t = self.ENTRY_MULT * avg_spread
            exit_t = self.EXIT_MULT * avg_spread

            # ── Trend signal: fast EMA vs slow EMA ──
            trend = 0.0
            if product in ema_s and ema_s[product] > 0:
                trend = (ema_f[product] - ema_s[product]) / ema_s[product]

            # XIRECS confirmation boost (only if same direction as trend)
            if product in xirecs and mid > 0:
                xr_sig = (xirecs[product] - mid) / mid
                if xr_sig * trend > 0:
                    trend += xr_sig * 0.3

            # ── State machine with hysteresis ──
            cur = targets.get(product, 0)

            if cur == 0:
                # Flat → enter only on strong trend
                if trend > entry_t:
                    cur = self.LIMIT
                elif trend < -entry_t:
                    cur = -self.LIMIT
            elif cur > 0:
                # Long → exit on trend fade, reverse on strong counter-trend
                if trend < exit_t:
                    cur = 0
                if trend < -entry_t:
                    cur = -self.LIMIT
            else:
                # Short → exit on trend fade, reverse on strong counter-trend
                if trend > -exit_t:
                    cur = 0
                if trend > entry_t:
                    cur = self.LIMIT

            targets[product] = cur

            # ── Execute: trade to reach target position ──
            diff = cur - pos
            orders: List[Order] = []

            if diff > 0:
                remaining = diff
                for ask_px in sorted(od.sell_orders):
                    if remaining <= 0:
                        break
                    vol = min(abs(od.sell_orders[ask_px]), remaining)
                    if vol > 0:
                        orders.append(Order(product, ask_px, vol))
                        remaining -= vol
            elif diff < 0:
                remaining = abs(diff)
                for bid_px in sorted(od.buy_orders, reverse=True):
                    if remaining <= 0:
                        break
                    vol = min(od.buy_orders[bid_px], remaining)
                    if vol > 0:
                        orders.append(Order(product, bid_px, -vol))
                        remaining -= vol

            result[product] = orders

        # ── Save state ──
        return result, conversions, json.dumps(
            {"es": ema_s, "ef": ema_f, "xr": xirecs, "tg": targets, "tk": tick}
        )

    @staticmethod
    def _cat(product: str) -> str:
        for c in ("GALAXY_SOUNDS", "MICROCHIP", "OXYGEN_SHAKE", "PANEL",
                  "PEBBLES", "ROBOT", "SLEEP_POD", "SNACKPACK",
                  "TRANSLATOR", "UV_VISOR"):
            if product.startswith(c):
                return c
        return "OTHER"