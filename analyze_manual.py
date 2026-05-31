"""
analyze_manual.py — process manually-provided trade data (CSV/table format)
and produce an HTML coaching report using the same pipeline as main.py.

Contract size assumptions (standard MT4/MT5 specs):
  XAUUSD : 100 oz / lot
  XAGUSD : 5000 oz / lot
  BRENT  : 1000 bbl / lot
  WTI    : 1000 bbl / lot
  BTCUSD : 1 BTC / lot
  ETHUSD : 1 ETH / lot
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from analysis import run_analysis
from report import render_trader_report
from trades import RoundTrip, TraderStats, build_trade_table, compute_stats, stats_to_dict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("manual")

OUTPUT_DIR = "reports"

# ── Contract multipliers (USD PnL = price_diff * lot_size * multiplier) ───────
CONTRACT_MULTIPLIER = {
    "XAUUSD": 100,
    "XAGUSD": 5000,
    "BRENT":  1000,
    "WTI":    1000,
    "BTCUSD": 1,
    "ETHUSD": 1,
}

# ── Raw trade data ─────────────────────────────────────────────────────────────
# Fields: num, date, time, instrument, direction, entry, sl, tp, exit, lot_size
RAW_TRADES = [
    (1,  "21/04/2026", "11:50", "XAUUSD", "Long",  4781.00, 4776.00,  4786.00,  4785.60, 0.10),
    (2,  "22/04/2026", "12:22", "BRENT",  "Short", 93.165,  96.135,   90.10,    96.135,  0.01),
    (3,  "22/04/2026", "11:43", "XAUUSD", "Long",  4755.00, 4751.84,  4760.00,  4751.84, 0.10),
    (4,  "28/04/2026", "17:43", "XAUUSD", "Short", 4579.00, 4597.00,  4539.00,  4597.00, 0.10),
    (5,  "28/04/2026", "20:11", "XAUUSD", "Short", 4562.00, 4609.00,  None,     4546.00, 0.10),
    (6,  "04/05/2026", "19:12", "XAUUSD", "Short", 4520.00, 4611.00,  4509.00,  4509.00, 0.10),
    (7,  "06/05/2026", "17:30", "XAUUSD", "Short", 4685.00, 4700.00,  4639.00,  4680.00, 0.10),
    (8,  "07/05/2026", "13:09", "XAGUSD", "Long",  79.49,   None,     None,     79.63,   0.01),
    (9,  "12/05/2026", "15:36", "ETHUSD", "Short", 2291.70, 2308.00,  2257.00,  2285.60, 1.00),
    (10, "12/05/2026", "17:38", "WTI",    "Long",  99.02,   98.15,    100.03,   98.15,   0.10),
    (11, "13/05/2026", "11:35", "WTI",    "Long",  97.45,   97.07,    98.44,    97.68,   0.10),
    (12, "13/05/2026", "12:33", "BTCUSD", "Long",  80997.0, 80632.00, 82618.00, 81113.0, 1.00),
    (13, "13/05/2026", "15:31", "BTCUSD", "Long",  81246.0, 80632.00, 82618.00, 80632.0, 1.00),
    (14, "13/05/2026", "14:57", "ETHUSD", "Long",  2318.10, 2296.00,  2383.00,  2296.00, 10.0),
]

WALLET_LABEL = "manual_trader"
DISPLAY_ADDRESS = "Manual Trader"


def parse_ts(date_str: str, time_str: str) -> int:
    """Return UTC timestamp in ms."""
    dt = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%Y %H:%M").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def build_round_trips() -> list[RoundTrip]:
    trips = []
    for row in RAW_TRADES:
        num, date, time_, instrument, direction, entry, sl, tp, exit_px, lot_size = row

        multiplier = CONTRACT_MULTIPLIER.get(instrument, 1)
        price_diff = (exit_px - entry) if direction == "Long" else (entry - exit_px)
        realized_pnl = price_diff * lot_size * multiplier

        # Determine if trade hit SL, TP, or neither
        if direction == "Long":
            hit_sl = sl is not None and abs(exit_px - sl) < 0.001
            hit_tp = tp is not None and abs(exit_px - tp) < 0.001
        else:
            hit_sl = sl is not None and abs(exit_px - sl) < 0.001
            hit_tp = tp is not None and abs(exit_px - tp) < 0.001

        # Planned risk in USD (for context)
        if sl is not None:
            sl_dist = abs(entry - sl)
            planned_risk = sl_dist * lot_size * multiplier
        else:
            planned_risk = None

        entry_ts = parse_ts(date, time_)
        # Assume 1h hold for display; real exit time unknown from data
        exit_ts = entry_ts + 3_600_000

        trip = RoundTrip(
            address=WALLET_LABEL,
            coin=instrument,
            direction=direction,
            entry_time_ms=entry_ts,
            exit_time_ms=exit_ts,
            avg_entry_px=entry,
            avg_exit_px=exit_px,
            peak_size=lot_size,
            total_size=lot_size,
            realized_pnl=realized_pnl,
            fees_paid=0.0,
            funding_pnl=0.0,
            holding_hours=1.0,
            leverage=None,
            is_liquidation=False,
        )
        # Annotate with hit_sl / hit_tp for extra context in the prompt
        trip._hit_sl = hit_sl          # type: ignore[attr-defined]
        trip._hit_tp = hit_tp          # type: ignore[attr-defined]
        trip._planned_risk = planned_risk  # type: ignore[attr-defined]
        trip._trade_num = num          # type: ignore[attr-defined]
        trips.append(trip)
    return trips


def build_detailed_table(trips: list) -> str:
    """Extended trade table with SL/TP outcome for richer LLM context."""
    lines = [
        "# | date       | instrument | dir   | entry      | exit       | pnl_usd     | outcome   | planned_risk_usd"
    ]
    lines.append("-" * 105)
    for t in trips:
        sl_hit = getattr(t, "_hit_sl", False)
        tp_hit = getattr(t, "_hit_tp", False)
        outcome = "HIT_SL" if sl_hit else ("HIT_TP" if tp_hit else "manual_exit")
        pr = getattr(t, "_planned_risk", None)
        pr_str = f"${pr:,.0f}" if pr else "n/a"
        num = getattr(t, "_trade_num", "?")
        dt = datetime.fromtimestamp(t.entry_time_ms / 1000, tz=timezone.utc)
        pnl = t.realized_pnl
        pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
        lines.append(
            f"{num:>2} | {dt.strftime('%Y-%m-%d')} | {t.coin:<10} | {t.direction:<5} | "
            f"{t.avg_entry_px:>10.2f} | {t.avg_exit_px:>10.2f} | {pnl_str:>11} | {outcome:<10} | {pr_str}"
        )
    return "\n".join(lines)


def build_manual_prompt(stats_dict: dict, trade_table: str) -> str:
    return f"""You are reviewing the trading history of a manual trader across multiple instruments (Gold, Silver, Brent, WTI crude oil, Bitcoin, Ethereum).

