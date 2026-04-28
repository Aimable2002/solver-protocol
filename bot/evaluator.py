# ─────────────────────────────────────────────────────────────────────────────
# evaluator.py — profitability check before execution
#
# HOW PROFIT WORKS (no flash loan):
# ══════════════════════════════════════════════════════════════════════
# The reactor gives us tokenIn BEFORE the swap.
# We swap tokenIn → tokenOut on Uniswap V3.
# We give the user exactly `requiredOut` of tokenOut.
# We keep the surplus as profit.
#
# profit = V3_quote(tokenIn, tokenOut, amountIn) - requiredOut - gas_cost
#
# If profit > MIN_PROFIT_USD → fill it
#
# DESIGN RULES:
# ══════════════════════════════════════════════════════════════════════
# 1. No hardcoded price/gas fallbacks — if QuoterV2 or gas_price fails,
#    raise QuoteError and skip the order. Never guess.
# 2. No token whitelist — any ERC20 token in an order gets evaluated.
#    Decimals are fetched on-chain if not in the local cache.
# 3. quote_v3 and get_decimals are plain blocking calls designed to be
#    driven from a ThreadPoolExecutor in monitor.py so many orders are
#    evaluated concurrently.
# ─────────────────────────────────────────────────────────────────────────────
import threading
import time
from web3 import Web3
from config import (
    QUOTER_V2, FEE_TIERS, MIN_PROFIT_USD,
    GAS_ESTIMATE, DECIMALS
)

# Thread-safe lock for the shared DECIMALS cache
_decimals_lock = threading.Lock()


# ── Custom exceptions ─────────────────────────────────────────────────────────

class QuoteError(Exception):
    """Raised when QuoterV2 cannot return a usable quote."""

class GasPriceError(Exception):
    """Raised when the node cannot return the current gas price."""

class DecimalsError(Exception):
    """Raised when token decimals cannot be determined."""


# ── ABIs ──────────────────────────────────────────────────────────────────────

QUOTER_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "tokenIn",             "type": "address"},
                {"name": "tokenOut",            "type": "address"},
                {"name": "amountIn",            "type": "uint256"},
                {"name": "fee",                 "type": "uint24"},
                {"name": "sqrtPriceLimitX96",   "type": "uint160"},
            ],
            "name": "params",
            "type": "tuple"
        }],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"name": "amountOut",               "type": "uint256"},
            {"name": "sqrtPriceX96After",        "type": "uint160"},
            {"name": "initializedTicksCrossed",  "type": "uint32"},
            {"name": "gasEstimate",              "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

ERC20_ABI = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"type": "uint8"}],
        "stateMutability": "view",
        "type": "function"
    }
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_decimals(w3: Web3, token: str) -> int:
    """
    Return token decimals.

    Checks the local cache first (thread-safe read).
    If not cached, calls decimals() on-chain and populates the cache.

    Raises DecimalsError if the on-chain call fails — callers must catch
    this and skip the order rather than guessing.
    """
    token = token.lower()

    with _decimals_lock:
        if token in DECIMALS:
            return DECIMALS[token]

    # On-chain fetch (outside the lock — only one thread wastes a call at worst)
    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token),
            abi=ERC20_ABI
        )
        dec = contract.functions.decimals().call()
    except Exception as exc:
        raise DecimalsError(f"cannot fetch decimals for {token}: {exc}") from exc

    with _decimals_lock:
        DECIMALS[token] = dec

    return dec


def get_gas_price(w3: Web3) -> int:
    """
    Return current gas price in wei from the node.

    Raises GasPriceError if the node call fails.
    Never falls back to a hardcoded value.
    """
    try:
        return w3.eth.gas_price
    except Exception as exc:
        raise GasPriceError(f"cannot fetch gas price: {exc}") from exc


def get_eth_price_usd(w3: Web3) -> float:
    """
    Get ETH price in USD using the USDC/WETH 0.05% pool.

    Raises QuoteError if the quote fails — the caller (monitor.py) should
    decide whether to retry or abort the poll cycle.
    """
    WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
    USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

    quoter = w3.eth.contract(
        address=Web3.to_checksum_address(QUOTER_V2),
        abi=QUOTER_ABI
    )
    try:
        result = quoter.functions.quoteExactInputSingle({
            "tokenIn":           Web3.to_checksum_address(WETH),
            "tokenOut":          Web3.to_checksum_address(USDC),
            "amountIn":          10**18,
            "fee":               500,
            "sqrtPriceLimitX96": 0,
        }).call()
    except Exception as exc:
        raise QuoteError(f"ETH/USDC quote failed: {exc}") from exc

    price = result[0] / 1e6   # USDC has 6 decimals
    if price <= 0:
        raise QuoteError(f"ETH/USDC quote returned non-positive price: {price}")
    return price


