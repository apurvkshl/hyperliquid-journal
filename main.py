"""
Orchestrator for hyperliquid-journal.

Steps:
1. Load config
2. Confirm asset universe (verify target assets exist on Hyperliquid)
3. If WALLET_ADDRESSES empty, fetch leaderboard and pick 3 active + 2 dormant
4. For each wallet: fetch fills → reconstruct trades → compute stats → LLM analysis → render HTML
5. Generate index.html
6. Print summary
"""

import json
import logging
import os
import time
from pathlib import Path

import config
from fetch import (
    get_fills,
    get_funding_history,
    get_meta,
    get_portfolio,
    pick_wallets_asset_first,
)
from trades import (
    RoundTrip,
    TraderStats,
    build_trade_table,
    compute_stats,
    patch_leverage_from_portfolio,
    reconstruct_round_trips,
    stats_to_dict,
)
from analysis import run_analysis
from report import render_index, render_trader_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


# ── Step 1 & 2: Universe confirmation ─────────────────────────────────────────

def confirm_target_assets() -> tuple[set, list]:
    """
    Check which of the configured TARGET_ASSETS actually exist on Hyperliquid.
    Returns (confirmed_set, asset_report_lines).
    """
    logger.info("Fetching perp universe from Hyperliquid...")
    universe = get_meta()
    universe_names = {u.get("name", "") for u in universe}

    # For builder perps (cash:, xyz:) we verify via candle data
    from fetch import get_candles
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 2 * 24 * 60 * 60 * 1000

    confirmed = set()
    report_lines = []

    for asset in config.TARGET_ASSETS:
        if asset in universe_names:
            confirmed.add(asset)
            report_lines.append(f"  CONFIRMED (regular perp): {asset}")
        else:
            # Try candle data to verify builder perp
            candles = get_candles(asset, "1d", start_ms, now_ms)
            if candles:
                confirmed.add(asset)
                last_close = candles[-1].get("c", "?")
                report_lines.append(
                    f"  CONFIRMED (builder perp): {asset}  last_close={last_close}"
                )
            else:
                report_lines.append(f"  NOT FOUND: {asset}")

    return confirmed, report_lines


# ── Step 3: Wallet selection ───────────────────────────────────────────────────

def resolve_wallets() -> tuple[list, list]:
    """
    Returns (active_addresses, low_activity_addresses).
    If config.WALLET_ADDRESSES is set, uses those (all treated as active).
    Otherwise seeds from recentTrades on non-BTC/ETH target assets and scores.
    """
    if config.WALLET_ADDRESSES:
        logger.info("Using %d configured wallet addresses", len(config.WALLET_ADDRESSES))
        return config.WALLET_ADDRESSES, []

    non_btc_eth = [a for a in config.TARGET_ASSETS if a not in ("BTC", "ETH")]
    logger.info("Asset-first wallet selection from %d non-BTC/ETH assets...", len(non_btc_eth))
    active, low_activity = pick_wallets_asset_first(
        non_btc_eth_assets=non_btc_eth,
        target_assets=set(config.TARGET_ASSETS),
        active_count=config.ACTIVE_TRADER_COUNT,
        low_activity_count=config.DORMANT_TRADER_COUNT,
    )
    return active, low_activity


# ── Step 4: Per-wallet pipeline ───────────────────────────────────────────────

def process_wallet(
    address: str,
    confirmed_assets: set,
    date_range_days: int,
) -> tuple[TraderStats, str, str]:
    """
    Run the full pipeline for one wallet.
    Returns (stats, analysis_text, report_path).
    """
    logger.info("Processing wallet %s...", address[:20])

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - date_range_days * 24 * 60 * 60 * 1000

    # Fetch
    fills = get_fills(address)
    logger.info("  Fills: %d", len(fills))

    funding = get_funding_history(address, start_ms)
    logger.info("  Funding entries: %d", len(funding))

    # Reconstruct
    trips = reconstruct_round_trips(address, fills, funding, confirmed_assets)
    logger.info("  Round-trip trades reconstructed: %d", len(trips))

    # Approximate historical leverage from portfolio equity snapshots
    portfolio = get_portfolio(address)
    if portfolio:
        patch_leverage_from_portfolio(trips, portfolio)
        logger.info("  Leverage patched via portfolio snapshots")

    # Compute stats
    stats = compute_stats(address, trips)

    # LLM analysis
    stats_d = stats_to_dict(stats)
    trade_table = build_trade_table(trips)

    if stats.error or stats.total_trades == 0:
        analysis_text = stats.error or "No trades in target assets."
    else:
        try:
            analysis_text = run_analysis(stats.display_address, stats_d, trade_table)
        except Exception as e:
            logger.warning("LLM analysis failed: %s", e)
            analysis_text = f"[LLM analysis failed: {e}]"

    # Render HTML
    report_path = render_trader_report(
        stats, analysis_text, config.OUTPUT_DIR, config.DATE_RANGE_DAYS
    )
    logger.info("  Report: %s", report_path)

    return stats, analysis_text, report_path


