"""
Round-trip trade reconstruction and Layer-1 statistics computation.

Fill field reference (from live API, 2025-05-21):
  coin, px (str), sz (str), side (A=sell/ask, B=buy/bid),
  dir: "Open Long" | "Close Long" | "Open Short" | "Close Short" | "Settlement" | "Spot Dust Conversion",
  time (ms int), startPosition (str), closedPnl (str), fee (str), hash, oid, tid, feeToken

Funding field reference:
  time (ms), delta: {type, coin, usdc (str), szi (str), fundingRate (str)}

Position state reference (clearinghouseState.assetPositions[i].position):
  coin, szi, leverage:{type, value}, entryPx, positionValue, unrealizedPnl,
  cumFunding:{allTime, sinceOpen, sinceChange}
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

TARGET_ASSET_SET = None  # set at runtime from config


@dataclass
class RoundTrip:
    address: str
    coin: str
    direction: str          # "Long" or "Short"
    entry_time_ms: int
    exit_time_ms: int
    avg_entry_px: float
    avg_exit_px: float
    peak_size: float        # largest open interest during the trade
    total_size: float       # sum of all closing fill sizes
    realized_pnl: float     # from closedPnl fields (includes partial closes)
    fees_paid: float        # sum of fee fields (negative fee = rebate)
    funding_pnl: float      # funding received (+) or paid (-) during hold
    holding_hours: float
    leverage: Optional[float]       # if inferable
    is_liquidation: bool    # if final fill appears to be a forced liquidation
    low_confidence: bool = False    # fewer than 20 trades in this wallet's set


@dataclass
class TraderStats:
    address: str
    display_address: str    # first6...last4
    trades: list            # list of RoundTrip
    # Per-asset breakdown
    by_asset: dict = field(default_factory=dict)
    # Aggregate stats
    total_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    win_loss_ratio: float = 0.0
    expectancy: float = 0.0
    profit_factor: float = 0.0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    avg_leverage: Optional[float] = None
    peak_leverage: Optional[float] = None
    avg_hold_winners_h: float = 0.0
    avg_hold_losers_h: float = 0.0
    long_count: int = 0
    short_count: int = 0
    long_pnl: float = 0.0
    short_pnl: float = 0.0
    hour_buckets: dict = field(default_factory=dict)    # {0..23: count}
    dow_buckets: dict = field(default_factory=dict)     # {0..6: count}
    size_quartiles: list = field(default_factory=list)  # [Q1, Q2, Q3] of peak_size
    total_funding_paid: float = 0.0     # negative = we paid
    total_funding_earned: float = 0.0   # positive = we received
    liquidation_count: int = 0
    low_confidence: bool = False
    error: Optional[str] = None         # set if no fills / analysis impossible


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _shorten_addr(addr: str) -> str:
    if len(addr) < 10:
        return addr
    return f"{addr[:6]}...{addr[-4:]}"


def reconstruct_round_trips(
    address: str,
    fills: list,
    funding_history: list,
    target_assets: set,
) -> list:
    """
    Group fills by (address, coin) in time order and emit RoundTrip objects.

    Logic:
    - Sort fills by time ascending.
    - For each (coin), track a running net position.
    - An "open" starts when position moves from 0 → non-zero.
    - Scale-ins and partial closes are absorbed.
    - When position returns to 0 (or crosses 0), the round-trip closes.
    - "Settlement" fills are counted as closures.
    """
    # Filter to target assets only
    filtered = [f for f in fills if f.get("coin") in target_assets]
    if not filtered:
        logger.debug("No target-asset fills for %s", address[:10])
        return []

    # Sort ascending by time
    filtered.sort(key=lambda f: f["time"])

    # Build per-coin funding lookup: list of (time_ms, usdc_amount)
    funding_by_coin: dict[str, list] = defaultdict(list)
    for entry in funding_history:
        delta = entry.get("delta", {})
        if delta.get("type") == "funding":
            coin = delta.get("coin", "")
            if coin in target_assets:
                funding_by_coin[coin].append(
                    (entry["time"], _safe_float(delta.get("usdc", 0)))
                )

    # Group fills by coin
    fills_by_coin: dict[str, list] = defaultdict(list)
    for f in filtered:
        fills_by_coin[f["coin"]].append(f)

    round_trips = []

    for coin, coin_fills in fills_by_coin.items():
        net_pos = 0.0         # running net position (positive=long, negative=short)
        open_fills = []       # fills since last time position was 0
        close_fills = []      # fills that partially/fully close

        def _emit_trip(o_fills, c_fills):
            if not o_fills:
                return None
            direction = "Long" if _safe_float(o_fills[0].get("sz")) > 0 and \
                "Open Long" in [f.get("dir", "") for f in o_fills] else "Short"
            # Determine direction from first open fill
            for f in o_fills:
                d = f.get("dir", "")
                if "Open Long" in d:
                    direction = "Long"
                    break
                if "Open Short" in d:
                    direction = "Short"
                    break

            entry_time = o_fills[0]["time"]
            exit_time = (c_fills[-1]["time"] if c_fills else o_fills[-1]["time"])

            # Avg entry: weighted by size across opening fills
            open_sizes = [_safe_float(f["sz"]) for f in o_fills
                          if "Open" in f.get("dir", "")]
            open_pxs = [_safe_float(f["px"]) for f in o_fills
                        if "Open" in f.get("dir", "")]
            total_open = sum(open_sizes)
            avg_entry = (
                sum(p * s for p, s in zip(open_pxs, open_sizes)) / total_open
                if total_open > 0
                else (_safe_float(o_fills[0]["px"]))
            )

            # Avg exit: weighted by closing fills
            all_c = c_fills
            close_sizes = [_safe_float(f["sz"]) for f in all_c if "Close" in f.get("dir", "") or "Settlement" in f.get("dir", "")]
            close_pxs = [_safe_float(f["px"]) for f in all_c if "Close" in f.get("dir", "") or "Settlement" in f.get("dir", "")]
            total_close = sum(close_sizes)
            avg_exit = (
                sum(p * s for p, s in zip(close_pxs, close_sizes)) / total_close
                if total_close > 0
                else (_safe_float(all_c[-1]["px"]) if all_c else avg_entry)
            )

            # PnL from closedPnl fields on closing fills
            realized_pnl = sum(
                _safe_float(f.get("closedPnl", 0))
                for f in all_c
            )
            if realized_pnl == 0 and o_fills:
                # fallback: compute from prices
                if direction == "Long":
                    realized_pnl = (avg_exit - avg_entry) * total_close
                else:
                    realized_pnl = (avg_entry - avg_exit) * total_close

            # Fees (negative fee = rebate, we model from taker's perspective)
            all_trip_fills = o_fills + c_fills
            fees = sum(_safe_float(f.get("fee", 0)) for f in all_trip_fills)

            # Peak size
            peak_sz = max(abs(_safe_float(f.get("startPosition", 0))) for f in all_trip_fills)
            if peak_sz == 0:
                peak_sz = total_open

            # Holding duration
            holding_h = max(0.0, (exit_time - entry_time) / 3_600_000)

            # Funding during this trip
            funding_pnl = sum(
                usdc
                for ts, usdc in funding_by_coin.get(coin, [])
                if entry_time <= ts <= exit_time
            )

            # Liquidation check: final closing fill is Settlement or dir contains "Liquidat"
            is_liq = False
            if all_c:
                last_dir = all_c[-1].get("dir", "")
                is_liq = "Settlement" in last_dir or "Liquidat" in last_dir

            return RoundTrip(
                address=address,
                coin=coin,
                direction=direction,
                entry_time_ms=entry_time,
                exit_time_ms=exit_time,
                avg_entry_px=avg_entry,
                avg_exit_px=avg_exit,
                peak_size=peak_sz,
                total_size=total_close if total_close > 0 else total_open,
                realized_pnl=realized_pnl,
                fees_paid=fees,
                funding_pnl=funding_pnl,
                holding_hours=holding_h,
                leverage=None,  # filled in later from state if possible
                is_liquidation=is_liq,
            )

        for fill in coin_fills:
            sz = _safe_float(fill["sz"])
            direction_dir = fill.get("dir", "")
            side = fill.get("side", "")

            # Determine signed size change.
            # dir values seen in wild: Open Long, Close Long, Open Short, Close Short,
            # Long > Short (flip: was long, now short — net change is -2*sz worth),
            # Short > Long (flip: was short, now long — net change is +2*sz worth),
            # Settlement (forced close).
            if direction_dir == "Open Long":
                signed_sz = sz
            elif direction_dir == "Open Short":
                signed_sz = -sz
            elif direction_dir == "Close Long":
                signed_sz = -sz
            elif direction_dir == "Close Short":
                signed_sz = sz
            elif direction_dir in ("Long > Short", "Short > Long"):
                # These fills flip the position.
                # "Long > Short": B→A flip, side=A, net change = -(prev_pos + sz)
                # Use side as the ground truth for direction of the change.
                signed_sz = sz if side == "B" else -sz
            elif "Settlement" in direction_dir:
                # forced settlement — treat as close
                signed_sz = -sz if net_pos > 0 else sz
            else:
                # Fallback: use side
                signed_sz = sz if side == "B" else -sz

            prev_pos = net_pos
            net_pos += signed_sz
            net_pos = round(net_pos, 10)  # float precision cleanup

            if abs(prev_pos) < 1e-9 and abs(net_pos) > 1e-9:
                # Opening a new position
                open_fills = [fill]
                close_fills = []
            elif abs(prev_pos) > 1e-9 and abs(net_pos) < 1e-9:
                # Closing (position back to 0)
                close_fills.append(fill)
                trip = _emit_trip(open_fills, close_fills)
                if trip:
                    round_trips.append(trip)
                open_fills = []
                close_fills = []
            elif abs(prev_pos) > 1e-9 and abs(net_pos) > 1e-9:
                # Check for direction flip (crossed zero)
                if (prev_pos > 0) != (net_pos > 0):
                    # Partial close that crosses zero — emit old trip, start new
                    close_fills.append(fill)
                    trip = _emit_trip(open_fills, close_fills)
                    if trip:
                        round_trips.append(trip)
                    open_fills = [fill]
                    close_fills = []
                elif "Open" in direction_dir:
                    # Scale-in
                    open_fills.append(fill)
                else:
                    # Partial close
                    close_fills.append(fill)
            else:
                open_fills.append(fill)

        # Emit any open position at end of fills (still open)
        if open_fills:
            trip = _emit_trip(open_fills, close_fills)
            if trip:
                # Mark as still open (exit_time == last fill time)
                round_trips.append(trip)

    return round_trips


def compute_stats(address: str, round_trips: list) -> TraderStats:
    """Compute Layer-1 statistics from a list of RoundTrip objects."""
    display = _shorten_addr(address)
    stats = TraderStats(address=address, display_address=display, trades=round_trips)

    if not round_trips:
        stats.error = "No round-trip trades found in target assets"
        return stats

    n = len(round_trips)
    stats.total_trades = n
    stats.low_confidence = n < 20

    net_pnls = [t.realized_pnl + t.funding_pnl - t.fees_paid for t in round_trips]
    winners = [p for p in net_pnls if p > 0]
    losers = [p for p in net_pnls if p <= 0]

    stats.win_rate = len(winners) / n if n > 0 else 0.0
    stats.avg_win = sum(winners) / len(winners) if winners else 0.0
    stats.avg_loss = sum(losers) / len(losers) if losers else 0.0
    stats.win_loss_ratio = (
        abs(stats.avg_win / stats.avg_loss) if stats.avg_loss != 0 else float("inf")
    )
    loss_rate = 1 - stats.win_rate
    stats.expectancy = (stats.win_rate * stats.avg_win) + (loss_rate * stats.avg_loss)

    gross_wins = sum(winners)
    gross_losses = abs(sum(losers))
    stats.profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")
    stats.total_pnl = sum(net_pnls)

    # Max drawdown (running PnL from peak)
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in net_pnls:
        running += p
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    stats.max_drawdown = max_dd

    # Leverage
    leverages = [t.leverage for t in round_trips if t.leverage is not None]
    if leverages:
        stats.avg_leverage = sum(leverages) / len(leverages)
        stats.peak_leverage = max(leverages)

    # Long vs short
    longs = [t for t in round_trips if t.direction == "Long"]
    shorts = [t for t in round_trips if t.direction == "Short"]
    stats.long_count = len(longs)
    stats.short_count = len(shorts)
    stats.long_pnl = sum(t.realized_pnl + t.funding_pnl - t.fees_paid for t in longs)
    stats.short_pnl = sum(t.realized_pnl + t.funding_pnl - t.fees_paid for t in shorts)

    # Hold time: winners vs losers
    win_idx = [i for i, p in enumerate(net_pnls) if p > 0]
    loss_idx = [i for i, p in enumerate(net_pnls) if p <= 0]
    if win_idx:
        stats.avg_hold_winners_h = sum(round_trips[i].holding_hours for i in win_idx) / len(win_idx)
    if loss_idx:
        stats.avg_hold_losers_h = sum(round_trips[i].holding_hours for i in loss_idx) / len(loss_idx)

    # Time-of-day (UTC) and day-of-week buckets
    from datetime import datetime, timezone
    hour_counts: dict[int, int] = defaultdict(int)
    dow_counts: dict[int, int] = defaultdict(int)
    for t in round_trips:
        dt = datetime.fromtimestamp(t.entry_time_ms / 1000, tz=timezone.utc)
        hour_counts[dt.hour] += 1
        dow_counts[dt.weekday()] += 1
    stats.hour_buckets = dict(sorted(hour_counts.items()))
    stats.dow_buckets = dict(sorted(dow_counts.items()))

    # Position size quartiles
    sizes = sorted(t.peak_size for t in round_trips)
    if sizes:
        q1_idx = int(len(sizes) * 0.25)
        q2_idx = int(len(sizes) * 0.50)
        q3_idx = int(len(sizes) * 0.75)
        stats.size_quartiles = [sizes[q1_idx], sizes[q2_idx], sizes[q3_idx]]

    # Funding
    stats.total_funding_paid = sum(t.funding_pnl for t in round_trips if t.funding_pnl < 0)
    stats.total_funding_earned = sum(t.funding_pnl for t in round_trips if t.funding_pnl > 0)

    # Liquidations
    stats.liquidation_count = sum(1 for t in round_trips if t.is_liquidation)

    # Per-asset breakdown
    asset_trips: dict[str, list] = defaultdict(list)
    for t in round_trips:
        asset_trips[t.coin].append(t)

    for coin, trips in asset_trips.items():
        asset_pnls = [t.realized_pnl + t.funding_pnl - t.fees_paid for t in trips]
        a_winners = [p for p in asset_pnls if p > 0]
        a_losers = [p for p in asset_pnls if p <= 0]
        a_gross_wins = sum(a_winners)
        a_gross_losses = abs(sum(a_losers))
        stats.by_asset[coin] = {
            "count": len(trips),
            "win_rate": len(a_winners) / len(trips) if trips else 0,
            "total_pnl": sum(asset_pnls),
            "avg_win": sum(a_winners) / len(a_winners) if a_winners else 0,
            "avg_loss": sum(a_losers) / len(a_losers) if a_losers else 0,
            "profit_factor": a_gross_wins / a_gross_losses if a_gross_losses > 0 else float("inf"),
        }

    return stats


def stats_to_dict(stats: TraderStats) -> dict:
    """Convert TraderStats to a plain dict for JSON serialisation and LLM prompt."""
    def fmt(v, decimals=2):
        if v is None:
            return None
        if isinstance(v, float):
            if v == float("inf"):
                return "∞"
            return round(v, decimals)
        return v

    return {
        "address": stats.display_address,
        "total_trades": stats.total_trades,
        "low_confidence": stats.low_confidence,
        "win_rate_pct": fmt(stats.win_rate * 100),
        "avg_win_usd": fmt(stats.avg_win),
        "avg_loss_usd": fmt(stats.avg_loss),
        "win_loss_ratio": fmt(stats.win_loss_ratio),
        "expectancy_usd": fmt(stats.expectancy),
        "profit_factor": fmt(stats.profit_factor),
        "total_pnl_usd": fmt(stats.total_pnl),
        "max_drawdown_usd": fmt(stats.max_drawdown),
        "avg_leverage": fmt(stats.avg_leverage),
        "peak_leverage": fmt(stats.peak_leverage),
        "avg_hold_winners_hours": fmt(stats.avg_hold_winners_h),
        "avg_hold_losers_hours": fmt(stats.avg_hold_losers_h),
        "long_count": stats.long_count,
        "short_count": stats.short_count,
        "long_pnl_usd": fmt(stats.long_pnl),
        "short_pnl_usd": fmt(stats.short_pnl),
        "total_funding_paid_usd": fmt(stats.total_funding_paid),
        "total_funding_earned_usd": fmt(stats.total_funding_earned),
        "liquidation_count": stats.liquidation_count,
        "hour_buckets": stats.hour_buckets,
        "dow_buckets": stats.dow_buckets,
        "size_quartiles": [fmt(q) for q in stats.size_quartiles],
        "by_asset": {
            coin: {k: fmt(v) if isinstance(v, float) else v for k, v in d.items()}
            for coin, d in stats.by_asset.items()
        },
    }


def patch_leverage_from_portfolio(round_trips: list, portfolio: dict) -> None:
    """
    Approximate account-level effective leverage at each trade's entry time.

    Hyperliquid does not store per-fill historical leverage. The best available
    proxy is: effective_leverage = total_open_notional / account_equity_at_entry.

    account_equity comes from portfolio.accountValueHistory (hourly snapshots).
    total_open_notional is reconstructed from the round trips that were open
    at each entry timestamp.

    Mutates each RoundTrip.leverage in-place. Skips if portfolio data is absent.
    """
    # portfolio response is a list of [window_name, {accountValueHistory: [...]}]
    # Use perpAllTime for max coverage, fall back to allTime or longest window.
    history = None
    if isinstance(portfolio, list):
        window_map = {item[0]: item[1] for item in portfolio if isinstance(item, list) and len(item) == 2}
        for preferred in ("perpAllTime", "allTime", "perpMonth", "month"):
            if preferred in window_map:
                history = window_map[preferred].get("accountValueHistory")
                break
        if not history:
            # Take whichever window has the most entries
            best = max(window_map.values(), key=lambda d: len(d.get("accountValueHistory", [])), default=None)
            if best:
                history = best.get("accountValueHistory")
    if not history:
        return

    # Each entry is [timestamp_ms, equity_str]
    equity_timeline: list[tuple[int, float]] = []
    for entry in history:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            equity_timeline.append((int(entry[0]), _safe_float(entry[1])))
    if not equity_timeline:
        return
    equity_timeline.sort(key=lambda x: x[0])

    def equity_at(ts_ms: int) -> float | None:
        """Return the equity snapshot closest to (but not after) ts_ms."""
        lo, hi = 0, len(equity_timeline) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if equity_timeline[mid][0] <= ts_ms:
                best = equity_timeline[mid][1]
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    # Sort trips by entry time so we can compute concurrent open notional
    sorted_trips = sorted(round_trips, key=lambda t: t.entry_time_ms)

    for trip in sorted_trips:
        equity = equity_at(trip.entry_time_ms)
        if not equity or equity <= 0:
            continue
        # All trips open at this entry time (started before, not yet closed)
        concurrent_notional = sum(
            abs(t.peak_size * t.avg_entry_px)
            for t in sorted_trips
            if t.entry_time_ms <= trip.entry_time_ms < t.exit_time_ms
        )
        if concurrent_notional > 0:
            trip.leverage = round(concurrent_notional / equity, 1)


def build_trade_table(round_trips: list, max_rows: int = 60) -> str:
    """
    Produce a plain-text table of round trips for the LLM prompt.
    Most recent first.
    """
    from datetime import datetime, timezone

    if not round_trips:
        return "(no trades)"

    sorted_trips = sorted(round_trips, key=lambda t: t.entry_time_ms, reverse=True)[:max_rows]
    lines = ["date_utc            | coin        | dir   | net_pnl_usd | hold_h | entry_px   | exit_px    | lev"]
    lines.append("-" * 95)
    for t in sorted_trips:
        dt = datetime.fromtimestamp(t.entry_time_ms / 1000, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d %H:%M")
        net = t.realized_pnl + t.funding_pnl - t.fees_paid
        pnl_str = f"+${net:,.2f}" if net >= 0 else f"-${abs(net):,.2f}"
        lev_str = f"{t.leverage:.0f}x" if t.leverage else "?"
        coin_str = t.coin[:11].ljust(11)
        lines.append(
            f"{date_str} | {coin_str} | {t.direction[:5].ljust(5)} | "
            f"{pnl_str:>11} | {t.holding_hours:>6.1f} | "
            f"{t.avg_entry_px:>10.2f} | {t.avg_exit_px:>10.2f} | {lev_str}"
        )
    return "\n".join(lines)