def quote_v3(
    w3: Web3,
    token_in: str,
    token_out: str,
    amount_in: int,
) -> tuple[int, int]:
    """
    Get best V3 quote across all fee tiers.

    Returns (best_amount_out, best_fee_tier).

    Raises QuoteError if every fee tier fails (no pool exists or all
    calls error). Callers must catch this and skip the order.
    """
    quoter = w3.eth.contract(
        address=Web3.to_checksum_address(QUOTER_V2),
        abi=QUOTER_ABI
    )
    best_out  = 0
    best_fee  = 0
    errors    = []

    for fee in FEE_TIERS:
        try:
            result = quoter.functions.quoteExactInputSingle({
                "tokenIn":           Web3.to_checksum_address(token_in),
                "tokenOut":          Web3.to_checksum_address(token_out),
                "amountIn":          amount_in,
                "fee":               fee,
                "sqrtPriceLimitX96": 0,
            }).call()
            amount_out = result[0]
            if amount_out > best_out:
                best_out = amount_out
                best_fee = fee
        except Exception as exc:
            errors.append(f"fee={fee}: {exc}")
            continue  # this pool doesn't exist or reverted — try next tier

    if best_out == 0 or best_fee == 0:
        raise QuoteError(
            f"no V3 quote for {token_in}->{token_out} "
            f"amount={amount_in}. Errors: {errors}"
        )

    return best_out, best_fee


# ── Dutch V2 decay ────────────────────────────────────────────────────────────

def current_required_out(order: dict) -> int:
    """
    Calculate the current decayed minimum output amount for a Dutch V2 order.

    Uses cosignerData for decay times and output overrides.
    Filters out fee outputs (isFeeOutput=True).
    Returns 0 if the order has no usable primary output.
    """
    now = int(time.time())

    cosigner_data    = order.get("cosignerData", {})
    decay_start      = cosigner_data.get("decayStartTime") or order.get("decayStartTime", 0)
    decay_end        = cosigner_data.get("decayEndTime")   or order.get("decayEndTime", 0)
    output_overrides = cosigner_data.get("outputOverrides", [])

    outputs = [o for o in order.get("outputs", []) if not o.get("isFeeOutput", False)]
    if not outputs:
        return 0

    primary      = max(outputs, key=lambda x: int(x.get("endAmount", 0)))
    start_amount = int(primary.get("startAmount", 0))
    end_amount   = int(primary.get("endAmount", 0))

    # Apply cosigner override if provided
    if output_overrides:
        idx = outputs.index(primary)
        if idx < len(output_overrides) and output_overrides[idx] != "0":
            start_amount = int(output_overrides[idx])

    if now <= decay_start:
        return start_amount
    if now >= decay_end or decay_end == 0:
        return end_amount

    elapsed  = now - decay_start
    duration = decay_end - decay_start
    decay    = (start_amount - end_amount) * elapsed // duration
    return start_amount - decay


# ── Main evaluator ────────────────────────────────────────────────────────────

