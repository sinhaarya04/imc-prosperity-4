"""
Backtest v2: Trend & Hold strategy against Day 4 only (matching submission).
Uses the actual submission's Day 4 data from the activities log.
"""
import pandas as pd
import numpy as np
import json
import math
import warnings
warnings.filterwarnings("ignore")

DATA = "ROUND_5/"
LIMIT = 10
MAX_TIMESTAMP = 99900  # actual submission only uses ticks 0-99900 (1000 ticks/day)

# ── Strategy parameters (mirrors trader.py v2) ──
EMA_SLOW_ALPHA = 0.0003
EMA_FAST_ALPHA = 0.005
WARMUP_TICKS   = 300

SPREAD_PCT = {
    "ROBOT": 0.073, "MICROCHIP": 0.089, "SLEEP_POD": 0.088,
    "TRANSLATOR": 0.089, "PANEL": 0.097, "GALAXY_SOUNDS": 0.127,
    "OXYGEN_SHAKE": 0.128, "PEBBLES": 0.129, "UV_VISOR": 0.128,
    "SNACKPACK": 0.167,
}
ENTRY_MULT = 5.0
EXIT_MULT  = 1.0
SKIP_CATEGORIES = {"SNACKPACK", "ROBOT"}  # matches trader.py


def get_category(p):
    for c in SPREAD_PCT:
        if p.startswith(c):
            return c
    return "OTHER"


