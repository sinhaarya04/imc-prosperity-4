"""
Quick parameter sweep to find optimal settings.
Tests key parameters that drive PnL.
"""
import pandas as pd
import numpy as np
import json
import math
import warnings
warnings.filterwarnings("ignore")

DATA = "ROUND_5/"
LIMIT = 10
DAYS = [2, 3, 4]

PAIR_MAP = {
    "SNACKPACK_CHOCOLATE":  [("SNACKPACK_VANILLA",    -0.915)],
    "SNACKPACK_VANILLA":    [("SNACKPACK_CHOCOLATE",  -0.915)],
    "SNACKPACK_RASPBERRY":  [("SNACKPACK_STRAWBERRY", -0.923),
                             ("SNACKPACK_PISTACHIO",  -0.831)],
    "SNACKPACK_STRAWBERRY": [("SNACKPACK_RASPBERRY",  -0.923),
                             ("SNACKPACK_PISTACHIO",   0.913)],
    "SNACKPACK_PISTACHIO":  [("SNACKPACK_RASPBERRY",  -0.831),
                             ("SNACKPACK_STRAWBERRY",  0.913)],
    "PEBBLES_XL": [("PEBBLES_L",  -0.493), ("PEBBLES_M",  -0.506),
                   ("PEBBLES_S",  -0.483), ("PEBBLES_XS", -0.475)],
    "PEBBLES_L":  [("PEBBLES_XL", -0.493)],
    "PEBBLES_M":  [("PEBBLES_XL", -0.506)],
    "PEBBLES_S":  [("PEBBLES_XL", -0.483)],
    "PEBBLES_XS": [("PEBBLES_XL", -0.475)],
}

MEAN_REV = {
    "ROBOT_DISHES": 0.222,
    "ROBOT_IRONING": 0.121,
    "OXYGEN_SHAKE_EVENING_BREATH": 0.118,
    "OXYGEN_SHAKE_CHOCOLATE": 0.082,
}


def get_category(p):
    for cat in ['GALAXY_SOUNDS', 'MICROCHIP', 'OXYGEN_SHAKE', 'PANEL', 'PEBBLES',
                'ROBOT', 'SLEEP_POD', 'SNACKPACK', 'TRANSLATOR', 'UV_VISOR']:
        if p.startswith(cat):
            return cat
    return 'OTHER'


