# ─────────────────────────────────────────────────────────────────────────────
# evaluator.py
#
# DESIGN:
#   - No hardcoded fallbacks anywhere. If we can't get a real value, we raise
#     and the order is skipped. Never guess.
#   - All tokens supported — no whitelist. Any token the order uses gets quoted.
#   - Concurrent — quote_all_fees() fires all 3 fee tiers in parallel threads.
#   - USD pricing uses on-chain QuoterV2 only, never CoinGecko or hardcodes.
#     surplus token → WETH → USDC gives a 2-hop price for any token.
# ─────────────────────────────────────────────────────────────────────────────
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from web3 import Web3

from eth_account import Account
from config import (
    QUOTER_V2, FEE_TIERS, MIN_PROFIT_USD,
    DECIMALS, STABLES, EXECUTOR_KEY,
    WETH, USDC, WBTC
)

def account_address() -> str:
    """Return executor wallet address for gas estimation calls."""
    return Account.from_key(EXECUTOR_KEY).address

# ── ABIs ──────────────────────────────────────────────────────────────────────
QUOTER_ABI = [{
    "inputs": [{"components": [
        {"name": "tokenIn",           "type": "address"},
        {"name": "tokenOut",          "type": "address"},
        {"name": "amountIn",          "type": "uint256"},
        {"name": "fee",               "type": "uint24"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"},
    ], "name": "params", "type": "tuple"}],
    "name": "quoteExactInputSingle",
    "outputs": [
        {"name": "amountOut",              "type": "uint256"},
        {"name": "sqrtPriceX96After",      "type": "uint160"},
        {"name": "initializedTicksCrossed","type": "uint32"},
        {"name": "gasEstimate",            "type": "uint256"},
    ],
    "stateMutability": "nonpayable",
    "type": "function",
}]

ERC20_ABI = [
    {"inputs": [], "name": "decimals",
     "outputs": [{"type": "uint8"}],
     "stateMutability": "view", "type": "function"},
]

# ── Decimals cache lock (shared across threads) ───────────────────────────────
_dec_lock = threading.Lock()


def get_decimals(w3: Web3, token: str) -> int:
    """
    Fetch token decimals. Uses cache, falls back to on-chain call.
    Raises if on-chain call fails — never returns a guess.
    """
    token = token.lower()
    with _dec_lock:
        if token in DECIMALS:
            return DECIMALS[token]

    c = w3.eth.contract(
        address=Web3.to_checksum_address(token),
        abi=ERC20_ABI
    )
    dec = c.functions.decimals().call()   # raises on failure — intentional

    with _dec_lock:
        DECIMALS[token] = dec
    return dec


def _quote_single(w3: Web3, token_in: str, token_out: str,
                  amount_in: int, fee: int) -> int:
    """
    Call QuoterV2.quoteExactInputSingle via eth_call (simulation, no gas cost).
    Returns amountOut or 0 if the pool does not exist / reverts.
    """
    quoter = w3.eth.contract(
        address=Web3.to_checksum_address(QUOTER_V2),
        abi=QUOTER_ABI
    )
    try:
        result = quoter.functions.quoteExactInputSingle({
            "tokenIn":           Web3.to_checksum_address(token_in),
            "tokenOut":          Web3.to_checksum_address(token_out),
            "amountIn":          amount_in,
            "fee":               fee,
            "sqrtPriceLimitX96": 0,
        }).call()
        return result[0]  # amountOut
    except Exception:
        return 0  # pool doesn't exist or no liquidity at this fee tier


def quote_best(w3: Web3, token_in: str, token_out: str,
               amount_in: int) -> tuple[int, int]:
    """
    Quote across all fee tiers concurrently.
    Returns (best_amount_out, best_fee_tier).
    Returns (0, 0) if no pool found.
    """
    best_out = 0
    best_fee = 0

    with ThreadPoolExecutor(max_workers=len(FEE_TIERS)) as ex:
        futures = {
            ex.submit(_quote_single, w3, token_in, token_out, amount_in, fee): fee
            for fee in FEE_TIERS
        }
        for fut in as_completed(futures):
            fee = futures[fut]
            try:
                out = fut.result()
                if out > best_out:
                    best_out = out
                    best_fee = fee
            except Exception:
                pass

    return best_out, best_fee


def token_to_usd(w3: Web3, token: str, amount_raw: int, dec: int) -> float:
    """
    Convert any token amount to USD using on-chain quotes only.
    Route: token → USDC (direct) OR token → WETH → USDC (2-hop).
    Raises if neither route can produce a quote.
    """
    token = token.lower()
    amount_h = amount_raw / (10 ** dec)

    # Stablecoins are $1
    if token in STABLES:
        return amount_h

    # WETH — price via WETH/USDC pool
    if token == WETH:
        usdc_out, _ = quote_best(w3, WETH, USDC, amount_raw)
        if usdc_out == 0:
            raise ValueError(f"Cannot price WETH→USDC")
        return usdc_out / 1e6

    # Try direct token → USDC first
    usdc_out, _ = quote_best(w3, token, USDC, amount_raw)
    if usdc_out > 0:
        return usdc_out / 1e6

    # Fallback: token → WETH → USDC (2-hop)
    weth_out, _ = quote_best(w3, token, WETH, amount_raw)
    if weth_out == 0:
        raise ValueError(f"Cannot price token {token[:10]} — no V3 pool found")

    usdc_out, _ = quote_best(w3, WETH, USDC, weth_out)
    if usdc_out == 0:
        raise ValueError(f"Cannot price WETH→USDC for token {token[:10]}")

    return usdc_out / 1e6


def get_gas_price_usd(w3: Web3, gas_units: int) -> float:
    """
    Estimate gas cost in USD for a given number of gas units.
    Uses EIP-1559 baseFee from latest block (not legacy gasPrice).
    ETH price sourced from QuoterV2 on-chain — no hardcodes.
    Raises if either call fails.
    """
    latest   = w3.eth.get_block("latest")
    base_fee = latest["baseFeePerGas"]           # wei — actual current cost
    tip      = w3.eth.max_priority_fee            # wei — node suggested tip
    gas_eth  = (gas_units * (base_fee + tip)) / 1e18

    # ETH price via QuoterV2 — 1 WETH → USDC
    usdc_out, _ = quote_best(w3, WETH, USDC, 10**18)
    if usdc_out == 0:
        raise ValueError("Cannot get ETH price — WETH/USDC pool returned 0")

    eth_price = usdc_out / 1e6
    return gas_eth * eth_price


def current_required_out(order: dict) -> tuple[int, str]:
    """
    Calculate current decayed minimum output and token address.
    Handles Dutch V1 (top-level decay times) and V2 (cosignerData).
    Filters out fee outputs.
    Returns (required_out_raw, token_out_address).
    Raises if order structure is invalid.
    """
    now = int(time.time())

    cosigner_data    = order.get("cosignerData", {})
    decay_start      = int(cosigner_data.get("decayStartTime")
                           or order.get("decayStartTime") or 0)
    decay_end        = int(cosigner_data.get("decayEndTime")
                           or order.get("decayEndTime") or 0)
    output_overrides = cosigner_data.get("outputOverrides", [])

    # Filter fee outputs
    outputs = [o for o in order.get("outputs", [])
               if not o.get("isFeeOutput", False)]
    if not outputs:
        raise ValueError("Order has no non-fee outputs")

    # Pick primary output (largest endAmount)
    primary = max(outputs, key=lambda x: int(x.get("endAmount", 0)))
    token_out    = primary.get("token", "").lower()
    start_amount = int(primary.get("startAmount", 0))
    end_amount   = int(primary.get("endAmount", 0))

    if not token_out or start_amount == 0:
        raise ValueError("Invalid output token or zero startAmount")

    # Apply cosigner outputOverride if present
    idx = outputs.index(primary)
    if idx < len(output_overrides) and output_overrides[idx] not in ("0", "", None):
        start_amount = int(output_overrides[idx])

    # Decay calculation
    if decay_end == 0 or now >= decay_end:
        return end_amount, token_out
    if now <= decay_start:
        return start_amount, token_out

    elapsed  = now - decay_start
    duration = decay_end - decay_start
    decay    = (start_amount - end_amount) * elapsed // duration
    return start_amount - decay, token_out


def evaluate(w3: Web3, order: dict) -> dict | None:
    """
    Evaluate profitability of a single order.
    Returns fill-params dict if profitable, None otherwise.
    Never raises — all errors are caught and return None.
    """
    try:
        # ── Deadline ─────────────────────────────────────────────────────────
        deadline = int(order.get("deadline") or 0)
        if deadline and time.time() >= deadline:
            return None

        # ── Exclusivity ───────────────────────────────────────────────────────
        cosigner_data    = order.get("cosignerData", {})
        exclusive_filler = (cosigner_data.get("exclusiveFiller") or "").lower()
        decay_start      = int(cosigner_data.get("decayStartTime")
                               or order.get("decayStartTime") or 0)
        null_addr        = "0x0000000000000000000000000000000000000000"
        if exclusive_filler not in ("", null_addr) and time.time() < decay_start:
            return None  # exclusive window, not our order yet

        # ── Token in ──────────────────────────────────────────────────────────
        token_in = order.get("input", {}).get("token", "").lower()
        if not token_in:
            return None

        # ── Skip native ETH output — not ERC20, reactor can't pull it ────────
        ETH_ADDR = "0x0000000000000000000000000000000000000000"

        # ── Required output ───────────────────────────────────────────────────
        required_out, token_out = current_required_out(order)
        if token_out == ETH_ADDR:
            return None
        if required_out == 0:
            return None

        # ── Amount in (with cosigner inputOverride) ───────────────────────────
        input_override = cosigner_data.get("inputOverride", "0") or "0"
        amount_in = (int(input_override) if input_override != "0"
                     else int(order.get("input", {}).get("startAmount", 0)))
        if amount_in == 0:
            return None

        # ── V3 quote — concurrent across all fee tiers ────────────────────────
        v3_quote, pool_fee = quote_best(w3, token_in, token_out, amount_in)
        if v3_quote == 0 or pool_fee == 0:
            return None  # no liquidity

        if v3_quote <= required_out:
            return None  # V3 can't beat required output

        # ── Surplus ───────────────────────────────────────────────────────────
        surplus_raw = v3_quote - required_out
        dec_out     = get_decimals(w3, token_out)
        surplus_usd = token_to_usd(w3, token_out, surplus_raw, dec_out)

        # ── Gas cost — simulate exact gas via eth_estimateGas ────────────────
        from config import FILLERBOT_ADDR
        if not FILLERBOT_ADDR:
            raise ValueError("FILLERBOT_ADDRESS not set")

        FILLERBOT_ABI_MIN = [{"inputs":[
            {"name":"encodedOrder","type":"bytes"},
            {"name":"sig","type":"bytes"},
            {"name":"tokenIn","type":"address"},
            {"name":"tokenOut","type":"address"},
            {"name":"amountIn","type":"uint256"},
            {"name":"requiredOut","type":"uint256"},
            {"name":"poolFee","type":"uint24"}],
            "name":"fillOrder","outputs":[],
            "stateMutability":"nonpayable","type":"function"}]

        contract  = w3.eth.contract(
            address=Web3.to_checksum_address(FILLERBOT_ADDR),
            abi=FILLERBOT_ABI_MIN
        )
        gas_units = contract.functions.fillOrder(
            bytes.fromhex(order.get("encodedOrder","").replace("0x","")),
            bytes.fromhex(order.get("signature","").replace("0x","")),
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            amount_in,
            required_out,
            pool_fee,
        ).estimate_gas({"from": account_address()})

        gas_cost_usd = get_gas_price_usd(w3, gas_units)

        # ── Profit check ──────────────────────────────────────────────────────
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

    except Exception:
        return None  # skip this order, log happens in monitor