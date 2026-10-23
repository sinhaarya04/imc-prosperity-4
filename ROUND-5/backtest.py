"""
Backtester for IMC Prosperity Round 5 Strategy
Simulates the trader against historical price/trade data.
"""
import pandas as pd
import numpy as np
import json
import math
import warnings
warnings.filterwarnings("ignore")

# ─── Config ───
DATA = "ROUND_5/"
LIMIT = 10
DAYS = [2, 3, 4]

# ─── Trader parameters (mirrors trader.py) ───
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

EMA_SLOW_ALPHA = 0.002
EMA_FAST_ALPHA = 0.02
W_XIRECS      = 0.45
W_TREND        = 3.0
W_PAIR         = 2.0
W_MEAN_REV     = 1.0
INV_PENALTY    = 1.5


def get_category(p):
    for cat in ['GALAXY_SOUNDS', 'MICROCHIP', 'OXYGEN_SHAKE', 'PANEL', 'PEBBLES',
                'ROBOT', 'SLEEP_POD', 'SNACKPACK', 'TRANSLATOR', 'UV_VISOR']:
        if p.startswith(cat):
            return cat
    return 'OTHER'


def run_backtest():
    # ─── Load data ───
    print("Loading data...")
    prices = pd.concat([
        pd.read_csv(f'{DATA}prices_round_5_day_{d}.csv', sep=';') for d in DAYS
    ], ignore_index=True)
    trades = pd.concat([
        pd.read_csv(f'{DATA}trades_round_5_day_{d}.csv', sep=';').assign(day=d) for d in DAYS
    ], ignore_index=True)
    prices['global_ts'] = (prices['day'] - 2) * 1_000_000 + prices['timestamp']
    trades['global_ts'] = (trades['day'] - 2) * 1_000_000 + trades['timestamp']

    products = sorted(prices['product'].unique())
    timestamps = sorted(prices['global_ts'].unique())
    print(f"Products: {len(products)}, Timestamps: {len(timestamps)}")

    # ─── State ───
    ema_slow = {}
    ema_fast = {}
    xirecs_prices = {}
    prev_mid = {}
    positions = {p: 0 for p in products}
    cash = {p: 0.0 for p in products}
    trade_count = {p: 0 for p in products}

    # Pre-index data for speed
    print("Indexing data...")
    price_idx = {}
    for _, row in prices.iterrows():
        ts = row['global_ts']
        prod = row['product']
        if ts not in price_idx:
            price_idx[ts] = {}
        price_idx[ts][prod] = row

    trade_idx = {}
    for _, row in trades.iterrows():
        ts = row['global_ts']
        prod = row['symbol']
        if ts not in trade_idx:
            trade_idx[ts] = {}
        trade_idx[ts][prod] = row['price']

    print(f"Running backtest across {len(timestamps)} ticks...")

    tick_count = 0
    for ts in timestamps:
        tick_count += 1
        if tick_count % 50000 == 0:
            total_pnl = sum(
                cash[p] + positions[p] * prev_mid.get(p, 0) for p in products
            )
            print(f"  Tick {tick_count}/{len(timestamps)} | PnL so far: {total_pnl:,.0f}")

        rows = price_idx.get(ts, {})
        xt = trade_idx.get(ts, {})

        # Update XIRECS
        for prod, xp in xt.items():
            xirecs_prices[prod] = xp

        # Collect mids
        mids = {}
        book = {}
        for prod, row in rows.items():
            bid1 = row['bid_price_1']
            ask1 = row['ask_price_1']
            bv1  = row['bid_volume_1']
            av1  = row['ask_volume_1']
            if pd.notna(bid1) and pd.notna(ask1) and bid1 > 0 and ask1 > 0:
                mid = (bid1 + ask1) / 2.0
                mids[prod] = mid
                book[prod] = {
                    'bids': {},
                    'asks': {},
                }
                # Level 1
                book[prod]['bids'][int(bid1)] = int(bv1)
                book[prod]['asks'][int(ask1)] = int(av1)
                # Level 2
                if pd.notna(row['bid_price_2']) and row['bid_price_2'] > 0:
                    book[prod]['bids'][int(row['bid_price_2'])] = int(row['bid_volume_2'])
                if pd.notna(row['ask_price_2']) and row['ask_price_2'] > 0:
                    book[prod]['asks'][int(row['ask_price_2'])] = int(row['ask_volume_2'])
                # Level 3
                if pd.notna(row.get('bid_price_3', np.nan)) and row.get('bid_price_3', 0) > 0:
                    book[prod]['bids'][int(row['bid_price_3'])] = int(row['bid_volume_3'])
                if pd.notna(row.get('ask_price_3', np.nan)) and row.get('ask_price_3', 0) > 0:
                    book[prod]['asks'][int(row['ask_price_3'])] = int(row['ask_volume_3'])

                # Update EMAs
                if prod in ema_slow:
                    ema_slow[prod] += EMA_SLOW_ALPHA * (mid - ema_slow[prod])
                    ema_fast[prod] += EMA_FAST_ALPHA * (mid - ema_fast[prod])
                else:
                    ema_slow[prod] = mid
                    ema_fast[prod] = mid

        # Generate orders and simulate fills for each product
        for prod in mids:
            if prod not in book:
                continue

            pos = positions[prod]
            mid = mids[prod]
            bids = book[prod]['bids']
            asks = book[prod]['asks']
            if not bids or not asks:
                continue
            bbid = max(bids)
            bask = min(asks)
            spread = bask - bbid

            # ── Fair value ──
            fv = mid

            # XIRECS
            if prod in xirecs_prices:
                fv = (1.0 - W_XIRECS) * fv + W_XIRECS * xirecs_prices[prod]

            # EMA trend
            if prod in ema_slow and ema_slow[prod] > 0:
                trend = (ema_fast[prod] - ema_slow[prod]) / ema_slow[prod]
                fv += trend * mid * W_TREND

            # Pair signal
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
                    fv += pair_adj * mid * W_PAIR

            # Mean reversion
            if prod in MEAN_REV and prod in prev_mid and prev_mid[prod] > 0:
                last_ret = (mid - prev_mid[prod]) / prev_mid[prod]
                mr_signal = -last_ret * MEAN_REV[prod]
                fv += mr_signal * mid * W_MEAN_REV

            # Inventory
            fv -= pos * INV_PENALTY

            # ── Order generation & fill simulation ──
            buy_cap = LIMIT - pos
            sell_cap = LIMIT + pos

            # Aggressive buys: take asks below FV
            for ask_px in sorted(asks):
                if ask_px < fv and buy_cap > 0:
                    vol = min(abs(asks[ask_px]), buy_cap)
                    if vol > 0:
                        cash[prod] -= ask_px * vol
                        positions[prod] += vol
                        pos += vol
                        buy_cap -= vol
                        trade_count[prod] += 1

            # Aggressive sells: take bids above FV
            for bid_px in sorted(bids, reverse=True):
                if bid_px > fv and sell_cap > 0:
                    vol = min(bids[bid_px], sell_cap)
                    if vol > 0:
                        cash[prod] += bid_px * vol
                        positions[prod] -= vol
                        pos -= vol
                        sell_cap -= vol
                        trade_count[prod] += 1

            # Passive orders: we model a 20% fill probability for resting orders
            edge = max(1, spread // 2)
            fill_prob = 0.15

            if buy_cap > 0 and np.random.random() < fill_prob:
                buy_px = int(fv) - edge
                if buy_px > 0:
                    vol = min(1, buy_cap)  # conservative: 1 unit passive
                    cash[prod] -= buy_px * vol
                    positions[prod] += vol
                    trade_count[prod] += 1

            sell_cap = LIMIT + positions[prod]
            if sell_cap > 0 and np.random.random() < fill_prob:
                sell_px = math.ceil(fv) + edge
                vol = min(1, sell_cap)
                cash[prod] += sell_px * vol
                positions[prod] -= vol
                trade_count[prod] += 1

        # Save prev mids
        prev_mid = dict(mids)

    # ─── Results ───
    print("\n" + "=" * 90)
    print("                   BACKTEST RESULTS — IMC Prosperity Round 5")
    print("=" * 90)

    results = []
    for prod in sorted(products):
        pos = positions[prod]
        mid = prev_mid.get(prod, 0)
        realized = cash[prod]
        unrealized = pos * mid
        total_pnl = realized + unrealized
        cat = get_category(prod)
        results.append({
            'product': prod,
            'category': cat,
            'final_pos': pos,
            'realized': realized,
            'unrealized': unrealized,
            'total_pnl': total_pnl,
            'trades': trade_count[prod],
        })

    df = pd.DataFrame(results).sort_values('total_pnl', ascending=False)

    print(f"\n{'Product':<42} {'Cat':<15} {'Pos':>5} {'Trades':>7} {'Realized':>12} {'Unrealized':>12} {'Total PnL':>12}")
    print("-" * 110)
    for _, r in df.iterrows():
        print(f"{r['product']:<42} {r['category']:<15} {r['final_pos']:>5} {r['trades']:>7} "
              f"{r['realized']:>12,.0f} {r['unrealized']:>12,.0f} {r['total_pnl']:>12,.0f}")

    print("-" * 110)
    total = df['total_pnl'].sum()
    total_trades = df['trades'].sum()
    print(f"{'TOTAL':<42} {'':<15} {'':<5} {total_trades:>7} "
          f"{df['realized'].sum():>12,.0f} {df['unrealized'].sum():>12,.0f} {total:>12,.0f}")

    print(f"\n{'Category':<20} {'PnL':>12} {'Trades':>10}")
    print("-" * 45)
    cat_summary = df.groupby('category').agg(
        pnl=('total_pnl', 'sum'), trades=('trades', 'sum')
    ).sort_values('pnl', ascending=False)
    for cat, row in cat_summary.iterrows():
        print(f"{cat:<20} {row['pnl']:>12,.0f} {row['trades']:>10,}")

    print(f"\n{'GRAND TOTAL PnL':>20}: {total:>12,.0f}")
    print(f"{'Total Trades':>20}: {total_trades:>12,}")

    return df


if __name__ == "__main__":
    df = run_backtest()