def run_fast_backtest(params):
    """Runs backtest with given params, returns total PnL (no printing)."""
    ema_slow_alpha = params['ema_slow']
    ema_fast_alpha = params['ema_fast']
    w_xirecs = params['w_xirecs']
    w_trend = params['w_trend']
    w_pair = params['w_pair']
    w_mr = params['w_mr']
    inv_pen = params['inv_pen']

    ema_slow = {}
    ema_fast = {}
    xirecs_prices = {}
    prev_mid = {}
    positions = {}
    cash = {}

    for ts in timestamps:
        rows = price_idx.get(ts, {})
        xt = trade_idx.get(ts, {})

        for prod, xp in xt.items():
            xirecs_prices[prod] = xp

        mids = {}
        book = {}
        for prod, row in rows.items():
            bid1, ask1 = row[0], row[1]
            bv1, av1 = row[2], row[3]
            if bid1 > 0 and ask1 > 0:
                mid = (bid1 + ask1) / 2.0
                mids[prod] = mid
                book[prod] = (
                    {int(bid1): int(bv1)},
                    {int(ask1): int(av1)},
                    int(ask1) - int(bid1)
                )
                # Add level 2 if present
                if row[4] > 0:
                    book[prod][0][int(row[4])] = int(row[5])
                if row[6] > 0:
                    book[prod][1][int(row[6])] = int(row[7])

                if prod in ema_slow:
                    ema_slow[prod] += ema_slow_alpha * (mid - ema_slow[prod])
                    ema_fast[prod] += ema_fast_alpha * (mid - ema_fast[prod])
                else:
                    ema_slow[prod] = mid
                    ema_fast[prod] = mid

        for prod in mids:
            if prod not in book:
                continue
            if prod not in positions:
                positions[prod] = 0
                cash[prod] = 0.0

            pos = positions[prod]
            mid = mids[prod]
            bids, asks, spread = book[prod]
            bbid = max(bids)
            bask = min(asks)

            fv = mid

            if prod in xirecs_prices:
                fv = (1.0 - w_xirecs) * fv + w_xirecs * xirecs_prices[prod]

            if prod in ema_slow and ema_slow[prod] > 0:
                trend = (ema_fast[prod] - ema_slow[prod]) / ema_slow[prod]
                fv += trend * mid * w_trend

            if prod in PAIR_MAP:
                pair_adj = 0.0
                cnt = 0
                for partner, corr in PAIR_MAP[prod]:
                    if partner in mids and partner in ema_slow and ema_slow[partner] > 0:
                        pdev = (mids[partner] - ema_slow[partner]) / ema_slow[partner]
                        pair_adj += corr * pdev
                        cnt += 1
                if cnt > 0:
                    pair_adj /= cnt
                    fv += pair_adj * mid * w_pair

            if prod in MEAN_REV and prod in prev_mid and prev_mid[prod] > 0:
                last_ret = (mid - prev_mid[prod]) / prev_mid[prod]
                fv += -last_ret * MEAN_REV[prod] * mid * w_mr

            fv -= pos * inv_pen

            buy_cap = LIMIT - pos
            sell_cap = LIMIT + pos

            for ask_px in sorted(asks):
                if ask_px < fv and buy_cap > 0:
                    vol = min(abs(asks[ask_px]), buy_cap)
                    if vol > 0:
                        cash[prod] -= ask_px * vol
                        positions[prod] += vol
                        pos += vol
                        buy_cap -= vol

            for bid_px in sorted(bids, reverse=True):
                if bid_px > fv and sell_cap > 0:
                    vol = min(bids[bid_px], sell_cap)
                    if vol > 0:
                        cash[prod] += bid_px * vol
                        positions[prod] -= vol
                        pos -= vol
                        sell_cap -= vol

        prev_mid = dict(mids)

    total_pnl = sum(cash[p] + positions[p] * prev_mid.get(p, 0) for p in positions)
    return total_pnl


# ─── Load data once ───
print("Loading data...")
prices = pd.concat([
    pd.read_csv(f'{DATA}prices_round_5_day_{d}.csv', sep=';') for d in DAYS
], ignore_index=True)
trades_df = pd.concat([
    pd.read_csv(f'{DATA}trades_round_5_day_{d}.csv', sep=';').assign(day=d) for d in DAYS
], ignore_index=True)
prices['global_ts'] = (prices['day'] - 2) * 1_000_000 + prices['timestamp']
trades_df['global_ts'] = (trades_df['day'] - 2) * 1_000_000 + trades_df['timestamp']

timestamps = sorted(prices['global_ts'].unique())

print("Indexing...")
price_idx = {}
for _, row in prices.iterrows():
    ts = row['global_ts']
    prod = row['product']
    if ts not in price_idx:
        price_idx[ts] = {}
    price_idx[ts][prod] = (
        row['bid_price_1'], row['ask_price_1'],
        row['bid_volume_1'], row['ask_volume_1'],
        row.get('bid_price_2', 0) if pd.notna(row.get('bid_price_2', np.nan)) else 0,
        row.get('bid_volume_2', 0) if pd.notna(row.get('bid_volume_2', np.nan)) else 0,
        row.get('ask_price_2', 0) if pd.notna(row.get('ask_price_2', np.nan)) else 0,
        row.get('ask_volume_2', 0) if pd.notna(row.get('ask_volume_2', np.nan)) else 0,
    )

trade_idx = {}
for _, row in trades_df.iterrows():
    ts = row['global_ts']
    prod = row['symbol']
    if ts not in trade_idx:
        trade_idx[ts] = {}
    trade_idx[ts][prod] = row['price']

