"""
Param sweep v3: Continue from existing results.
Phase 1: New skip categories (SR_GS, SR, S, NONE) on the original grid.
Phase 2: Fine grid around the top-performing region for ALL skip categories.
Appends to param_sweep_results.csv.
"""
import pandas as pd
import numpy as np
import itertools
import time
import warnings
warnings.filterwarnings("ignore")

DATA = "ROUND_5/"
LIMIT = 10
MAX_TIMESTAMP = 99900
DAYS = [2, 3, 4]

SPREAD_PCT = {
    "ROBOT": 0.073, "MICROCHIP": 0.089, "SLEEP_POD": 0.088,
    "TRANSLATOR": 0.089, "PANEL": 0.097, "GALAXY_SOUNDS": 0.127,
    "OXYGEN_SHAKE": 0.128, "PEBBLES": 0.129, "UV_VISOR": 0.128,
    "SNACKPACK": 0.167,
}

SKIP_CONFIGS = {
    "SR_OX_GS": {"SNACKPACK", "ROBOT", "OXYGEN_SHAKE", "GALAXY_SOUNDS"},
    "SR_OX":    {"SNACKPACK", "ROBOT", "OXYGEN_SHAKE"},
    "SR_GS":    {"SNACKPACK", "ROBOT", "GALAXY_SOUNDS"},
    "SR":       {"SNACKPACK", "ROBOT"},
    "S":        {"SNACKPACK"},
    "NONE":     set(),
}


def get_category(p):
    for c in SPREAD_PCT:
        if p.startswith(c):
            return c
    return "OTHER"


def load_day_data(day):
    prices = pd.read_csv(f'{DATA}prices_round_5_day_{day}.csv', sep=';')
    trades = pd.read_csv(f'{DATA}trades_round_5_day_{day}.csv', sep=';')
    trades['day'] = day
    prices = prices[prices['timestamp'] <= MAX_TIMESTAMP].copy()
    trades = trades[trades['timestamp'] <= MAX_TIMESTAMP].copy()
    return prices, trades


def index_day(prices, trades, day_offset=0):
    prices['global_ts'] = day_offset * 1_000_000 + prices['timestamp']
    trades['global_ts'] = day_offset * 1_000_000 + trades['timestamp']

    timestamps = sorted(prices['global_ts'].unique())

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
    for _, row in trades.iterrows():
        ts = row['global_ts']
        prod = row['symbol']
        if ts not in trade_idx:
            trade_idx[ts] = {}
        trade_idx[ts][prod] = row['price']

    return timestamps, price_idx, trade_idx


def run_single_day(timestamps, price_idx, trade_idx, skip_cats, entry_mult, exit_mult, ema_slow, ema_fast, warmup):
    """Run backtest on pre-indexed single-day data. Returns PnL."""
    ema_s = {}
    ema_f = {}
    xirecs = {}
    target = {}
    positions = {}
    cash = {}
    tick = 0

    for ts in timestamps:
        tick += 1
        rows = price_idx.get(ts, {})
        xt = trade_idx.get(ts, {})

        for prod, xp in xt.items():
            xirecs[prod] = xp

        mids = {}
        for prod, row in rows.items():
            bid1, ask1 = row[0], row[1]
            bv1, av1 = row[2], row[3]
            if bid1 > 0 and ask1 > 0:
                mid = (bid1 + ask1) / 2.0
                mids[prod] = mid
                if prod in ema_s:
                    ema_s[prod] += ema_slow * (mid - ema_s[prod])
                    ema_f[prod] += ema_fast * (mid - ema_f[prod])
                else:
                    ema_s[prod] = mid
                    ema_f[prod] = mid

        if tick < warmup:
            continue

        for prod in mids:
            mid = mids[prod]
            cat = get_category(prod)
            if cat in skip_cats:
                continue

            if prod not in positions:
                positions[prod] = 0
                cash[prod] = 0.0

            avg_spread = SPREAD_PCT.get(cat, 0.10) / 100.0
            entry_t = entry_mult * avg_spread
            exit_t = exit_mult * avg_spread

            trend = 0.0
            if prod in ema_s and ema_s[prod] > 0:
                trend = (ema_f[prod] - ema_s[prod]) / ema_s[prod]

            if prod in xirecs and mid > 0:
                xr_sig = (xirecs[prod] - mid) / mid
                if xr_sig * trend > 0:
                    trend += xr_sig * 0.3

            cur_tgt = target.get(prod, 0)
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

            pos = positions[prod]
            diff = cur_tgt - pos
            r = rows.get(prod)
            if r is None or diff == 0:
                continue

            bid1, ask1 = r[0], r[1]
            bv1, av1 = int(r[2]), int(r[3])

            if diff > 0:
                fills = [(int(ask1), min(av1, diff))]
                if r[6] > 0:
                    rem = diff - fills[0][1]
                    if rem > 0:
                        fills.append((int(r[6]), min(int(r[7]), rem)))
                for px, qty in fills:
                    if qty > 0:
                        cash[prod] -= px * qty
                        positions[prod] += qty
                        diff -= qty
                        if diff <= 0:
                            break
            elif diff < 0:
                diff_abs = abs(diff)
                fills = [(int(bid1), min(bv1, diff_abs))]
                if r[4] > 0:
                    rem = diff_abs - fills[0][1]
                    if rem > 0:
                        fills.append((int(r[4]), min(int(r[5]), rem)))
                for px, qty in fills:
                    if qty > 0:
                        cash[prod] += px * qty
                        positions[prod] -= qty
                        diff_abs -= qty
                        if diff_abs <= 0:
                            break

    # Final PnL
    last_mids = {}
    for ts in reversed(timestamps):
        rows = price_idx.get(ts, {})
        for prod, r in rows.items():
            if prod not in last_mids and r[0] > 0 and r[1] > 0:
                last_mids[prod] = (r[0] + r[1]) / 2.0
        if len(last_mids) >= len(positions):
            break

    total = sum(cash.get(p, 0) + positions.get(p, 0) * last_mids.get(p, 0) for p in positions)
    return total


