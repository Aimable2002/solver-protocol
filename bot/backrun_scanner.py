# ─────────────────────────────────────────────────────────────────────────────
# backrun_scanner.py
# Watches the pending mempool for swaps and simulates backrun profitability.
# NO execution — observe only.
# ─────────────────────────────────────────────────────────────────────────────
import json
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

# ── Config ────────────────────────────────────────────────────────────────────
IPC_PATH     = "/bsc/reth/reth.ipc"
MIN_PROFIT   = 0          # take everything not in loss
FEE_TIERS    = [100, 500, 3000, 10000]

# ── Token decimals / symbols (extended) ──────────────────────────────────────
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6231488ccd050ce3eba90c95bdc17b4"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
DAI  = "0x6b175474e89094c44da98b954eedeac495271d0f"
WBTC = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"

DECIMALS: dict[str, int] = {
    WETH: 18, WBTC: 8, USDC: 6, USDT: 6, DAI: 18,
}
SYMBOLS: dict[str, str] = {
    WETH: "WETH", WBTC: "WBTC", USDC: "USDC", USDT: "USDT", DAI: "DAI",
}

# ── Quoter V2 ─────────────────────────────────────────────────────────────────
QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
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
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol",   "outputs": [{"type": "string"}],
     "stateMutability": "view", "type": "function"},
]

# ── All known DEX router / aggregator addresses ───────────────────────────────
ROUTERS = {
    # Uniswap
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uni-V2",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "Uni-V3",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uni-UniversalRouter-old",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "Uni-UniversalRouter",
    "0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b": "Uni-UniversalRouter2",
    # Sushiswap
    "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f": "Sushi-V2",
    "0x1b02da8cb0d097eb8d57a175b88c7d8b47997506": "Sushi-V2-b",
    # Curve
    "0x99a58482bd75cbab83b27ec03ca68ff489b5788f": "Curve-Router",
    "0x8301ae4fc9c624d1d396cbdaa1ed877821d7c511": "Curve-CRVETH",
    "0xd51a44d3fae010294c616388b506acda1bfaae46": "Curve-Tri",
    # Balancer
    "0xba12222222228d8ba445958a75a0704d566bf2c8": "Balancer-V2",
    # 1inch
    "0x1111111254fb6c44bac0bed2854e76f90643097d": "1inch-V4",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch-V5",
    "0x111111125421ca6dc452d289314280a0f8842a65": "1inch-V6",
    # Paraswap
    "0xdef171fe48cf0115b1d80b88dc8eab59176fee57": "Paraswap",
    "0x216b4b4ba9f3e719726886d34a177484278bfcae": "Paraswap-router2",
    # 0x / Matcha
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x-ExchangeProxy",
    # Cowswap settlement
    "0x9008d19f58aabd9ed0d60971565aa8510560ab41": "CoW-Settlement",
    # Kyberswap
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5": "Kyber-Meta",
    # Maverick
    "0xeba3977f2a6abb236eb9d0bb58dde31bc456d24e": "Maverick",
    # Pancakeswap on ETH
    "0x13f4ea83d0bd40e75c8222255bc855a974568dd4": "PancakeV3",
    # DODO
    "0xa356867fdcea8e71aeaf87805808803806231fdc": "DODO-Proxy",
    # Odos
    "0xcf5540fffcdc3d510b18bfca6d2b9987b0772559": "Odos-V2",
}
ROUTER_SET = set(ROUTERS.keys())

# ── Swap selectors to decode ──────────────────────────────────────────────────
# Maps 4-byte selector -> (name, (tokenIn_idx, tokenOut_idx, amountIn_idx))
# idx refers to ABI-decoded tuple position; None = must parse differently
SWAP_SELECTORS = {
    # Uniswap V2
    bytes.fromhex("38ed1739"): ("swapExactTokensForTokens", "v2"),
    bytes.fromhex("8803dbee"): ("swapTokensForExactTokens",  "v2"),
    bytes.fromhex("7ff36ab5"): ("swapExactETHForTokens",     "v2_eth_in"),
    bytes.fromhex("18cbafe5"): ("swapExactTokensForETH",     "v2"),
    # Uniswap V3
    bytes.fromhex("414bf389"): ("exactInputSingle",          "v3_single"),
    bytes.fromhex("c04b8d59"): ("exactInput",                "v3_multi"),
    bytes.fromhex("db3e2198"): ("exactOutputSingle",         "v3_out_single"),
    # Universal Router
    bytes.fromhex("3593564c"): ("execute",                   "universal"),
}

