# IMC Prosperity Round 1 - Post-Mortem Analysis

## Our Result: 90,250 (Pepper: 72,368 + Ash: 17,882)
## 1st Place: 125,850
## Gap: 35,600

---

## The Two Products Explained Simply

### INTARIAN_PEPPER_ROOT (Trending Product)
Think of Pepper like a stock that goes up at a perfectly predictable rate. Every second, the price increases by exactly 1 tick. If you know it's going up, you just buy as much as you can (80 units, the max allowed) and hold. You make money every second just by sitting there.

**Our Pepper PnL: 72,368 out of ~72,600 possible = 99.7% perfect.**
Nothing to improve. Everyone who figured out the linear drift formula (`price = base + timestamp/1000`) got roughly the same score.

### ASH_COATED_OSMIUM (Mean-Reverting Product)
Think of Ash like a rubber band around the price 10,000. It bounces up and down randomly (range: 9,981 to 10,020) but always comes back to ~10,000. There's no trend to ride -- instead you make money by **market-making**: placing buy orders slightly below the current price and sell orders slightly above, collecting the "spread" (the difference) each time both sides fill.

**Our Ash PnL: 17,882. Top teams got ~53,000. This is where we lost.**

---

## What is Market Making?

Imagine you're at a currency exchange at an airport. You buy euros at $1.08 and sell them at $1.12. You don't care which way the euro goes -- you profit $0.04 on every round trip (buy then sell, or sell then buy).

That's market making. In our case:
- Ash bounces around 10,000
- We place a **bid** (buy order) at say 9,998
- We place an **ask** (sell order) at say 10,002
- When both fill, we made 4 per unit
- With 20 units, that's 80 profit per round trip
- Over 9,099 ticks, this compounds to real money

**The key requirement: you need to stay close to FLAT (position near 0).** If you're holding +80 units (max long), you can only sell, not buy. You've lost half your business.

---

## What Went Wrong: The Four Problems

### Problem 1: The Dead Parameter (Cost us ~8,000-10,000)

```python
SimpleAshTrader(mm_qty=20, mm_spread=1, inv_thresh=60)
#                                        ^^^^^^^^^^^
#                          This parameter was NEVER USED in the code
```

`inv_thresh` was supposed to be an inventory threshold -- "if my position goes above 60, stop buying and sell back down." But the code that would actually DO this was never written. The parameter was passed to `__init__`, stored as `self.inv_thresh`, and then... nothing. It's never read in the `run()` function.

**What should have been there:**
```python
# Before posting MM orders:
if position > self.inv_thresh:
    bid_qty = 0  # STOP BUYING, you're too long
elif position < -self.inv_thresh:
    ask_qty = 0  # STOP SELLING, you're too short
```

Without this, our position drifted to 80 (max long) and got stuck. Once at 80, we could only sell -- half our money-making ability was gone.

### Problem 2: Position Drift Killed Our Profit Rate (Cost us ~10,000)

Here's what the loss/gain ratio looked like over time:

| Time Period | Gains | Losses | % Kept | Likely Position |
|---|---|---|---|---|
| 0-100k (early) | 4,542 | -1,585 | **65%** | Small, near 0 |
| 100k-400k | 12,593 | -6,906 | **45%** | Growing |
| 500k-700k | 20,711 | -17,622 | **15%** | Large, near 80 |
| 700k-800k | 9,063 | -8,130 | **10%** | Stuck at 80 |

Early on, we kept 65 cents of every dollar earned. By mid-round, we were keeping only 10-15 cents. The position was so bloated that every tiny price wiggle caused massive mark-to-market losses that ate our spread profits.

**The worst single-tick losses:**
- t=756,700: **-90.2** (one tick wiped out ~45 ticks of spread income)
- t=618,000: **-78.3**
- t=912,800: **-78.1**
- t=747,900: **-74.1**

These are all large-position losses. A 1-2 tick adverse move on 60-80 units = instant 60-80 loss. A well-managed strategy with position capped at 30-40 would see losses of only 30-40 -- literally half.

### Problem 3: MM Quote Size Was Too Small For Flat Operation (Cost us ~8,000-12,000)

We used `mm_qty=20` -- posting 20-unit bids and asks each tick. This is conservative. If we had proper inventory management (Problem 1 fixed), we could safely have used `mm_qty=40` or even `60`, because we'd know the position would be actively managed back to 0.