# ─── Parameter sweep ───
print("\n=== PARAMETER SWEEP (aggressive orders only) ===\n")

configs = [
    {"name": "Baseline",        "ema_slow": 0.002, "ema_fast": 0.02,  "w_xirecs": 0.45, "w_trend": 3.0, "w_pair": 2.0, "w_mr": 1.0, "inv_pen": 1.5},
    {"name": "More XIRECS",     "ema_slow": 0.002, "ema_fast": 0.02,  "w_xirecs": 0.60, "w_trend": 3.0, "w_pair": 2.0, "w_mr": 1.0, "inv_pen": 1.5},
    {"name": "Less XIRECS",     "ema_slow": 0.002, "ema_fast": 0.02,  "w_xirecs": 0.30, "w_trend": 3.0, "w_pair": 2.0, "w_mr": 1.0, "inv_pen": 1.5},
    {"name": "More trend",      "ema_slow": 0.002, "ema_fast": 0.02,  "w_xirecs": 0.45, "w_trend": 5.0, "w_pair": 2.0, "w_mr": 1.0, "inv_pen": 1.5},
    {"name": "Less trend",      "ema_slow": 0.002, "ema_fast": 0.02,  "w_xirecs": 0.45, "w_trend": 1.5, "w_pair": 2.0, "w_mr": 1.0, "inv_pen": 1.5},
    {"name": "Faster EMA",      "ema_slow": 0.005, "ema_fast": 0.05,  "w_xirecs": 0.45, "w_trend": 3.0, "w_pair": 2.0, "w_mr": 1.0, "inv_pen": 1.5},
    {"name": "Slower EMA",      "ema_slow": 0.001, "ema_fast": 0.01,  "w_xirecs": 0.45, "w_trend": 3.0, "w_pair": 2.0, "w_mr": 1.0, "inv_pen": 1.5},
    {"name": "More pair",       "ema_slow": 0.002, "ema_fast": 0.02,  "w_xirecs": 0.45, "w_trend": 3.0, "w_pair": 4.0, "w_mr": 1.0, "inv_pen": 1.5},
    {"name": "Less pair",       "ema_slow": 0.002, "ema_fast": 0.02,  "w_xirecs": 0.45, "w_trend": 3.0, "w_pair": 1.0, "w_mr": 1.0, "inv_pen": 1.5},
    {"name": "High inv pen",    "ema_slow": 0.002, "ema_fast": 0.02,  "w_xirecs": 0.45, "w_trend": 3.0, "w_pair": 2.0, "w_mr": 1.0, "inv_pen": 3.0},
    {"name": "Low inv pen",     "ema_slow": 0.002, "ema_fast": 0.02,  "w_xirecs": 0.45, "w_trend": 3.0, "w_pair": 2.0, "w_mr": 1.0, "inv_pen": 0.5},
    {"name": "XIRECS=0.6+trend5","ema_slow": 0.002, "ema_fast": 0.02, "w_xirecs": 0.60, "w_trend": 5.0, "w_pair": 2.0, "w_mr": 1.0, "inv_pen": 1.5},
    {"name": "XIRECS=0.6+fast", "ema_slow": 0.005, "ema_fast": 0.05,  "w_xirecs": 0.60, "w_trend": 3.0, "w_pair": 2.0, "w_mr": 1.0, "inv_pen": 1.5},
    {"name": "Aggressive combo", "ema_slow": 0.003, "ema_fast": 0.03, "w_xirecs": 0.55, "w_trend": 4.0, "w_pair": 3.0, "w_mr": 1.5, "inv_pen": 1.0},
]

results = []
for cfg in configs:
    name = cfg.pop("name")
    pnl = run_fast_backtest(cfg)
    cfg["name"] = name
    results.append({"name": name, "pnl": pnl})
    print(f"  {name:<25} PnL = {pnl:>14,.0f}")

print("\n--- RANKED ---")
for r in sorted(results, key=lambda x: x['pnl'], reverse=True):
    print(f"  {r['name']:<25} PnL = {r['pnl']:>14,.0f}")
