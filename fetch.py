"""
Hyperliquid API fetch layer with disk cache.

All API calls are POST to BASE_URL with JSON bodies.
Cache TTL: 6 hours. Cache files: cache/<type>_<key>_<timestamp>.json

Field shapes confirmed from live API on 2025-05-21:
  userFills:   list of {coin, px, sz, side, time, startPosition, dir,
                        closedPnl, hash, oid, crossed, fee, tid, feeToken, twapId}
  userFunding: list of {time, hash, delta: {type, coin, usdc, szi, fundingRate, nSamples}}
  clearinghouseState: {marginSummary, crossMarginSummary, withdrawable,
                       assetPositions: [{type, position: {coin, szi, leverage, entryPx,
                                         positionValue, unrealizedPnl, cumFunding, ...}}]}
  candleSnapshot: list of {t, T, s, i, o, c, h, l, v, n}
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import requests

from config import BASE_URL, CACHE_DIR, STATS_BASE_URL

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 6 * 3600  # 6 hours


def _cache_path(cache_type: str, key: str) -> Path:
    """Return path for a cache file. Key should be alphanumeric-safe."""
    safe_key = key.replace(":", "_").replace("/", "_")
    return Path(CACHE_DIR) / f"{cache_type}_{safe_key}.json"


def _load_cache(path: Path) -> Optional[Any]:
    """Return cached data if file exists and is within TTL, else None."""
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > CACHE_TTL_SECONDS:
        logger.debug("Cache expired for %s (age=%.0fs)", path.name, age)
        return None
    with open(path) as f:
        return json.load(f)


def _save_cache(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _post(body: dict, retries: int = 3) -> Any:
    """POST to BASE_URL with rate limiting and retry."""
    time.sleep(0.3)
    for attempt in range(retries):
        try:
            resp = requests.post(BASE_URL, json=body, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logger.warning("HTTP error on attempt %d: %s", attempt + 1, e)
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
        except Exception as e:
            logger.warning("Request error on attempt %d: %s", attempt + 1, e)
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    logger.error("All retries failed for body: %s", str(body)[:200])
    return None


def get_meta() -> Optional[list]:
    """Return the universe list from /info meta endpoint."""
    cache_key = _cache_path("meta", "universe")
    cached = _load_cache(cache_key)
    if cached is not None:
        logger.debug("Cache hit: meta")
        return cached

    data = _post({"type": "meta"})
    if data and "universe" in data:
        result = data["universe"]
        _save_cache(cache_key, result)
        return result
    logger.warning("meta endpoint returned unexpected shape: %s", str(data)[:200])
    return []


def get_fills(address: str) -> list:
    """
    Return all fills for a wallet (up to API limit of 2000).
    Real field names: coin, px, sz, side (A=ask/sell, B=bid/buy),
    dir (Open Long / Close Long / Open Short / Close Short / Settlement),
    time (ms), startPosition, closedPnl, fee, hash, oid, tid, feeToken.
    """
    address = address.lower()
    cache_key = _cache_path("fills", address)
    cached = _load_cache(cache_key)
    if cached is not None:
        logger.debug("Cache hit: fills %s", address[:10])
        return cached

    data = _post({"type": "userFills", "user": address})
    if data is None:
        return []
    if not isinstance(data, list):
        logger.warning("Unexpected fills shape for %s: %s", address[:10], type(data))
        return []
    _save_cache(cache_key, data)
    return data


def get_fills_by_time(address: str, start_time_ms: int, end_time_ms: int) -> list:
    """Return fills for a wallet within a time range."""
    address = address.lower()
    cache_key = _cache_path("fills_time", f"{address}_{start_time_ms}_{end_time_ms}")
    cached = _load_cache(cache_key)
    if cached is not None:
        return cached

    data = _post({
        "type": "userFillsByTime",
        "user": address,
        "startTime": start_time_ms,
        "endTime": end_time_ms,
    })
    if data is None:
        return []
    if not isinstance(data, list):
        logger.warning("Unexpected fillsByTime shape: %s", type(data))
        return []
    _save_cache(cache_key, data)
    return data


def get_state(address: str) -> Optional[dict]:
    """
    Return clearinghouseState for a wallet.
    Keys: marginSummary, crossMarginSummary, withdrawable, assetPositions, time
    assetPositions[i].position: {coin, szi, leverage:{type,value}, entryPx,
                                  positionValue, unrealizedPnl, cumFunding:{allTime,...}}
    """
    address = address.lower()
    cache_key = _cache_path("state", address)
    cached = _load_cache(cache_key)
    if cached is not None:
        return cached

    data = _post({"type": "clearinghouseState", "user": address})
    if data is None:
        return {}
    _save_cache(cache_key, data)
    return data


def get_funding_history(address: str, start_time_ms: int) -> list:
    """
    Return funding payment history.
    Each entry: {time, hash, delta: {type, coin, usdc, szi, fundingRate, nSamples}}
    usdc is positive when you receive funding, negative when you pay.
    """
    address = address.lower()
    cache_key = _cache_path("funding", f"{address}_{start_time_ms}")
    cached = _load_cache(cache_key)
    if cached is not None:
        return cached

    data = _post({
        "type": "userFunding",
        "user": address,
        "startTime": start_time_ms,
    })
    if data is None:
        return []
    if not isinstance(data, list):
        logger.warning("Unexpected funding shape: %s", type(data))
        return []
    _save_cache(cache_key, data)
    return data


def get_candles(coin: str, interval: str, start_time_ms: int, end_time_ms: int) -> list:
    """
    Return OHLCV candles.
    Each entry: {t (open_time ms), T (close_time ms), s (symbol), i (interval),
                 o, c, h, l (prices), v (volume), n (trade count)}
    """
    cache_key = _cache_path("candles", f"{coin}_{interval}_{start_time_ms}_{end_time_ms}")
    cached = _load_cache(cache_key)
    if cached is not None:
        return cached

    data = _post({
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": start_time_ms,
            "endTime": end_time_ms,
        },
    })
    if data is None:
        return []
    if not isinstance(data, list):
        logger.warning("Unexpected candles shape for %s: %s", coin, type(data))
        return []
    _save_cache(cache_key, data)
    return data


def get_portfolio(address: str) -> dict:
    """
    Return portfolio snapshots including accountValueHistory.
    Shape: {accountValueHistory: [[timestamp_ms, equity_usd], ...], ...}
    Used to approximate account-level effective leverage over time.
    """
    address = address.lower()
    cache_key = _cache_path("portfolio", address)
    cached = _load_cache(cache_key)
    if cached is not None:
        return cached

    data = _post({"type": "portfolio", "user": address})
    if data is None:
        return {}
    _save_cache(cache_key, data)
    return data


def get_recent_trades(coin: str) -> list:
    """
    Return the most recent trades for a coin.
    Each entry: {coin, side, px, sz, time, hash, tid, users: [maker, taker]}
    Returns up to 10 entries (API limit).
    """
    cache_key = _cache_path("recent_trades", coin.replace(":", "_"))
    cached = _load_cache(cache_key)
    if cached is not None:
        return cached

    data = _post({"type": "recentTrades", "coin": coin})
    if not isinstance(data, list):
        return []
    _save_cache(cache_key, data)
    return data


def score_wallet_fills(fills: list, non_btc_eth_assets: set, target_assets: set) -> dict:
    """
    Score a wallet's fills for suitability as an analysis subject.
    Returns a dict with score fields; score=0 means discard.
    """
    NULL_ADDR = "0x0000000000000000000000000000000000000000"

    target_fills = [f for f in fills if f.get("coin") in target_assets]
    non_btc_eth_fills = [f for f in fills if f.get("coin") in non_btc_eth_assets]
    coins_traded = {f.get("coin") for f in target_fills}
    non_btc_eth_coins = {f.get("coin") for f in non_btc_eth_fills}

    # Bot/MM filter: avg gap between consecutive fills < 5 min → discard
    times = sorted(f["time"] for f in fills)
    if len(times) > 1:
        gaps_min = [(times[i + 1] - times[i]) / 60000 for i in range(len(times) - 1)]
        avg_gap_min = sum(gaps_min) / len(gaps_min)
    else:
        avg_gap_min = 9999.0

    is_bot = avg_gap_min < 5.0
    enough_trades = 15 <= len(target_fills) <= 2000
    has_non_btc_eth = len(non_btc_eth_coins) >= 1

    if is_bot or not enough_trades or not has_non_btc_eth:
        score = 0
    else:
        # Higher score = more diverse + right trade count
        diversity = len(non_btc_eth_coins)
        count_score = 1 if len(target_fills) <= 500 else 0  # prefer manageable history
        score = diversity * 2 + count_score

    return {
        "score": score,
        "avg_gap_min": round(avg_gap_min, 1),
        "total_fills": len(fills),
        "target_fills": len(target_fills),
        "non_btc_eth_coins": sorted(non_btc_eth_coins),
        "all_coins": sorted(coins_traded),
        "is_bot": is_bot,
    }


def pick_wallets_asset_first(
    non_btc_eth_assets: list,
    target_assets: set,
    active_count: int = 3,
    low_activity_count: int = 2,
) -> tuple[list, list]:
    """
    Seed wallet candidates from recentTrades on each non-BTC/ETH target asset,
    fetch their fills, score them, and return (active, low_activity) lists.

    Active: highest-scoring (diverse assets, human hold times, enough trades).
    Low-activity: wallets that qualify but have fewer target fills (15–80 range).
    """
    NULL_ADDR = "0x0000000000000000000000000000000000000000"

    # Step 1: collect unique candidate addresses from recentTrades
    candidates: set[str] = set()
    for coin in non_btc_eth_assets:
        for trade in get_recent_trades(coin):
            for addr in trade.get("users", []):
                if addr and addr.lower() != NULL_ADDR:
                    candidates.add(addr.lower())

    logger.info("Asset-first seed: %d unique candidate wallets", len(candidates))

    # Step 2: fetch fills + score each candidate
    scored: list[dict] = []
    for addr in candidates:
        fills = get_fills(addr)
        info = score_wallet_fills(fills, set(non_btc_eth_assets), target_assets)
        info["address"] = addr
        scored.append(info)
        logger.debug(
            "  %s  score=%d  gap=%.1fmin  assets=%s",
            addr[:16], info["score"], info["avg_gap_min"], info["non_btc_eth_coins"],
        )

    # Step 3: split into active / low-activity
    qualifiers = [s for s in scored if s["score"] > 0]
    qualifiers.sort(key=lambda s: (s["score"], s["target_fills"]), reverse=True)

    # Low-activity = qualifies but fewer target fills (15–80) — enough to show patterns
    low_activity_pool = [s for s in qualifiers if s["target_fills"] <= 80]
    active_pool = [s for s in qualifiers if s["target_fills"] > 80]

    active = [s["address"] for s in active_pool[:active_count]]
    low_activity = [s["address"] for s in low_activity_pool[:low_activity_count]]

    # Fallback: if not enough active wallets, pull from full qualifier list
    if len(active) < active_count:
        extras = [s["address"] for s in qualifiers if s["address"] not in active and s["address"] not in low_activity]
        active += extras[: active_count - len(active)]

    logger.info(
        "Selected %d active wallets, %d low-activity wallets",
        len(active), len(low_activity),
    )
    for s in qualifiers[:10]:
        logger.info(
            "  wallet %s  score=%d  fills=%d  assets=%s  gap=%.1fmin  bot=%s",
            s["address"][:16], s["score"], s["target_fills"],
            s["non_btc_eth_coins"], s["avg_gap_min"], s["is_bot"],
        )

    return active, low_activity


def get_leaderboard() -> list:
    """
    Fetch leaderboard from stats endpoint.
    Returns list of {ethAddress, accountValue, windowPerformances, displayName}.
    Falls back to hardcoded active addresses if fetch fails.
    """
    cache_key = _cache_path("leaderboard", "all")
    cached = _load_cache(cache_key)
    if cached is not None:
        logger.debug("Cache hit: leaderboard")
        return cached

    # The main /info endpoint doesn't support leaderboard; use stats-data subdomain
    try:
        time.sleep(0.3)
        resp = requests.get(
            f"{STATS_BASE_URL}/Mainnet/leaderboard",
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("leaderboardRows", [])
        if rows:
            _save_cache(cache_key, rows)
            return rows
    except Exception as e:
        logger.warning("Leaderboard fetch failed: %s", e)

    # Fallback: hardcoded well-known active addresses (verified 2025-05-21)
    logger.warning("Using hardcoded fallback wallet addresses")
    fallback = [
        {
            "ethAddress": "0x7fdafde5cfb5465924316eced2d3715494c517d1",
            "accountValue": "29000000",
            "windowPerformances": [["allTime", {"vlm": "11900000000", "pnl": "164800000"}]],
        },
        {
            "ethAddress": "0xecb63caa47c7c4e77f60f1ce858cf28dc2b82b00",
            "accountValue": "73000000",
            "windowPerformances": [["allTime", {"vlm": "255700000000", "pnl": "207600000"}]],
        },
        {
            "ethAddress": "0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36",
            "accountValue": "21000000",
            "windowPerformances": [["allTime", {"vlm": "3900000000", "pnl": "125600000"}]],
        },
        {
            "ethAddress": "0x87f9cd15f5050a9283b8896300f7c8cf69ece2cf",
            "accountValue": "83000000",
            "windowPerformances": [["allTime", {"vlm": "495000000000", "pnl": "56500000"}]],
        },
        {
            "ethAddress": "0x5b5d51203a0f9079f8aeb098a6523a13f298c060",
            "accountValue": "19000000",
            "windowPerformances": [["allTime", {"vlm": "4200000000", "pnl": "179900000"}]],
        },
    ]
    return fallback


def pick_wallets_from_leaderboard(
    rows: list, active_count: int = 3, dormant_count: int = 2
) -> tuple[list, list]:
    """
    From leaderboard rows, pick active (high volume) and low-activity (small but non-zero volume).
    Zero-volume wallets are excluded — they have no fills to analyze.
    Returns (active_addresses, low_activity_addresses).
    """

    def alltime_vol(row: dict) -> float:
        for window, perf in row.get("windowPerformances", []):
            if window == "allTime":
                return float(perf.get("vlm", 0))
        return 0.0

    # Only consider wallets with non-zero all-time volume
    active_rows = [r for r in rows if alltime_vol(r) > 0]
    sorted_rows = sorted(active_rows, key=alltime_vol, reverse=True)

    active = [r["ethAddress"] for r in sorted_rows[:active_count]]

    # Low-activity: bottom 10% by volume (non-zero, not already selected as active)
    cutoff = max(active_count, len(sorted_rows) - len(sorted_rows) // 10)
    low_activity_candidates = [
        r for r in sorted_rows[cutoff:]
        if r["ethAddress"] not in active
    ]
    low_activity = [r["ethAddress"] for r in low_activity_candidates[:dormant_count]]

    # Fallback: take from just above the top-active slice if bottom is thin
    if len(low_activity) < dormant_count:
        mid = [
            r for r in sorted_rows[active_count:active_count + 200]
            if r["ethAddress"] not in active and r["ethAddress"] not in low_activity
        ]
        low_activity += [r["ethAddress"] for r in mid[: dormant_count - len(low_activity)]]

    return active, low_activity
