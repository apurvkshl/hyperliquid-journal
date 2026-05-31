# hyperliquid-journal

A trading journal that fetches fills from Hyperliquid, reconstructs round-trip trades across commodity and crypto perpetuals, and generates per-wallet HTML coaching reports powered by Claude.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```bash
python3 main.py
```

Reports are written to `reports/`. Open `reports/index.html` in a browser to browse all traders.

## Target assets

The pipeline looks for trades on: BTC, ETH, Gold, Silver, and WTI Crude Oil perpetuals (both regular and builder-perp variants). BRENT crude and S&P500 index perps were not found on Hyperliquid at the time of writing.

## Wallet selection

Leave `WALLET_ADDRESSES = []` in `config.py` to auto-select 3 active + 2 dormant wallets from the Hyperliquid leaderboard. Or set specific addresses:

```python
WALLET_ADDRESSES = ["0xabc...", "0xdef..."]
```

## Caching

Raw API responses are cached in `cache/` as JSON files (keyed by endpoint + params). Delete the cache directory or individual files to force a fresh fetch. The cache is especially useful for re-running analysis or re-rendering reports without hitting the Hyperliquid API again.

## Without an API key

If `ANTHROPIC_API_KEY` is not set, the pipeline still runs: fills are fetched, trades are reconstructed, stats are computed, and HTML reports are generated — but the coaching sections ("Why You're Losing", "Your Biases", etc.) will show placeholder text. Set the key and re-run to populate those sections.