This data comes from a CFD/forex-style broker. Contract sizes assumed:
- XAUUSD: 100 oz/lot · XAGUSD: 5000 oz/lot · BRENT/WTI: 1000 bbl/lot · BTCUSD: 1 BTC/lot · ETHUSD: 1 ETH/lot

**Sample size is only 14 trades — treat all patterns as preliminary hypotheses.**

## Ground-truth statistics (computed from raw data)

{json.dumps(stats_dict, indent=2)}

## Full trade log (entry → exit, with SL/TP outcome)

{trade_table}

## What I need from you

Write a coaching report with these sections. Be specific and cite the actual trade numbers (Trade #1, #2, etc.) and prices.

### 1. The Verdict (1 paragraph)
What kind of trader is this and what is the single most expensive thing they are doing?

### 2. Why You're Losing
The recurring reasons losing trades lose. Cite specific trades with dollar amounts.

### 3. Your Biases
Test each of these — confirm or refute with evidence from the trades:
- Disposition effect: cutting winners early vs holding losers to full stop
- Revenge trading / position-size escalation after losses
- Overtrading a single session (May 13 had 3 trades, check sizing)
- Poor R:R on individual trades (TP too close, SL too wide)
- FOMO entries after a move has already run

### 4. What's Working
Where is the edge? Which instruments / directions show positive expectancy?

### 5. One Thing to Change Next
Single, concrete, data-driven action. Actionable tomorrow.

Cite specific trade numbers throughout. No filler."""


def main():
    Path(OUTPUT_DIR).mkdir(exist_ok=True)

    logger.info("Building round trips from manual data...")
    trips = build_round_trips()

    logger.info("Computing stats...")
    stats = compute_stats(WALLET_LABEL, trips)
    # Override display fields for manual trader
    stats.display_address = DISPLAY_ADDRESS
    stats.address = WALLET_LABEL

    stats_d = stats_to_dict(stats)
    trade_table = build_detailed_table(trips)

    logger.info("Stats: %d trades, win_rate=%.1f%%, net_pnl=$%.2f",
                stats.total_trades, stats.win_rate * 100, stats.total_pnl)

    logger.info("Running LLM analysis...")
    try:
        # Build a custom prompt — bypass build_user_prompt to use our richer table
        import subprocess, shutil
        from analysis import SYSTEM_PROMPT, MODEL

        user_prompt = build_manual_prompt(stats_d, trade_table)

        result = subprocess.run(
            [
                "claude", "-p",
                "--setting-sources", "project,local",
                "--system-prompt", SYSTEM_PROMPT,
                "--output-format", "text",
                "--tools", "",
                "--model", MODEL,
            ],
            input=user_prompt,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(result.stderr.strip()[:300])
        analysis_text = result.stdout.strip()
        logger.info("LLM analysis complete.")
    except Exception as e:
        logger.warning("LLM analysis failed: %s", e)
        analysis_text = f"[LLM analysis failed: {e}]"

    logger.info("Rendering HTML report...")
    report_path = render_trader_report(stats, analysis_text, OUTPUT_DIR, date_range_days=None)
    logger.info("Report written: %s", report_path)
    print(f"\nReport: {report_path}")


if __name__ == "__main__":
    main()