def evaluate(w3: Web3, order: dict, eth_price: float) -> dict | None:
    """
    Evaluate whether an order is profitable to fill.

    Designed to be called from a ThreadPoolExecutor — all blocking I/O
    (QuoterV2, gas price, on-chain decimals) happens synchronously here
    so many orders can be evaluated concurrently across threads.

    Returns a fill-params dict if profitable, None if the order should
    be skipped for any reason (unprofitable, expired, exclusive, or any
    on-chain call fails).

    Return dict fields:
        token_in      : str   — address (lowercase)
        token_out     : str   — address (lowercase)
        amount_in     : int   — raw units
        required_out  : int   — current decayed minimum output
        v3_quote      : int   — what V3 will give us
        pool_fee      : int   — best V3 fee tier
        profit_usd    : float — estimated net profit after gas
        gas_cost_usd  : float — estimated gas cost
        surplus_usd   : float — gross surplus before gas
        order_hash    : str
        encoded_order : str
        signature     : str
    """
    # ── Basic field extraction ────────────────────────────────────────────────
    token_in  = order.get("input",  {}).get("token", "").lower()
    outputs   = [o for o in order.get("outputs", []) if not o.get("isFeeOutput", False)]
    if not outputs or not token_in:
        return None

    primary   = max(outputs, key=lambda x: int(x.get("endAmount", 0)))
    token_out = primary.get("token", "").lower()

    # Skip native ETH output (zero address) — not an ERC20, cannot approve
    if token_out == "0x0000000000000000000000000000000000000000":
        return None

    # ── Deadline check ────────────────────────────────────────────────────────
    deadline = order.get("deadline", 0)
    if deadline and int(time.time()) >= deadline:
        return None

    # ── Exclusivity check ─────────────────────────────────────────────────────
    cosigner_data    = order.get("cosignerData", {})
    exclusive_filler = cosigner_data.get(
        "exclusiveFiller", "0x0000000000000000000000000000000000000000"
    )
    decay_start = cosigner_data.get("decayStartTime") or order.get("decayStartTime", 0)

    if (exclusive_filler.lower() not in ("0x0000000000000000000000000000000000000000", "")
            and int(time.time()) < decay_start):
        return None  # still in exclusive period

    # ── Amounts ───────────────────────────────────────────────────────────────
    input_override = cosigner_data.get("inputOverride", "0")
    if input_override and input_override != "0":
        amount_in = int(input_override)
    else:
        amount_in = int(order.get("input", {}).get("startAmount", 0))

    if amount_in == 0:
        return None

    required_out = current_required_out(order)
    if required_out == 0:
        return None

    # ── V3 quote — raises QuoteError if no pool or all tiers fail ─────────────
    try:
        v3_quote, pool_fee = quote_v3(w3, token_in, token_out, amount_in)
    except QuoteError:
        return None  # no liquidity on any fee tier — skip silently

    if v3_quote <= required_out:
        return None  # V3 can't beat required output — no gross surplus

    surplus_raw = v3_quote - required_out

    # ── Token decimals — raises DecimalsError if on-chain call fails ──────────
    try:
        dec_out = get_decimals(w3, token_out)
    except DecimalsError:
        return None  # can't price the surplus — skip

    surplus_h = surplus_raw / (10 ** dec_out)

    # ── Gas cost — raises GasPriceError if node unavailable ──────────────────
    try:
        gas_price_wei = get_gas_price(w3)
    except GasPriceError:
        return None  # can't determine gas cost — skip to avoid under-priced fills

    gas_cost_eth = (GAS_ESTIMATE * gas_price_wei) / 1e18
    gas_cost_usd = gas_cost_eth * eth_price

    # ── Surplus → USD conversion ──────────────────────────────────────────────
    USDC = "0xa0b86991c6231488ccd050ce3eba90c95bdc17b4"
    USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    DAI  = "0x6b175474e89094c44da98b954eedeac495271d0f"
    WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

    if token_out in (USDC, USDT, DAI):
        # Stablecoins — 1:1 with USD
        surplus_usd = surplus_h
    elif token_out == WETH:
        surplus_usd = surplus_h * eth_price
    else:
        # Unknown token — price surplus against USDC via V3
        # This works for any ERC20 with a USDC pool; raises QuoteError otherwise
        try:
            surplus_quote, _ = quote_v3(w3, token_out, USDC, int(surplus_raw))
        except QuoteError:
            return None  # can't price this token — skip
        surplus_usd = surplus_quote / 1e6

    profit_usd = surplus_usd - gas_cost_usd

    if profit_usd < MIN_PROFIT_USD:
        return None

    return {
        "token_in":      token_in,
        "token_out":     token_out,
        "amount_in":     amount_in,
        "required_out":  required_out,
        "v3_quote":      v3_quote,
        "pool_fee":      pool_fee,
        "profit_usd":    round(profit_usd, 4),
        "gas_cost_usd":  round(gas_cost_usd, 4),
        "surplus_usd":   round(surplus_usd, 4),
        "order_hash":    order.get("orderHash", ""),
        "encoded_order": order.get("encodedOrder", ""),
        "signature":     order.get("signature", ""),
    }