def run_backtest(entry_mult, exit_mult, ema_slow, ema_fast, warmup, day_list=None,
                 verbose=True):
    """Run backtest with given params. Returns (total_pnl, per_product_dict)."""

    if day_list is None:
        day_list = [4]  # Match submission: Day 4 only

    prices = pd.concat([
        pd.read_csv(f'{DATA}prices_round_5_day_{d}.csv', sep=';') for d in day_list
    ], ignore_index=True)
    trades_df = pd.concat([
        pd.read_csv(f'{DATA}trades_round_5_day_{d}.csv', sep=';').assign(day=d) for d in day_list
    ], ignore_index=True)

    # Filter to actual submission tick range (0-99900 per day)
    prices = prices[prices['timestamp'] <= MAX_TIMESTAMP].copy()
    trades_df = trades_df[trades_df['timestamp'] <= MAX_TIMESTAMP].copy()

    prices['global_ts'] = (prices['day'] - min(day_list)) * 1_000_000 + prices['timestamp']
    trades_df['global_ts'] = (trades_df['day'] - min(day_list)) * 1_000_000 + trades_df['timestamp']

    products = sorted(prices['product'].unique())
    timestamps = sorted(prices['global_ts'].unique())

    # Index data
    price_idx = {}
    for _, row in prices.iterrows():
        ts = row['global_ts']
        prod = row['product']
        if ts not in price_idx:
            price_idx[ts] = {}
        price_idx[ts][prod] = row

    trade_idx = {}
    for _, row in trades_df.iterrows():
        ts = row['global_ts']
        prod = row['symbol']
        if ts not in trade_idx:
            trade_idx[ts] = {}
        trade_idx[ts][prod] = row['price']

    # State
    ema_s = {}
    ema_f = {}
    xirecs = {}
    target = {}
    positions = {p: 0 for p in products}
    cash = {p: 0.0 for p in products}
    trade_count = {p: 0 for p in products}
    tick = 0

    for ts in timestamps:
        tick += 1
        rows = price_idx.get(ts, {})
        xt = trade_idx.get(ts, {})

        # Update XIRECS
        for prod, xp in xt.items():
            xirecs[prod] = xp

        # Update mids and EMAs
        mids = {}
        for prod, row in rows.items():
            bid1 = row['bid_price_1']
            ask1 = row['ask_price_1']
            if pd.notna(bid1) and pd.notna(ask1) and bid1 > 0 and ask1 > 0:
                mid = (bid1 + ask1) / 2.0
                mids[prod] = mid
                if prod in ema_s:
                    ema_s[prod] += ema_slow * (mid - ema_s[prod])
                    ema_f[prod] += ema_fast * (mid - ema_f[prod])
                else:
                    ema_s[prod] = mid
                    ema_f[prod] = mid

        # Warmup
        if tick < warmup:
            continue

        # Generate targets and execute
        for prod in mids:
            mid = mids[prod]
            cat = get_category(prod)

            if cat in SKIP_CATEGORIES:
                continue

            avg_spread = SPREAD_PCT.get(cat, 0.10) / 100.0

            entry_t = entry_mult * avg_spread
            exit_t  = exit_mult  * avg_spread

            trend = 0.0
            if prod in ema_s and ema_s[prod] > 0:
                trend = (ema_f[prod] - ema_s[prod]) / ema_s[prod]

            # XIRECS boost
            if prod in xirecs and mid > 0:
                xr_sig = (xirecs[prod] - mid) / mid
                if xr_sig * trend > 0:
                    trend += xr_sig * 0.3

            cur_tgt = target.get(prod, 0)

            # State machine
            if cur_tgt == 0:
                if trend > entry_t:
                    cur_tgt = LIMIT
                elif trend < -entry_t:
                    cur_tgt = -LIMIT
            elif cur_tgt > 0:
                if trend < exit_t:
                    cur_tgt = 0
                if trend < -entry_t:
                    cur_tgt = -LIMIT
            elif cur_tgt < 0:
                if trend > -exit_t:
                    cur_tgt = 0
                if trend > entry_t:
                    cur_tgt = LIMIT

            target[prod] = cur_tgt

            # Execute
            pos = positions[prod]
            diff = cur_tgt - pos
            row_data = rows.get(prod)
            if row_data is None or diff == 0:
                continue

            bid1 = row_data['bid_price_1']
            ask1 = row_data['ask_price_1']
            bv1 = int(row_data['bid_volume_1'])
            av1 = int(row_data['ask_volume_1'])

            if diff > 0:
                # Buy at ask
                fills = [(int(ask1), min(av1, diff))]
                if pd.notna(row_data.get('ask_price_2')) and row_data.get('ask_price_2', 0) > 0:
                    remaining = diff - fills[0][1]
                    if remaining > 0:
                        fills.append((int(row_data['ask_price_2']),
                                      min(int(row_data['ask_volume_2']), remaining)))
                for px, qty in fills:
                    if qty > 0:
                        cash[prod] -= px * qty
                        positions[prod] += qty
                        trade_count[prod] += 1
                        diff -= qty
                        if diff <= 0:
                            break
            elif diff < 0:
                # Sell at bid
                diff_abs = abs(diff)
                fills = [(int(bid1), min(bv1, diff_abs))]
                if pd.notna(row_data.get('bid_price_2')) and row_data.get('bid_price_2', 0) > 0:
                    remaining = diff_abs - fills[0][1]
                    if remaining > 0:
                        fills.append((int(row_data['bid_price_2']),
                                      min(int(row_data['bid_volume_2']), remaining)))
                for px, qty in fills:
                    if qty > 0:
                        cash[prod] += px * qty
                        positions[prod] -= qty
                        trade_count[prod] += 1
                        diff_abs -= qty
                        if diff_abs <= 0:
                            break

    # Results
    results = {}
    total_pnl = 0.0
    for prod in sorted(products):
        pos = positions[prod]
        last_mid = 0
        for ts in reversed(timestamps):
            if prod in price_idx.get(ts, {}):
                row_data = price_idx[ts][prod]
                b = row_data['bid_price_1']
                a = row_data['ask_price_1']
                if pd.notna(b) and pd.notna(a):
                    last_mid = (b + a) / 2.0
                break
        pnl = cash[prod] + pos * last_mid
        results[prod] = {
            'pos': pos, 'cash': cash[prod], 'unrealized': pos * last_mid,
            'pnl': pnl, 'trades': trade_count[prod], 'category': get_category(prod),
        }
        total_pnl += pnl

    if verbose:
        print(f"\n{'Product':<45} {'Cat':<15} {'Pos':>4} {'Tr':>4} "
              f"{'Cash':>10} {'Unreal':>10} {'PnL':>10}")
        print("-" * 105)
        for prod in sorted(results, key=lambda x: results[x]['pnl'], reverse=True):
            r = results[prod]
            print(f"{prod:<45} {r['category']:<15} {r['pos']:>4} {r['trades']:>4} "
                  f"{r['cash']:>10,.0f} {r['unrealized']:>10,.0f} {r['pnl']:>10,.0f}")
        print("-" * 105)
        tr = sum(r['trades'] for r in results.values())
        print(f"{'TOTAL':<45} {'':<15} {'':<4} {tr:>4} "
              f"{sum(r['cash'] for r in results.values()):>10,.0f} "
              f"{sum(r['unrealized'] for r in results.values()):>10,.0f} "
              f"{total_pnl:>10,.0f}")

        print(f"\n{'Category':<20} {'PnL':>12} {'Trades':>8}")
        print("-" * 42)
        cat_pnl = {}
        for r in results.values():
            cat_pnl[r['category']] = cat_pnl.get(r['category'], 0) + r['pnl']
        for cat in sorted(cat_pnl, key=lambda x: cat_pnl[x], reverse=True):
            print(f"{cat:<20} {cat_pnl[cat]:>12,.0f} "
                  f"{sum(r['trades'] for r in results.values() if r['category']==cat):>8}")

    return total_pnl, results


