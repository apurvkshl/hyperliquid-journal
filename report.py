"""
HTML report renderer.

Generates a self-contained dark-mode HTML file per trader.
Also generates an index.html linking all trader reports.

Design constraints:
- Inline CSS only, no external deps
- Dark background (#0f1117), white text, monospaced numbers
- Inline SVG equity curve
- Low-confidence banner when trade count < 20
"""

import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from trades import RoundTrip, TraderStats


# ── CSS ────────────────────────────────────────────────────────────────────────

BASE_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: #0f1117;
  color: #e2e8f0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  font-size: 15px;
  line-height: 1.65;
  padding: 2rem 1rem;
}
.container { max-width: 900px; margin: 0 auto; }
h1 { font-size: 1.6rem; color: #f8fafc; margin-bottom: 0.3rem; }
h2 { font-size: 1.15rem; color: #94a3b8; font-weight: 500; margin-bottom: 1.5rem; }
h3 { font-size: 1.05rem; color: #cbd5e1; margin: 2rem 0 0.7rem; border-bottom: 1px solid #1e293b; padding-bottom: 0.3rem; }
p, li { margin-bottom: 0.5rem; }
ul, ol { padding-left: 1.4rem; }
code, .mono { font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace; font-size: 0.875em; }

/* Header */
.header { margin-bottom: 2.5rem; }
.header .wallet { font-size: 1.3rem; color: #f1f5f9; font-family: monospace; letter-spacing: 0.03em; }
.header .period  { color: #64748b; font-size: 0.9rem; margin-top: 0.2rem; }
.header .pnl     { font-size: 2rem; font-weight: 700; margin-top: 0.5rem; font-family: monospace; }
.pnl.pos  { color: #4ade80; }
.pnl.neg  { color: #f87171; }
.pnl.zero { color: #94a3b8; }

/* Low-confidence banner */
.banner {
  background: #451a03;
  border: 1px solid #92400e;
  border-radius: 6px;
  padding: 0.75rem 1rem;
  margin-bottom: 1.5rem;
  color: #fbbf24;
  font-size: 0.9rem;
}

/* Stats table */
.stats-table { width: 100%; border-collapse: collapse; margin: 1rem 0 2rem; }
.stats-table th {
  background: #1e293b;
  color: #94a3b8;
  text-align: left;
  padding: 0.5rem 0.75rem;
  font-size: 0.8rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.stats-table td {
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid #1e293b;
  font-family: monospace;
  font-size: 0.9rem;
}
.stats-table tr:hover td { background: #1a2234; }
.num-pos { color: #4ade80; }
.num-neg { color: #f87171; }
.num-neutral { color: #93c5fd; }

/* Markdown-rendered analysis */
.analysis { margin-top: 1rem; }
.analysis h3 { color: #e2e8f0; border-color: #334155; }
.analysis p  { color: #cbd5e1; }
.analysis li { color: #cbd5e1; }
.analysis strong { color: #f1f5f9; }

/* Asset chips */
.chips { display: flex; gap: 0.5rem; flex-wrap: wrap; margin: 0.5rem 0 1.5rem; }
.chip {
  background: #1e293b;
  border: 1px solid #334155;
  border-radius: 999px;
  padding: 0.2rem 0.75rem;
  font-size: 0.8rem;
  font-family: monospace;
  color: #7dd3fc;
}

/* Equity curve */
.equity-wrap { margin: 1.5rem 0; }
.equity-wrap svg { display: block; width: 100%; border-radius: 6px; background: #0d1420; }

/* Footer */
.footer { margin-top: 3rem; font-size: 0.75rem; color: #334155; border-top: 1px solid #1e293b; padding-top: 1rem; }
.no-data { color: #475569; font-style: italic; padding: 2rem 0; text-align: center; }
"""

# ── Helpers ────────────────────────────────────────────────────────────────────

def _e(s: str) -> str:
    """HTML-escape a string."""
    return html.escape(str(s))


def _pnl_class(v: float) -> str:
    if v > 0:
        return "num-pos"
    if v < 0:
        return "num-neg"
    return "num-neutral"


def _fmt_usd(v: float) -> str:
    if v >= 0:
        return f"+${v:,.2f}"
    return f"-${abs(v):,.2f}"


def _fmt_pct(v: float) -> str:
    return f"{v:.1f}%"


def _md_to_html(text: str) -> str:
    """
    Very light Markdown → HTML conversion (no external dep).
    Handles: ### headings, **bold**, bullet lists, blank-line paragraphs.
    """
    import re
    lines = text.split("\n")
    out = []
    in_ul = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Headings
        if stripped.startswith("### "):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(f"<h3>{_e(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(f"<h3>{_e(stripped[3:])}</h3>")
        elif stripped.startswith("# "):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(f"<h3>{_e(stripped[2:])}</h3>")
        # Bullet list
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            content = _inline_md(stripped[2:])
            out.append(f"<li>{content}</li>")
        # Numbered list
        elif re.match(r"^\d+\.\s", stripped):
            content = _inline_md(re.sub(r"^\d+\.\s", "", stripped))
            out.append(f"<li>{content}</li>")
        # Blank line
        elif stripped == "":
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append("")
        # Normal paragraph text
        else:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(f"<p>{_inline_md(stripped)}</p>")
        i += 1

    if in_ul:
        out.append("</ul>")

    return "\n".join(out)


def _inline_md(text: str) -> str:
    """Convert **bold** and `code` inline Markdown."""
    import re
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: f"<strong>{_e(m.group(1))}</strong>", text)
    # Code
    text = re.sub(r"`(.+?)`", lambda m: f"<code>{_e(m.group(1))}</code>", text)
    # Escape remaining HTML but preserve our tags
    # (already escaped in bold/code replacements above; plain text needs escaping)
    return text


def _equity_svg(round_trips: list, width: int = 860, height: int = 180) -> str:
    """
    Build an inline SVG equity curve from ordered round trips.
    Returns empty string if no trips.
    """
    if not round_trips:
        return ""

    sorted_trips = sorted(round_trips, key=lambda t: t.entry_time_ms)
    cumulative = []
    running = 0.0
    for t in sorted_trips:
        net = t.realized_pnl + t.funding_pnl - t.fees_paid
        running += net
        cumulative.append(running)

    if len(cumulative) < 2:
        return ""

    pad = 30
    w = width - 2 * pad
    h = height - 2 * pad

    min_v = min(cumulative)
    max_v = max(cumulative)
    span = max_v - min_v if max_v != min_v else 1.0

    n = len(cumulative)

    def to_x(i):
        return pad + (i / (n - 1)) * w

    def to_y(v):
        return pad + h - ((v - min_v) / span) * h

    # Build polyline points
    pts = " ".join(f"{to_x(i):.1f},{to_y(v):.1f}" for i, v in enumerate(cumulative))

    # Zero line
    zero_y = to_y(0.0)
    if min_v <= 0 <= max_v:
        zero_line = f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{pad+w}" y2="{zero_y:.1f}" stroke="#334155" stroke-width="1" stroke-dasharray="4,4"/>'
    else:
        zero_line = ""

    # Color: green if endpoint positive, red otherwise
    final_color = "#4ade80" if cumulative[-1] >= 0 else "#f87171"

    # Gradient fill under curve
    grad_id = "eqgrad"
    grad = f"""<defs>
      <linearGradient id="{grad_id}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="{final_color}" stop-opacity="0.35"/>
        <stop offset="100%" stop-color="{final_color}" stop-opacity="0.02"/>
      </linearGradient>
    </defs>"""

    # Build fill polygon (close at bottom)
    first_x = to_x(0)
    last_x = to_x(n - 1)
    bottom = pad + h
    poly_pts = f"{first_x:.1f},{bottom:.1f} {pts} {last_x:.1f},{bottom:.1f}"

    # Labels
    fmt_pnl = lambda v: f"${v:+,.0f}"
    label_max = f'<text x="{pad+4}" y="{to_y(max_v)-4:.1f}" fill="#94a3b8" font-size="10" font-family="monospace">{fmt_pnl(max_v)}</text>'
    label_min = f'<text x="{pad+4}" y="{to_y(min_v)+12:.1f}" fill="#94a3b8" font-size="10" font-family="monospace">{fmt_pnl(min_v)}</text>'

    svg = f"""<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
  {grad}
  {zero_line}
  <polygon points="{poly_pts}" fill="url(#{grad_id})"/>
  <polyline points="{pts}" fill="none" stroke="{final_color}" stroke-width="2" stroke-linejoin="round"/>
  {label_max}
  {label_min}
</svg>"""
    return svg


# ── Main render functions ──────────────────────────────────────────────────────

def render_trader_report(
    stats: TraderStats,
    analysis_text: str,
    output_dir: str,
    date_range_days: int = 90,
) -> str:
    """
    Render and write a self-contained HTML report for one trader.
    Returns the output file path.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    filename = f"trader_{stats.address.replace('0x', '').lower()[:16]}.html"
    out_path = os.path.join(output_dir, filename)

    if stats.error or stats.total_trades == 0:
        html_content = _render_no_data(stats)
    else:
        html_content = _render_full_report(stats, analysis_text, date_range_days)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return out_path


def _render_no_data(stats: TraderStats) -> str:
    msg = _e(stats.error or "No trades found in target assets for this wallet.")
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>No Data — {_e(stats.display_address)}</title>
<style>{BASE_CSS}</style></head>
<body><div class="container">
  <div class="header">
    <div class="wallet">{_e(stats.display_address)}</div>
    <div class="pnl zero">Insufficient Data</div>
  </div>
  <p class="no-data">{msg}</p>
</div></body></html>"""


def _render_full_report(
    stats: TraderStats, analysis_text: str, date_range_days: int
) -> str:
    addr = _e(stats.display_address)

    # PnL header
    pnl_cls = _pnl_class(stats.total_pnl)
    pnl_str = _e(_fmt_usd(stats.total_pnl))

    # Period
    if stats.trades:
        oldest_ms = min(t.entry_time_ms for t in stats.trades)
        newest_ms = max(t.exit_time_ms for t in stats.trades)
        oldest_dt = datetime.fromtimestamp(oldest_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        newest_dt = datetime.fromtimestamp(newest_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        period_str = f"{oldest_dt} → {newest_dt}"
    else:
        period_str = f"Last {date_range_days} days"

    # Assets traded chips
    assets_html = "".join(f'<span class="chip">{_e(a)}</span>' for a in sorted(stats.by_asset.keys()))

    # Low-confidence banner
    banner_html = ""
    if stats.low_confidence:
        banner_html = (
            '<div class="banner">'
            '⚠ LOW DATA — fewer than 20 round-trip trades. '
            'All patterns below are preliminary hypotheses, not conclusions.'
            '</div>'
        )

    # Stats table
    lev_avg = f"{stats.avg_leverage:.1f}x" if stats.avg_leverage else "N/A"
    lev_peak = f"{stats.peak_leverage:.1f}x" if stats.peak_leverage else "N/A"
    pf_str = "∞" if stats.profit_factor == float("inf") else f"{stats.profit_factor:.2f}"

    def stat_row(label, value, css_class="num-neutral"):
        return f'<tr><td>{_e(label)}</td><td class="{css_class} mono">{_e(str(value))}</td></tr>'

    stats_rows = "".join([
        stat_row("Total round-trip trades", stats.total_trades),
        stat_row("Win rate", _fmt_pct(stats.win_rate * 100),
                 "num-pos" if stats.win_rate >= 0.5 else "num-neg"),
        stat_row("Avg win", _fmt_usd(stats.avg_win), "num-pos"),
        stat_row("Avg loss", _fmt_usd(stats.avg_loss), "num-neg"),
        stat_row("Win/Loss ratio", f"{stats.win_loss_ratio:.2f}"),
        stat_row("Expectancy", _fmt_usd(stats.expectancy),
                 _pnl_class(stats.expectancy)),
        stat_row("Profit factor", pf_str,
                 "num-pos" if stats.profit_factor > 1 else "num-neg"),
        stat_row("Net PnL (after fees & funding)", _fmt_usd(stats.total_pnl),
                 _pnl_class(stats.total_pnl)),
        stat_row("Max drawdown", _fmt_usd(-stats.max_drawdown), "num-neg"),
        stat_row("Avg leverage", lev_avg),
        stat_row("Peak leverage", lev_peak),
        stat_row("Long trades / PnL",
                 f"{stats.long_count} / {_fmt_usd(stats.long_pnl)}",
                 _pnl_class(stats.long_pnl)),
        stat_row("Short trades / PnL",
                 f"{stats.short_count} / {_fmt_usd(stats.short_pnl)}",
                 _pnl_class(stats.short_pnl)),
        stat_row("Avg hold time — winners", f"{stats.avg_hold_winners_h:.1f}h"),
        stat_row("Avg hold time — losers",  f"{stats.avg_hold_losers_h:.1f}h"),
        stat_row("Funding earned", _fmt_usd(stats.total_funding_earned), "num-pos"),
        stat_row("Funding paid",   _fmt_usd(abs(stats.total_funding_paid)),  "num-neg"),
        stat_row("Liquidations", stats.liquidation_count,
                 "num-neg" if stats.liquidation_count > 0 else "num-neutral"),
    ])

    # Per-asset table
    asset_rows = ""
    for coin in sorted(stats.by_asset.keys()):
        d = stats.by_asset[coin]
        pf = d.get("profit_factor", 0)
        pf_disp = "∞" if pf == float("inf") or pf == "∞" else f"{pf:.2f}"
        total = d.get("total_pnl", 0) or 0
        asset_rows += (
            f'<tr>'
            f'<td class="mono">{_e(coin)}</td>'
            f'<td>{d.get("count", 0)}</td>'
            f'<td class="mono">{_fmt_pct((d.get("win_rate") or 0) * 100)}</td>'
            f'<td class="mono {_pnl_class(total)}">{_fmt_usd(total)}</td>'
            f'<td class="mono">{pf_disp}</td>'
            f'</tr>'
        )

    # Equity curve SVG
    svg = _equity_svg(stats.trades)
    equity_section = ""
    if svg:
        equity_section = f'<div class="equity-wrap"><h3>Equity Curve</h3>{svg}</div>'

    # Analysis section
    analysis_html = _md_to_html(analysis_text) if analysis_text else "<p><em>No analysis available.</em></p>"

    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trading Report — {addr}</title>
  <style>{BASE_CSS}</style>
</head>
<body>
<div class="container">

  {banner_html}

  <div class="header">
    <div class="wallet">{addr}</div>
    <div class="period">{_e(period_str)}</div>
    <div class="pnl {pnl_cls}">{pnl_str}</div>
  </div>

  <div class="chips">{assets_html}</div>

  {equity_section}

  <h3>The Numbers</h3>
  <table class="stats-table">
    <thead><tr><th>Metric</th><th>Value</th></tr></thead>
    <tbody>{stats_rows}</tbody>
  </table>

  <h3>Per-Asset Breakdown</h3>
  <table class="stats-table">
    <thead><tr><th>Asset</th><th>Trades</th><th>Win Rate</th><th>Net PnL</th><th>Profit Factor</th></tr></thead>
    <tbody>{asset_rows}</tbody>
  </table>

  <div class="analysis">
    {analysis_html}
  </div>

  <div class="footer">
    Generated by hyperliquid-journal on {_e(now_str)}. Data from Hyperliquid API. Not financial advice.
  </div>

</div>
</body>
</html>"""


# ── Index page ─────────────────────────────────────────────────────────────────

def render_index(
    trader_summaries: list,
    output_dir: str,
) -> str:
    """
    Render index.html with one-line summaries linking to each trader report.
    trader_summaries: list of dicts with keys:
      address, display_address, report_filename, total_pnl, total_trades,
      win_rate, assets, low_confidence, error
    Returns output path.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    out_path = os.path.join(output_dir, "index.html")

    rows = ""
    for s in trader_summaries:
        display = _e(s.get("display_address", s.get("address", "?")))
        fname = _e(s.get("report_filename", "#"))
        pnl = s.get("total_pnl", 0) or 0
        pnl_cls = _pnl_class(pnl)
        pnl_str = _e(_fmt_usd(pnl))
        n = s.get("total_trades", 0)
        wr = s.get("win_rate", 0) or 0
        assets = ", ".join(sorted(s.get("assets", []))) or "—"
        error = s.get("error", "")
        lc = " ⚠" if s.get("low_confidence") else ""

        if error:
            rows += f"""<tr>
              <td><a href="{fname}" style="color:#7dd3fc">{display}</a>{_e(lc)}</td>
              <td colspan="4" style="color:#475569;font-style:italic">{_e(error)}</td>
            </tr>"""
        else:
            rows += f"""<tr>
              <td><a href="{fname}" style="color:#7dd3fc">{display}</a>{_e(lc)}</td>
              <td class="{pnl_cls} mono">{pnl_str}</td>
              <td class="mono">{n}</td>
              <td class="mono">{_fmt_pct(wr * 100)}</td>
              <td class="mono" style="color:#64748b;font-size:0.8em">{_e(assets)}</td>
            </tr>"""

    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hyperliquid Journal — Reports</title>
  <style>
    {BASE_CSS}
    a {{ color: #7dd3fc; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Hyperliquid Trade Analysis</h1>
    <h2>Trader Report Index</h2>
  </div>

  <table class="stats-table" style="margin-bottom:2rem">
    <thead>
      <tr>
        <th>Wallet</th>
        <th>Net PnL</th>
        <th>Trades</th>
        <th>Win Rate</th>
        <th>Assets</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>

  <div class="footer">
    Generated by hyperliquid-journal on {_e(now_str)}.
    Target assets: BTC, ETH, cash:GOLD/xyz:GOLD, cash:SILVER/xyz:SILVER, cash:WTI/xyz:CL.
    BRENT and S&amp;P500 index perps not found on Hyperliquid.
    Not financial advice.
  </div>
</div>
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)

    return out_path