# ─── Load and index all day data once ───
print("Loading data...")
t0 = time.time()
day_data = {}
for day in DAYS:
    p, t = load_day_data(day)
    ts, pi, ti = index_day(p, t, day_offset=0)
    day_data[day] = (ts, pi, ti)
    print(f"  Day {day}: {len(ts)} ticks")
print(f"Data loaded in {time.time()-t0:.1f}s")

# ─── Load existing results to avoid re-running ───
existing = set()
try:
    df_existing = pd.read_csv('param_sweep_results.csv')
    for _, row in df_existing.iterrows():
        key = (row['skip'], row['entry_mult'], row['exit_mult'],
               row['ema_slow'], row['ema_fast'], row['warmup'])
        existing.add(key)
    print(f"Loaded {len(existing)} existing results")
except FileNotFoundError:
    print("No existing results found, starting fresh")

# ─── Phase 1: New skip categories on original grid ───
phase1_grid = {
    'entry_mult': [8, 10, 12, 15],
    'exit_mult': [0.5, 1.0, 1.5, 2.0],
    'ema_slow': [0.0001, 0.00015, 0.0002, 0.0003],
    'ema_fast': [0.002, 0.003, 0.004, 0.005],
    'warmup': [300, 350, 400, 450],
}
phase1_skips = ["SR_GS", "SR", "S", "NONE"]

# ─── Phase 2: Fine grid around top region for ALL skip categories ───
phase2_grid = {
    'entry_mult': [5, 6, 7, 8, 9, 10, 11],
    'exit_mult': [0.25, 0.5, 0.75, 1.0, 1.25],
    'ema_slow': [0.0002, 0.00025, 0.0003, 0.00035, 0.0004],
    'ema_fast': [0.0015, 0.002, 0.0025, 0.003, 0.0035],
    'warmup': [250, 300, 350, 400],
}
phase2_skips = ["SR_OX_GS", "SR_OX", "SR_GS", "SR"]

# Build all configs
all_configs = []

# Phase 1
for skip_name in phase1_skips:
    keys = list(phase1_grid.keys())
    for combo in itertools.product(*[phase1_grid[k] for k in keys]):
        params = dict(zip(keys, combo))
        key = (skip_name, params['entry_mult'], params['exit_mult'],
               params['ema_slow'], params['ema_fast'], params['warmup'])
        if key not in existing:
            all_configs.append(("P1", skip_name, params))

# Phase 2
for skip_name in phase2_skips:
    keys = list(phase2_grid.keys())
    for combo in itertools.product(*[phase2_grid[k] for k in keys]):
        params = dict(zip(keys, combo))
        key = (skip_name, params['entry_mult'], params['exit_mult'],
               params['ema_slow'], params['ema_fast'], params['warmup'])
        if key not in existing:
            all_configs.append(("P2", skip_name, params))

print(f"\n=== SWEEP: {len(all_configs)} new configs to run ===\n")

# ─── Run sweep ───
results_buffer = []
best_avg = -1e9
best_cfg = None
total = len(all_configs)

for i, (phase, skip_name, params) in enumerate(all_configs):
    skip_cats = SKIP_CONFIGS[skip_name]
    day_pnls = []
    for day in DAYS:
        ts, pi, ti = day_data[day]
        pnl = run_single_day(ts, pi, ti, skip_cats,
                             params['entry_mult'], params['exit_mult'],
                             params['ema_slow'], params['ema_fast'],
                             params['warmup'])
        day_pnls.append(pnl)

    avg = np.mean(day_pnls)
    mn = min(day_pnls)
    mx = max(day_pnls)

    row = {
        'skip': skip_name,
        'entry_mult': params['entry_mult'],
        'exit_mult': params['exit_mult'],
        'ema_slow': params['ema_slow'],
        'ema_fast': params['ema_fast'],
        'warmup': params['warmup'],
        'day2': day_pnls[0],
        'day3': day_pnls[1],
        'day4': day_pnls[2],
        'avg': avg,
        'min': mn,
        'max': mx,
    }
    results_buffer.append(row)

    if avg > best_avg:
        best_avg = avg
        best_cfg = row

    if (i + 1) % 50 == 0 or (i + 1) == total:
        # Flush to CSV
        df_new = pd.DataFrame(results_buffer)
        df_new.to_csv('param_sweep_results.csv', mode='a', header=False, index=False)
        results_buffer = []
        elapsed = time.time() - t0
        rate = (i + 1) / max(elapsed - 5, 1)  # subtract load time approx
        eta = (total - i - 1) / max(rate, 0.01)
        print(f"  [{i+1:>5}/{total}] {phase} {skip_name:<10} "
              f"avg={avg:>10,.0f}  best={best_avg:>10,.0f}  "
              f"ETA: {eta/60:.1f}min")

print(f"\n=== SWEEP COMPLETE ===")
print(f"Best config (avg PnL = {best_avg:,.0f}):")
for k, v in best_cfg.items():
    print(f"  {k}: {v}")

# Final summary
print("\n=== TOP 20 OVERALL (all sweeps combined) ===")
df_all = pd.read_csv('param_sweep_results.csv')
print(df_all.nlargest(20, 'avg').to_string(index=False))