if __name__ == "__main__":
    # ── Test on Day 4 only (matching submission environment) ──
    print("=" * 60)
    print("  BACKTEST: DAY 4 ONLY (matching submission)")
    print("=" * 60)
    pnl_d4, _ = run_backtest(
        entry_mult=ENTRY_MULT, exit_mult=EXIT_MULT,
        ema_slow=EMA_SLOW_ALPHA, ema_fast=EMA_FAST_ALPHA,
        warmup=WARMUP_TICKS, day_list=[4],
    )

    # ── Test on all 3 days ──
    print("\n" + "=" * 60)
    print("  BACKTEST: DAYS 2-4 (full historical)")
    print("=" * 60)
    pnl_all, _ = run_backtest(
        entry_mult=ENTRY_MULT, exit_mult=EXIT_MULT,
        ema_slow=EMA_SLOW_ALPHA, ema_fast=EMA_FAST_ALPHA,
        warmup=WARMUP_TICKS, day_list=[2, 3, 4],
    )

    # ── Quick parameter sweep on Day 4 ──
    print("\n" + "=" * 60)
    print("  PARAMETER SWEEP (Day 4)")
    print("=" * 60)
    configs = [
        ("Baseline",           5.0, 1.0, 0.0003, 0.005,  300),
        ("Lower entry",        3.0, 1.0, 0.0003, 0.005,  300),
        ("Higher entry",       8.0, 1.5, 0.0003, 0.005,  300),
        ("Faster EMA",         5.0, 1.0, 0.0005, 0.01,   300),
        ("Slower EMA",         5.0, 1.0, 0.0002, 0.003,  300),
        ("Shorter warmup",     5.0, 1.0, 0.0003, 0.005,  100),
        ("Longer warmup",      5.0, 1.0, 0.0003, 0.005,  500),
        ("No XIRECS+low entry",3.0, 0.5, 0.0003, 0.005,  200),
        ("Tight+fast",         3.0, 0.5, 0.0005, 0.008,  200),
        ("Very slow+wide",     8.0, 2.0, 0.0001, 0.002,  500),
        ("Aggressive",         2.0, 0.5, 0.0005, 0.01,   150),
        ("Ultra conservative", 10.0,2.0, 0.0002, 0.003,  500),
    ]

    results = []
    for name, em, xm, es, ef, wu in configs:
        pnl, _ = run_backtest(em, xm, es, ef, wu, day_list=[4], verbose=False)
        results.append((name, pnl))
        print(f"  {name:<28} PnL = {pnl:>12,.0f}")

    print("\n--- RANKED ---")
    for name, pnl in sorted(results, key=lambda x: x[1], reverse=True):
        print(f"  {name:<28} PnL = {pnl:>12,.0f}")
