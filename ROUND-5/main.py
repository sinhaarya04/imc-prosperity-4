"""
IMC Prosperity Round 5 — "The Final Stretch"
============================================

Strategy: trend-filtered passive market making across a curated universe
of 30 products. We JOIN the L1 quote on both sides for a fixed quantity,
but we DROP the side that the recent 5-tick price move is running into
(to avoid adverse selection from products with strong drift).

DESIGN NOTES (from full backtest of Day 2-4 data):
  - Naive "join L1 both sides" loses money on ~half the products because
    those products have meaningful intraday drift and we get adversely
    selected (we keep buying as price falls / selling as price rises).
  - Adding a short-term return filter (skip the bid when last 5-tick
    return < -threshold; skip the ask when last 5-tick return > +threshold)
    flips a lot of losers into winners and roughly TRIPLES total P&L.
  - Restricting to the curated universe of 30 robustly-winning products
    (selected by backtesting on Day 2 and validated on Days 3-4)
    gives ~25k seashells/day in simulation.
  - Trade counterparties (buyer/seller) are NaN in this round's trades
    file, so no counterparty signal exists. We ignore that channel entirely.
  - The mid-sum-=-50000 PEBBLES "arb" is NOT a real arb — the sum
    deviations are spread quantization artifacts, the round-trip cost of
    crossing all 5 spreads is ~64 ticks while the deviations only reach
    ±18. We therefore do NOT trade that explicitly; passive MM on PEBBLES_M
    and PEBBLES_XL captures their natural mean-reversion through normal
    market making instead.

PER-PRODUCT NOTES:
  - We only quote products whose L1 spread is ≥ 4 ticks (no room to make
    money in tighter books).
  - We use 2-unit quotes per side; max position cap is 10 (round rule).
  - When inventory leans (>5 long or <-5 short), we throttle the quote
    on the entrenching side to bias unwind.
  - We avoid quoting both sides if recent 5-tick return exceeds a
    threshold, scaled by spread.

The Trader.run() interface follows the Prosperity datamodel.
"""

import json
from collections import deque
from datamodel import Order


POSITION_LIMIT = 10  # rule: ±10 per product

# Curated universe: products that won robustly in Day 2 → Day 3-4 validation.
# Built by backtesting trend-filtered MM on Day 2 alone, keeping the top 30
# names, and verifying their out-of-sample (Day 3-4) performance.
ACTIVE_PRODUCTS = [
    "GALAXY_SOUNDS_SOLAR_WINDS",
    "MICROCHIP_CIRCLE", "MICROCHIP_OVAL", "MICROCHIP_RECTANGLE",
    "MICROCHIP_SQUARE", "MICROCHIP_TRIANGLE",
    "OXYGEN_SHAKE_CHOCOLATE", "OXYGEN_SHAKE_EVENING_BREATH",
    "OXYGEN_SHAKE_MORNING_BREATH",
    "PANEL_1X2", "PANEL_1X4", "PANEL_4X4",
    "PEBBLES_M", "PEBBLES_XL",
    "ROBOT_IRONING", "ROBOT_MOPPING", "ROBOT_VACUUMING",
    "SLEEP_POD_NYLON",
    "SNACKPACK_CHOCOLATE", "SNACKPACK_PISTACHIO", "SNACKPACK_RASPBERRY",
    "SNACKPACK_STRAWBERRY", "SNACKPACK_VANILLA",
    "TRANSLATOR_ASTRO_BLACK", "TRANSLATOR_ECLIPSE_CHARCOAL",
    "TRANSLATOR_GRAPHITE_MIST", "TRANSLATOR_SPACE_GRAY",
    "UV_VISOR_AMBER", "UV_VISOR_ORANGE", "UV_VISOR_RED",
]