More quote size = more fills = more spread captured:
- 20 units at 4 ticks spread = 80/round trip
- 40 units at 4 ticks spread = 160/round trip (2x)
- 60 units at 4 ticks spread = 240/round trip (3x)

Top teams likely ran 40-60 qty with tight inventory control. They filled more, earned more per tick, and stayed flat enough to do it on both sides consistently.

### Problem 4: Fair Value Estimation Was Basic (Cost us ~5,000-8,000)

Our mid-price calculation:
```python
deepest_bid = bids[-1][0]   # worst bid in the book
deepest_ask = asks[-1][0]   # worst ask in the book
mid = round((deepest_bid + deepest_ask) / 2)
```

This uses the **deepest** (worst) orders on each side. It's noisy -- the deepest orders are often outliers from slow NPCs. Top teams likely used:
- **Volume-weighted mid-price** (gives more weight to large orders)
- **Micro-price** (weights by which side has more volume -- if there's more volume on the bid, price is likely to go up)
- **NPC pattern detection** (specific bots quote predictably -- if you learn their patterns, you can front-run them)

A 1-tick better fair value estimate means every trade you make has 1 tick more edge. Over thousands of trades, that's thousands of extra profit.

---

## What The Top Team Probably Did

```
1. Identified Ash as mean-reverting around 10,000
2. Used a sharp fair-value estimate (micro-price or VWAP-mid)
3. Posted LARGE two-sided quotes (40-60 qty each side)
4. Had HARD inventory limits (position capped at +/- 30-40)
5. When position exceeded threshold: only posted orders that flatten
6. Result: consistent 5,000-6,000 per 100k ticks
7. Over 913k ticks = ~50,000-55,000 Ash PnL
```

vs our approach:
```
1. Identified Ash as mean-reverting (correct)
2. Used deepest-bid/ask mid (okay but noisy)
3. Posted small two-sided quotes (20 qty)
4. Had NO inventory limits (inv_thresh was dead code)
5. Position drifted to 80 and stayed there
6. Result: started at 3,000/100k, degraded to 900/100k
7. Over 913k ticks = 17,882 Ash PnL
```

---

## The Pepper Strategy - What We Did Right

Our `SimplePepperTrader` was actually clever and more general than needed:

1. **Slope detection**: Used `np.polyfit` on last 50 mid-prices to detect if the trend was up or down
2. **If uptrend (R^2 >= 0.2, slope > 0)**: Go max long (80 units), ride the drift up
3. **If downtrend**: Go max short, ride the drift down
4. **If no clear trend (R^2 < 0.2)**: Flatten to 0 and wait

For this round, the trend was always positive, so it just went max long and held. Perfect.

The `spread = 6 + (ts/100)` buy tolerance was aggressive -- at t=900k it would buy at literally any price 9,006 ticks above fair. This sounds crazy but is correct: when you're riding a drift that makes you 80/tick, paying an extra 10 ticks to fill faster loses 10 but gains 80/tick for every tick you filled earlier.

---

## Summary: Where Every Dollar Was Won/Lost

| Category | Our PnL | Potential PnL | Gap |
|---|---|---|---|
| Pepper (trend riding) | 72,368 | ~72,600 | ~200 (negligible) |
| Ash (market making) | 17,882 | ~53,000 | **~35,000** |
| **Total** | **90,250** | **~125,600** | **~35,350** |

**Ash gap breakdown:**
| Issue | Estimated Cost |
|---|---|
| Dead `inv_thresh` (no inventory management) | ~8,000-10,000 |
| Position drift reducing two-sided capacity | ~10,000 |
| Conservative MM size (20 instead of 40-60) | ~8,000-12,000 |
| Basic fair-value estimate | ~5,000-8,000 |
| **Total Ash gap** | **~35,000** |

---

## Key Takeaway

**Pepper was solved. Ash was the game.** The single biggest mistake was the dead `inv_thresh` parameter -- a bug where the inventory management logic was never connected to the parameter that was supposed to control it. Fixing that one bug alone (adding 4 lines of code) would have been worth ~8,000-10,000 in profit and bumped us from 90,250 to ~100,000. Combining that with larger MM sizes could have pushed us to ~110,000-115,000.

The remaining gap to 125,850 would have required a fundamentally sharper Ash strategy -- better fair value (micro-price), NPC pattern exploitation, and aggressive but controlled sizing.
