# ─────────────────────────────────────────────────────────────────────────────
# config.py
# ─────────────────────────────────────────────────────────────────────────────
import os

# ── Node ─────────────────────────────────────────────────────────────────────
IPC_PATH = os.getenv("IPC_PATH", "/bsc/reth/reth.ipc")

# ── Contracts (Ethereum Mainnet) ──────────────────────────────────────────────
DUTCH_REACTOR  = "0x00000011F84B9aa48e5f8aA8B9897600006289Be"
SWAP_ROUTER    = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
QUOTER_V2      = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
FILLERBOT_ADDR = os.getenv("FILLERBOT_ADDRESS", "")

# ── Bot wallet ────────────────────────────────────────────────────────────────
EXECUTOR_KEY   = os.getenv("EXECUTOR_PRIVATE_KEY", "")

# ── UniswapX API ──────────────────────────────────────────────────────────────
UNISWAPX_API   = "https://api.uniswap.org/v2/orders"
POLL_INTERVAL  = 0.5    # seconds — Uniswap recommends max 6 rps
ORDER_LIMIT    = 50

# ── Profitability ─────────────────────────────────────────────────────────────
MIN_PROFIT_USD = 1.50   # minimum net profit in USD after gas

# ── Uniswap V3 fee tiers ──────────────────────────────────────────────────────
FEE_TIERS = [500, 3000, 10000]  # 0.05%, 0.30%, 1.00%

# ── WETH / stablecoin addresses for USD pricing ───────────────────────────────
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
WBTC = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
USDC = "0xa0b86991c6231488ccd050ce3eba90c95bdc17b4"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
DAI  = "0x6b175474e89094c44da98b954eedeac495271d0f"

STABLES = {USDC, USDT, DAI,
           "0x83f20f44975d03b1b09e64809b757c47f942beea",   # sDAI
           "0x4fabb145d64652a948d72533023f6e7a623c7c53",}  # BUSD

# ── Decimals cache — grows automatically at runtime ───────────────────────────
# Prepopulate only the most common tokens to save RPC calls at startup.
# All other tokens are fetched from chain and added here automatically.
DECIMALS: dict[str, int] = {
    WETH: 18,
    WBTC: 8,
    USDC: 6,
    USDT: 6,
    DAI:  18,
}