# All 50 products (kept to ensure result dict has every key, harmless empty)
ALL_PRODUCTS = ACTIVE_PRODUCTS + [
    "GALAXY_SOUNDS_BLACK_HOLES", "GALAXY_SOUNDS_DARK_MATTER",
    "GALAXY_SOUNDS_PLANETARY_RINGS", "GALAXY_SOUNDS_SOLAR_FLAMES",
    "OXYGEN_SHAKE_GARLIC", "OXYGEN_SHAKE_MINT",
    "PANEL_2X2", "PANEL_2X4",
    "PEBBLES_L", "PEBBLES_S", "PEBBLES_XS",
    "ROBOT_DISHES", "ROBOT_LAUNDRY",
    "SLEEP_POD_COTTON", "SLEEP_POD_LAMB_WOOL", "SLEEP_POD_POLYESTER",
    "SLEEP_POD_SUEDE",
    "TRANSLATOR_VOID_BLUE",
    "UV_VISOR_MAGENTA", "UV_VISOR_YELLOW",
]


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def safe_json_loads(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def best_bid_ask(depth):
    if not depth.buy_orders or not depth.sell_orders:
        return None, None
    return max(depth.buy_orders.keys()), min(depth.sell_orders.keys())


# ─────────────────────────────────────────────────────────────────────────────
# ORDER MANAGER (per-product, per-tick)
# ─────────────────────────────────────────────────────────────────────────────
class OrderManager:
    """Tracks running position so we never exceed the ±10 limit even when
    multiple sub-strategies submit orders for the same product."""

    def __init__(self, product, start_position, limit=POSITION_LIMIT):
        self.product = product
        self.position = start_position
        self.limit = limit
        self.orders = []

    def buy(self, price, qty):
        room = max(0, self.limit - self.position)
        q = min(int(qty), room)
        if q > 0:
            self.orders.append(Order(self.product, int(price), q))
            self.position += q
        return q

    def sell(self, price, qty):
        room = max(0, self.limit + self.position)
        q = min(int(qty), room)
        if q > 0:
            self.orders.append(Order(self.product, int(price), -q))
            self.position -= q
        return q


# ─────────────────────────────────────────────────────────────────────────────
# TREND-FILTERED PASSIVE MARKET MAKER
# ─────────────────────────────────────────────────────────────────────────────
class TrendFilteredMM:
    """Per-product passive market maker.

    For each tick we:
      1. Read the L1 bid (BB) and L1 ask (BA).
      2. Skip the product if spread < MIN_SPREAD (no edge to extract).
      3. Compute the trailing 5-tick mid return (`ret_5`) using state
         carried in traderData.
      4. Set BID_QTY / ASK_QTY based on:
          - ret_5 strongly negative → drop the BID (price falling, we'd
            be adversely picked off as it keeps falling).
          - ret_5 strongly positive → drop the ASK.
          - inventory leaning long → reduce BID_QTY, ensure ASK_QTY.
          - inventory leaning short → reduce ASK_QTY, ensure BID_QTY.
      5. JOIN the L1 quote (price = BB on bid, BA on ask). We don't try
         to undercut by 1 tick; the simulation showed 0% of trades happen
         inside the spread so undercutting yields no fills.
      6. Stop entirely (qty=0 on the entering side) if we are at the cap.
    """

    QTY = 2
    MAX_POSITION = 10
    LEAN_POSITION = 5      # at |pos| > this, throttle one side
    MIN_SPREAD = 4         # don't bother making in tighter books
    HISTORY_LEN = 6        # need 6 mids to compute 5-tick return

    def run(self, state, managers, prev_data):
        new_data = {}
        for product in ACTIVE_PRODUCTS:
            if product not in state.order_depths:
                continue
            depth = state.order_depths[product]
            bb, ba = best_bid_ask(depth)
            if bb is None:
                continue
            spread = ba - bb
            mid = (bb + ba) / 2.0

            # Roll mid history forward (carried in traderData)
            hist = prev_data.get(product, [])
            hist = list(hist) + [mid]
            if len(hist) > self.HISTORY_LEN:
                hist = hist[-self.HISTORY_LEN:]
            new_data[product] = hist

            if spread < self.MIN_SPREAD:
                continue

            # 5-tick return (need at least 6 mids in history to be meaningful)
            ret_5 = (mid - hist[0]) if len(hist) >= 2 else 0.0
            # Trend threshold scales with spread, with a sensible floor
            trend_threshold = max(spread * 0.5, 5.0)

            m = managers[product]
            pos = m.position

            bid_qty = self.QTY
            ask_qty = self.QTY

            # Trend filter — skip the side facing into the trend
            if ret_5 < -trend_threshold:
                bid_qty = 0
            if ret_5 > trend_threshold:
                ask_qty = 0

            # Inventory throttling
            if pos >= self.MAX_POSITION:
                bid_qty = 0
            if pos <= -self.MAX_POSITION:
                ask_qty = 0
            if pos > self.LEAN_POSITION:
                bid_qty = max(0, bid_qty - 1)
            if pos < -self.LEAN_POSITION:
                ask_qty = max(0, ask_qty - 1)

            # Place orders by joining the L1 quote
            if bid_qty > 0:
                m.buy(bb, bid_qty)
            if ask_qty > 0:
                m.sell(ba, ask_qty)

        return new_data


# ─────────────────────────────────────────────────────────────────────────────
# INVENTORY UNWIND (defensive)
# ─────────────────────────────────────────────────────────────────────────────
class InventoryUnwind:
    """If for any product we're stuck at or near the position limit and the
    MM throttle hasn't cleared us, send a 1-unit aggressive order to cross
    the spread and reduce inventory. Cheap insurance against pinning."""

    DANGER_LEVEL = 9   # we are this close to ±10
    UNWIND_QTY = 1

    def run(self, state, managers):
        for product, m in managers.items():
            if product not in state.order_depths:
                continue
            depth = state.order_depths[product]
            bb, ba = best_bid_ask(depth)
            if bb is None:
                continue
            if m.position >= self.DANGER_LEVEL:
                # crossing-the-spread sell at best bid
                m.sell(bb, self.UNWIND_QTY)
            elif m.position <= -self.DANGER_LEVEL:
                m.buy(ba, self.UNWIND_QTY)


# ─────────────────────────────────────────────────────────────────────────────
# TRADER (entry point)
# ─────────────────────────────────────────────────────────────────────────────
class Trader:
    def __init__(self):
        self.mm = TrendFilteredMM()
        self.unwind = InventoryUnwind()

    def run(self, state):
        result = {p: [] for p in ALL_PRODUCTS}

        prev = safe_json_loads(state.traderData)
        prev_mids = prev.get("mids", {})

        # Build managers for every product
        managers = {}
        for p in ALL_PRODUCTS:
            pos = state.position.get(p, 0)
            managers[p] = OrderManager(p, pos)

        # Strategy 1: trend-filtered MM
        new_mids = self.mm.run(state, managers, prev_mids)

        # Strategy 2: defensive inventory unwind (only fires if pinned)
        self.unwind.run(state, managers)

        # Collect orders
        for p, m in managers.items():
            if m.orders:
                result[p] = m.orders

        new_data = {"mids": new_mids}

        return result, 0, json.dumps(new_data)