# ── Stats ─────────────────────────────────────────────────────────────────────
stats = {
    "seen": 0,
    "decoded": 0,
    "quoted": 0,
    "profitable": 0,
    "errors": 0,
}
stats_lock = threading.Lock()

_dec_lock = threading.Lock()

def log(msg: str):
    print(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {msg}", flush=True)


# ── Token helpers ─────────────────────────────────────────────────────────────
def get_decimals(w3: Web3, token: str) -> int:
    token = token.lower()
    with _dec_lock:
        if token in DECIMALS:
            return DECIMALS[token]
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        dec = c.functions.decimals().call()
        with _dec_lock:
            DECIMALS[token] = dec
        return dec
    except Exception:
        return 18

def get_symbol(w3: Web3, token: str) -> str:
    token = token.lower()
    with _dec_lock:
        if token in SYMBOLS:
            return SYMBOLS[token]
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        sym = c.functions.symbol().call()
        with _dec_lock:
            SYMBOLS[token] = sym
        return sym
    except Exception:
        short = token[:8]
        with _dec_lock:
            SYMBOLS[token] = short
        return short


# ── V3 Quoter ─────────────────────────────────────────────────────────────────
def quote_single(w3: Web3, token_in: str, token_out: str,
                 amount_in: int, fee: int) -> int:
    try:
        quoter = w3.eth.contract(
            address=Web3.to_checksum_address(QUOTER_V2), abi=QUOTER_ABI)
        result = quoter.functions.quoteExactInputSingle({
            "tokenIn":           Web3.to_checksum_address(token_in),
            "tokenOut":          Web3.to_checksum_address(token_out),
            "amountIn":          amount_in,
            "fee":               fee,
            "sqrtPriceLimitX96": 0,
        }).call()
        return result[0]
    except Exception:
        return 0


def quote_best(w3: Web3, token_in: str, token_out: str,
               amount_in: int) -> tuple[int, int]:
    best_out, best_fee = 0, 0
    with ThreadPoolExecutor(max_workers=len(FEE_TIERS)) as ex:
        futures = {
            ex.submit(quote_single, w3, token_in, token_out, amount_in, fee): fee
            for fee in FEE_TIERS
        }
        for fut in as_completed(futures):
            fee = futures[fut]
            out = fut.result()
            if out > best_out:
                best_out, best_fee = out, fee
    return best_out, best_fee


# ── Gas cost estimate in output token terms ───────────────────────────────────
def estimate_gas_cost_raw(w3: Web3, token_out: str, dec_out: int) -> int:
    """
    Rough gas cost of a backrun tx converted to token_out units via WETH price.
    Returns 0 if pricing fails — we still log the opportunity.
    """
    try:
        latest   = w3.eth.get_block("latest")
        base_fee = latest["baseFeePerGas"]
        gas_used = 300_000  # typical backrun tx
        eth_cost = base_fee * gas_used  # in wei

        if token_out.lower() in (USDC, USDT, DAI):
            # price ETH via WETH->USDC pool
            eth_in_usdc, _ = quote_best(w3, WETH, USDC, 10**18)
            if eth_in_usdc == 0:
                return 0
            # eth_cost in wei, scale to token_out
            gas_in_token = eth_cost * eth_in_usdc // 10**18
            return gas_in_token

        if token_out.lower() == WETH.lower():
            return eth_cost

        # Generic: price via WETH
        weth_per_token, _ = quote_best(w3, WETH, token_out, 10**18)
        if weth_per_token == 0:
            return 0
        gas_in_token = eth_cost * weth_per_token // 10**18
        return gas_in_token
    except Exception:
        return 0


# ── Tx decoder ────────────────────────────────────────────────────────────────
def decode_swap(w3: Web3, tx: dict) -> tuple[str, str, int] | None:
    """
    Attempt to decode a swap tx.
    Returns (token_in, token_out, amount_in) or None.
    """
    data = tx.get("input", "") or ""
    if len(data) < 10:
        return None

    selector = bytes.fromhex(data[2:10])
    if selector not in SWAP_SELECTORS:
        return None

    _, flavor = SWAP_SELECTORS[selector]
    raw = bytes.fromhex(data[10:])
    ETH = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
    NULL = "0x0000000000000000000000000000000000000000"

    try:
        if flavor == "v2":
            # (amountIn, amountOutMin, path[], to, deadline)
            decoded = w3.codec.decode(
                ["uint256", "uint256", "address[]", "address", "uint256"], raw)
            path = decoded[2]
            if len(path) < 2:
                return None
            return path[0].lower(), path[-1].lower(), decoded[0]

        elif flavor == "v2_eth_in":
            # (amountOutMin, path[], to, deadline) — value = amountIn
            decoded = w3.codec.decode(
                ["uint256", "address[]", "address", "uint256"], raw)
            path = decoded[1]
            if len(path) < 2:
                return None
            return WETH, path[-1].lower(), tx.get("value", 0)

        elif flavor == "v3_single":
            # exactInputSingle((tokenIn,tokenOut,fee,recipient,deadline,amountIn,...))
            decoded = w3.codec.decode(
                ["(address,address,uint24,address,uint256,uint256,uint256,uint160)"], raw)
            p = decoded[0]
            return p[0].lower(), p[1].lower(), p[5]

        elif flavor == "v3_out_single":
            # exactOutputSingle((tokenIn,tokenOut,fee,recipient,deadline,amountOut,amountInMax,...))
            decoded = w3.codec.decode(
                ["(address,address,uint24,address,uint256,uint256,uint256,uint160)"], raw)
            p = decoded[0]
            # amountIn unknown — use amountInMax as upper bound
            return p[0].lower(), p[1].lower(), p[6]

        elif flavor == "v3_multi":
            # exactInput((bytes path, address recipient, uint256 deadline, uint256 amountIn, ...))
            decoded = w3.codec.decode(
                ["(bytes,address,uint256,uint256,uint256)"], raw)
            path_bytes = decoded[0][0]
            # path encoding: [addr 20b][fee 3b][addr 20b]...
            if len(path_bytes) < 43:
                return None
            token_in  = "0x" + path_bytes[:20].hex()
            token_out = "0x" + path_bytes[-20:].hex()
            return token_in.lower(), token_out.lower(), decoded[0][3]

        # Universal router: too complex to fully decode — skip for now
        return None

    except Exception:
        return None


# ── Core opportunity evaluator ────────────────────────────────────────────────
def evaluate_backrun(w3: Web3, tx: dict) -> dict | None:
    """
    Given a decoded pending swap tx, simulate a same-direction backrun.
    The victim moves the price, we trade the same direction at a worse price
    but capture any remaining arb vs the quoted output.

    For a pure backrun: we swap tokenIn→tokenOut AFTER the victim.
    We quote with the SAME amount as the victim (conservative simulation).
    Net profit = v3_quote - gas_cost (in token_out units).
    Returns opportunity dict or None.
    """
    decoded = decode_swap(w3, tx)
    if decoded is None:
        return None

    token_in, token_out, amount_in = decoded
    if amount_in == 0:
        return None

    # Skip ETH outputs — our contract handles ERC20 only for now
    NULL = "0x0000000000000000000000000000000000000000"
    if token_out in (NULL, "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"):
        return None

    # Quote what we'd get on V3
    v3_out, best_fee = quote_best(w3, token_in, token_out, amount_in)
    if v3_out == 0:
        return None

    dec_in   = get_decimals(w3, token_in)
    dec_out  = get_decimals(w3, token_out)
    gas_cost = estimate_gas_cost_raw(w3, token_out, dec_out)

    net_raw  = v3_out - gas_cost

    return {
        "tx_hash":   tx["hash"].hex(),
        "router":    ROUTERS.get(tx.get("to", "").lower(), tx.get("to", "")[:10]),
        "token_in":  token_in,
        "token_out": token_out,
        "amount_in": amount_in,
        "v3_out":    v3_out,
        "gas_cost":  gas_cost,
        "net_raw":   net_raw,
        "fee":       best_fee,
        "dec_in":    dec_in,
        "dec_out":   dec_out,
    }


# ── Mempool listener ──────────────────────────────────────────────────────────
def handle_pending_tx(w3: Web3, tx_hash: str):
    try:
        tx = w3.eth.get_transaction(tx_hash)
    except Exception:
        return

    to = (tx.get("to") or "").lower()
    if to not in ROUTER_SET:
        return

    with stats_lock:
        stats["seen"] += 1

    opp = evaluate_backrun(w3, tx)

    if opp is None:
        with stats_lock:
            stats["decoded"] += 1
        return

    with stats_lock:
        stats["quoted"] += 1

    sym_in  = get_symbol(w3, opp["token_in"])
    sym_out = get_symbol(w3, opp["token_out"])

    amt_in_h  = opp["amount_in"]  / 10**opp["dec_in"]
    v3_out_h  = opp["v3_out"]     / 10**opp["dec_out"]
    gas_h     = opp["gas_cost"]   / 10**opp["dec_out"]
    net_h     = opp["net_raw"]    / 10**opp["dec_out"]

    if opp["net_raw"] > MIN_PROFIT:
        with stats_lock:
            stats["profitable"] += 1

        flag = "✓ PROFITABLE"
        print(f"\n{'─'*60}", flush=True)
        print(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {flag}", flush=True)
        print(f"  tx:        {opp['tx_hash']}", flush=True)
        print(f"  router:    {opp['router']}", flush=True)
        print(f"  pair:      {sym_in} → {sym_out}  fee={opp['fee']}", flush=True)
        print(f"  amount_in: {amt_in_h:.6f} {sym_in}", flush=True)
        print(f"  v3_out:    {v3_out_h:.6f} {sym_out}", flush=True)
        print(f"  gas_cost:  {gas_h:.6f} {sym_out}", flush=True)
        print(f"  NET:       {net_h:.6f} {sym_out}  ← keep this", flush=True)
        print(f"{'─'*60}", flush=True)
    else:
        # Log marginal / loss ops quietly
        sign = "+" if net_h >= 0 else ""
        log(f"  skip {sym_in}->{sym_out} via {opp['router']}"
            f" | net={sign}{net_h:.4f} {sym_out}"
            f" | v3={v3_out_h:.4f} gas={gas_h:.4f}")


# ── Stats printer ─────────────────────────────────────────────────────────────
def print_stats():
    while True:
        time.sleep(30)
        with stats_lock:
            s = dict(stats)
        log(f"[stats] seen={s['seen']} decoded={s['decoded']} "
            f"quoted={s['quoted']} profitable={s['profitable']} "
            f"errors={s['errors']}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60, flush=True)
    print("Backrun Scanner — observe only, no execution", flush=True)
    print(f"IPC: {IPC_PATH}", flush=True)
    print(f"Watching {len(ROUTERS)} router addresses", flush=True)
    print(f"Fee tiers: {FEE_TIERS}", flush=True)
    print("=" * 60, flush=True)

    w3 = Web3(Web3.IPCProvider(IPC_PATH))
    if not w3.is_connected():
        print(f"ERROR: cannot connect to {IPC_PATH}", flush=True)
        return

    print(f"Connected — block {w3.eth.block_number:,}", flush=True)
    print("Subscribing to pending txs...\n", flush=True)

    # Background stats thread
    threading.Thread(target=print_stats, daemon=True).start()

    # Thread pool for parallel tx processing
    executor = ThreadPoolExecutor(max_workers=32)

    # Subscribe to newPendingTransactions
    sub = w3.eth.filter("pending")

    try:
        while True:
            try:
                new_hashes = sub.get_new_entries()
                for tx_hash in new_hashes:
                    executor.submit(handle_pending_tx, w3, tx_hash)
            except Exception as e:
                with stats_lock:
                    stats["errors"] += 1
                log(f"filter error: {e}")
                # re-subscribe
                try:
                    sub = w3.eth.filter("pending")
                except Exception:
                    pass
            time.sleep(0.1)

    except KeyboardInterrupt:
        with stats_lock:
            s = dict(stats)
        print(f"\nStopped", flush=True)
        print(f"Final stats: seen={s['seen']} decoded={s['decoded']} "
              f"quoted={s['quoted']} profitable={s['profitable']}", flush=True)
        executor.shutdown(wait=False)


if __name__ == "__main__":
    main()