# ── Step 5 & 6: Summary ───────────────────────────────────────────────────────

def print_summary(
    asset_report: list,
    all_stats: list,
    report_paths: list,
    index_path: str,
):
    print()
    print("=" * 65)
    print("HYPERLIQUID JOURNAL — RUN COMPLETE")
    print("=" * 65)

    print("\n── Asset Discovery ──")
    for line in asset_report:
        print(line)

    print("\n── Wallets Analysed ──")
    for stats, path in zip(all_stats, report_paths):
        conf = " [LOW DATA]" if stats.low_confidence else ""
        err = f" — {stats.error}" if stats.error else ""
        print(
            f"  {stats.display_address}: "
            f"{stats.total_trades} trades, "
            f"PnL ${stats.total_pnl:,.0f}{conf}{err}"
        )

    print("\n── Output Files ──")
    print(f"  Index: {index_path}")
    for p in report_paths:
        print(f"  Report: {p}")

    # 5-line preview for the first wallet with trades
    for stats, _ in zip(all_stats, report_paths):
        if stats.total_trades > 0:
            print(f"\n── AI Insights Preview ({stats.display_address}) ──")
            print(f"  Trades: {stats.total_trades} | Win rate: {stats.win_rate:.1%} | "
                  f"Net PnL: ${stats.total_pnl:,.0f}")
            if stats.by_asset:
                best = max(stats.by_asset.items(), key=lambda x: x[1].get("total_pnl", 0))
                worst = min(stats.by_asset.items(), key=lambda x: x[1].get("total_pnl", 0))
                print(f"  Best asset: {best[0]} (PnL ${best[1].get('total_pnl', 0):,.0f})")
                print(f"  Worst asset: {worst[0]} (PnL ${worst[1].get('total_pnl', 0):,.0f})")
            print(f"  Avg hold winners: {stats.avg_hold_winners_h:.1f}h  "
                  f"Avg hold losers: {stats.avg_hold_losers_h:.1f}h")
            break

    print("\n── Honest Limitations ──")
    print("  • userFills API returns at most 2000 fills per wallet.")
    print("    High-frequency wallets may have far more historical fills.")
    print("    Use userFillsByTime for deeper history (not yet implemented).")
    print("  • Builder perps (cash:, xyz:) are separate liquidity pools;")
    print("    BRENT crude and S&P500 index perps were not found on Hyperliquid.")
    print("  • Leverage is approximated from portfolio equity snapshots (perpAllTime window).")
    print("    Trades that predate the wallet's first portfolio snapshot will show no leverage.")
    print("    Hyperliquid does not store per-fill leverage — no API or on-chain source exists.")
    print("  • Liquidation detection relies on 'Settlement' dir fills,")
    print("    which may also occur for expired contracts (not forced liq).")
    print()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    Path(config.CACHE_DIR).mkdir(parents=True, exist_ok=True)
    Path(config.OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    # Step 2: Confirm assets
    confirmed_assets, asset_report = confirm_target_assets()
    logger.info("Confirmed assets: %s", sorted(confirmed_assets))

    # Step 3: Wallets
    active, dormant = resolve_wallets()
    all_addresses = active + dormant

    if not all_addresses:
        logger.error("No wallet addresses available. Exiting.")
        return

    # Step 4: Per-wallet pipeline
    all_stats = []
    report_paths = []
    trader_summaries = []

    for address in all_addresses:
        try:
            stats, analysis_text, report_path = process_wallet(
                address, confirmed_assets, config.DATE_RANGE_DAYS
            )
        except Exception as e:
            logger.error("Failed to process wallet %s: %s", address[:20], e, exc_info=True)
            stats = TraderStats(
                address=address,
                display_address=f"{address[:6]}...{address[-4:]}",
                trades=[],
                error=str(e),
            )
            # Write error page
            report_path = render_trader_report(stats, "", config.OUTPUT_DIR)

        all_stats.append(stats)
        report_paths.append(report_path)

        fname = os.path.basename(report_path)
        trader_summaries.append({
            "address": address,
            "display_address": stats.display_address,
            "report_filename": fname,
            "total_pnl": stats.total_pnl,
            "total_trades": stats.total_trades,
            "win_rate": stats.win_rate,
            "assets": list(stats.by_asset.keys()),
            "low_confidence": stats.low_confidence,
            "error": stats.error,
        })

    # Step 5: Index
    index_path = render_index(trader_summaries, config.OUTPUT_DIR)

    # Step 6: Summary
    print_summary(asset_report, all_stats, report_paths, index_path)


if __name__ == "__main__":
    main()
