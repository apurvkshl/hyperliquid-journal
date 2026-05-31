"""
Configuration for hyperliquid-journal.
"""

# Leave empty to auto-fetch from leaderboard (3 active + 2 dormant)
WALLET_ADDRESSES = []

# Confirmed target assets from live API exploration (2025-05-21)
# BTC, ETH: standard Hyperliquid perps
# cash:GOLD, xyz:GOLD: Gold builder perps (~$4500)
# cash:SILVER, xyz:SILVER: Silver builder perps (~$75)
# cash:WTI, xyz:CL: WTI Crude Oil builder perps (~$99)
# xyz:BRENTOIL: Brent Crude Oil builder perp (~$102)  [trade.xyz]
# xyz:SP500: S&P 500 index builder perp (~$7426)      [trade.xyz]
TARGET_ASSETS = [
    "BTC",
    "ETH",
    "cash:GOLD",
    "xyz:GOLD",
    "cash:SILVER",
    "xyz:SILVER",
    "cash:WTI",
    "xyz:CL",
    "xyz:BRENTOIL",
    "xyz:SP500",
]

# Human-readable label for display
ASSET_DISPLAY_NAMES = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "cash:GOLD": "Gold (cash perp)",
    "xyz:GOLD": "Gold (xyz perp)",
    "cash:SILVER": "Silver (cash perp)",
    "xyz:SILVER": "Silver (xyz perp)",
    "cash:WTI": "WTI Crude Oil (cash perp)",
    "xyz:CL": "WTI Crude Oil (xyz perp)",
    "xyz:BRENTOIL": "Brent Crude Oil (trade.xyz perp)",
    "xyz:SP500": "S&P 500 Index (trade.xyz perp)",
}

DATE_RANGE_DAYS = 90   # look back 90 days from today

OUTPUT_DIR = "reports"
CACHE_DIR = "cache"

# Stats data endpoint (for leaderboard — separate from main API)
STATS_BASE_URL = "https://stats-data.hyperliquid.xyz"
BASE_URL = "https://api.hyperliquid.xyz/info"

# Leaderboard: pick top N by all-time volume as "active", bottom N as "dormant"
ACTIVE_TRADER_COUNT = 3
DORMANT_TRADER_COUNT = 2
