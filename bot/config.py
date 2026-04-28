# ─────────────────────────────────────────────────────────────────────────────
# config.py — all constants in one place
# Edit this file before running the bot
# ─────────────────────────────────────────────────────────────────────────────
import os

# ── Node ─────────────────────────────────────────────────────────────────────
# Your local reth node IPC path — no rate limits, no API key needed
IPC_PATH = os.getenv("IPC_PATH", "/bsc/reth/reth.ipc")

# ── Contracts (Ethereum Mainnet) ──────────────────────────────────────────────
DUTCH_REACTOR   = "0x00000011F84B9aa48e5f8aA8B9897600006289Be"  # UniswapX Dutch V2
SWAP_ROUTER     = "0xE592427A0AEce92De3Edee1F18E0157C05861564"  # Uniswap V3 SwapRouter
QUOTER_V2       = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"  # Uniswap V3 QuoterV2
FILLERBOT_ADDR  = os.getenv("FILLERBOT_ADDRESS", "")             # Set after deploy

# ── Bot wallet ────────────────────────────────────────────────────────────────
# Executor wallet — needs ETH for gas only, no token balance required
EXECUTOR_KEY    = os.getenv("EXECUTOR_PRIVATE_KEY", "")          # Hot wallet private key

# ── UniswapX API ──────────────────────────────────────────────────────────────
UNISWAPX_API    = "https://api.uniswap.org/v2/orders"
POLL_INTERVAL   = 0.1      # seconds between API polls (Uniswap recommends max 6 rps)
ORDER_LIMIT     = 50       # orders per API call

# ── Profitability ─────────────────────────────────────────────────────────────
# Minimum net profit in USD to execute a fill
# Set low during testing, raise once live
MIN_PROFIT_USD  = 1.50   # $1.50 minimum — must cover gas

# Gas estimate for a fill tx (conservative)
GAS_ESTIMATE    = 250_000  # units
GAS_PRICE_GWEI  = 5        # gwei — bot reads live value, this is fallback

# ── Uniswap V3 fee tiers to try ───────────────────────────────────────────────
# Bot tries all three and picks the one giving the best quote
FEE_TIERS = [500, 3000, 10000]  # 0.05%, 0.30%, 1.00%

# ── Concurrent evaluation ─────────────────────────────────────────────────────
# Max threads for concurrent order evaluation
MAX_EVAL_WORKERS = 10

# ── Token decimals cache ──────────────────────────────────────────────────────
DECIMALS = {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": 18,  # WETH
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": 8,   # WBTC
    "0xa0b86991c6231488ccd050ce3eba90c95bdc17b4": 6,   # USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,   # USDT
    "0x6b175474e89094c44da98b954eedeac495271d0f": 18,  # DAI
    "0x514910771af9ca656af840dff83e8264ecf986ca": 18,  # LINK
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": 18,  # AAVE
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": 18,  # UNI
    "0x5a98fcbea516cf06857215779fd812ca3bef1b32": 18,  # LDO
    "0xd533a949740bb3306d119cc777fa900ba034cd52": 18,  # CRV
    "0xc18360217d8f7ab5e7c516566761ea12ce7f9d72": 18,  # ENS
    "0x912ce59144191c1204e64559fe8253a0e49e6548": 18,  # ARB
    "0xae78736cd615f374d3085123a210448e74fc6393": 18,  # rETH
    "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": 18,  # stETH
}