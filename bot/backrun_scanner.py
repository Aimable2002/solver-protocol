# backrun_scanner.py — observe only, no execution
# IPC subscription (push, not poll)
# Two-leg profit: buy → sell, net of Aave fee + gas
# Aave flashloan routing: direct if available, USDC bridge if not
import time, threading, socket, json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from web3 import Web3

# ── Config ────────────────────────────────────────────────────────────────────
IPC_PATH    = "/bsc/reth/reth.ipc"
MIN_PROFIT  = 0          # wei — only log net > 0
AAVE_FEE_BP = 9          # 0.09% = 9 bps
FEE_TIERS   = [100, 500, 3000, 10000]

# ── Token addresses ───────────────────────────────────────────────────────────
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6231488ccd050ce3eba90c95bdc17b4"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
DAI  = "0x6b175474e89094c44da98b954eedeac495271d0f"
WBTC = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"

DECIMALS: dict[str, int] = {WETH:18, WBTC:8, USDC:6, USDT:6, DAI:18}
SYMBOLS:  dict[str, str]  = {WETH:"WETH", WBTC:"WBTC", USDC:"USDC", USDT:"USDT", DAI:"DAI"}

# ── Aave V3 Ethereum — assets available for flashloan ─────────────────────────
# Source: Aave V3 Ethereum market (only tokens with flashloan enabled)
AAVE_ASSETS = {
    WETH, WBTC, USDC, USDT, DAI,
    "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0",  # wstETH
    "0xae78736cd615f374d3085123a210448e74fc6393",  # rETH
    "0xbe9895146f7af43049ca1c1ae358b0541ea49704",  # cbETH
    "0x514910771af9ca656af840dff83e8264ecf986ca",  # LINK
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9",  # AAVE
    "0x5f98805a4e8be255a32880fdec7f6728c6568ba0",  # LUSD
    "0xd533a949740bb3306d119cc777fa900ba034cd52",  # CRV
    "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2",  # MKR
    "0xc011a73ee8576fb46f5e1c5751ca3b9fe0af2a6f",  # SNX
    "0xba100000625a3754423978a60c9317c58a424e3d",  # BAL
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",  # UNI
    "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI (duplicate safety)
}
AAVE_ASSETS_LOWER = {a.lower() for a in AAVE_ASSETS}

# Bridge token priority — used when tokenIn not in Aave
# We try these in order and pick the first that has V3 liquidity with tokenIn
BRIDGE_PRIORITY = [USDC, WETH, USDT, DAI]

# ── Contracts ─────────────────────────────────────────────────────────────────
QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
QUOTER_ABI = [{"inputs":[{"components":[
    {"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},
    {"name":"amountIn","type":"uint256"},{"name":"fee","type":"uint24"},
    {"name":"sqrtPriceLimitX96","type":"uint160"}
],"name":"params","type":"tuple"}],"name":"quoteExactInputSingle",
"outputs":[{"name":"amountOut","type":"uint256"},{"name":"sqrtPriceX96After","type":"uint160"},
{"name":"initializedTicksCrossed","type":"uint32"},{"name":"gasEstimate","type":"uint256"}],
"stateMutability":"nonpayable","type":"function"}]

ERC20_ABI = [
    {"inputs":[],"name":"decimals","outputs":[{"type":"uint8"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"symbol","outputs":[{"type":"string"}],"stateMutability":"view","type":"function"},
]

# ── Routers ───────────────────────────────────────────────────────────────────
ROUTERS = {
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uni-V2",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "Uni-V3",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uni-Universal-old",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "Uni-Universal",
    "0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b": "Uni-Universal2",
    "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f": "Sushi-V2",
    "0x1b02da8cb0d097eb8d57a175b88c7d8b47997506": "Sushi-V2-b",
    "0x99a58482bd75cbab83b27ec03ca68ff489b5788f": "Curve-Router",
    "0x8301ae4fc9c624d1d396cbdaa1ed877821d7c511": "Curve-CRVETH",
    "0xd51a44d3fae010294c616388b506acda1bfaae46": "Curve-Tri",
    "0xba12222222228d8ba445958a75a0704d566bf2c8": "Balancer-V2",
    "0x1111111254fb6c44bac0bed2854e76f90643097d": "1inch-V4",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch-V5",
    "0x111111125421ca6dc452d289314280a0f8842a65": "1inch-V6",
    "0xdef171fe48cf0115b1d80b88dc8eab59176fee57": "Paraswap",
    "0x216b4b4ba9f3e719726886d34a177484278bfcae": "Paraswap-r2",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x-Proxy",
    "0x9008d19f58aabd9ed0d60971565aa8510560ab41": "CoW-Settlement",
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5": "Kyber",
    "0xeba3977f2a6abb236eb9d0bb58dde31bc456d24e": "Maverick",
    "0x13f4ea83d0bd40e75c8222255bc855a974568dd4": "PancakeV3",
    "0xa356867fdcea8e71aeaf87805808803806231fdc": "DODO",
    "0xcf5540fffcdc3d510b18bfca6d2b9987b0772559": "Odos-V2",
}
ROUTER_SET = set(ROUTERS.keys())

# ── Swap selectors ────────────────────────────────────────────────────────────
SELECTORS = {
    bytes.fromhex("38ed1739"): "v2",          # swapExactTokensForTokens
    bytes.fromhex("8803dbee"): "v2",          # swapTokensForExactTokens
    bytes.fromhex("18cbafe5"): "v2",          # swapExactTokensForETH
    bytes.fromhex("7ff36ab5"): "v2_eth_in",   # swapExactETHForTokens
    bytes.fromhex("b6f9de95"): "v2_eth_in",   # swapExactETHForTokensSupportingFee
    bytes.fromhex("414bf389"): "v3_single",   # exactInputSingle
    bytes.fromhex("db3e2198"): "v3_single",   # exactOutputSingle
    bytes.fromhex("c04b8d59"): "v3_multi",    # exactInput
    bytes.fromhex("f28c0498"): "v3_multi",    # exactOutput
    bytes.fromhex("3593564c"): "universal",   # execute(bytes,bytes[],uint256)
    bytes.fromhex("24856bc3"): "universal",   # execute(bytes,bytes[])
}

# ── State ─────────────────────────────────────────────────────────────────────
stats      = {"seen":0, "router_hit":0, "decoded":0, "quoted":0,
              "profitable":0, "errors":0}
stats_lock = threading.Lock()
_cache_lock = threading.Lock()

NULL_ADDR = "0x0000000000000000000000000000000000000000"
ETH_ADDR  = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

executor  = ThreadPoolExecutor(max_workers=64)


def ts():
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]

def log(msg):
    print(f"{ts()} {msg}", flush=True)

def to_hex(data) -> str:
    if isinstance(data, (bytes, bytearray)):
        return "0x" + data.hex()
    s = str(data) if data else "0x"
    return s if s.startswith("0x") else "0x" + s


# ── Token helpers ─────────────────────────────────────────────────────────────
def get_decimals(w3, token: str) -> int:
    t = token.lower()
    with _cache_lock:
        if t in DECIMALS: return DECIMALS[t]
    try:
        d = w3.eth.contract(
            address=Web3.to_checksum_address(t), abi=ERC20_ABI
        ).functions.decimals().call()
        with _cache_lock: DECIMALS[t] = d
        return d
    except:
        return 18

def get_symbol(w3, token: str) -> str:
    t = token.lower()
    with _cache_lock:
        if t in SYMBOLS: return SYMBOLS[t]
    try:
        s = w3.eth.contract(
            address=Web3.to_checksum_address(t), abi=ERC20_ABI
        ).functions.symbol().call()
    except:
        s = t[:8]
    with _cache_lock: SYMBOLS[t] = s
    return s


# ── V3 Quoter ─────────────────────────────────────────────────────────────────
def quote_single(w3, tin: str, tout: str, ain: int, fee: int) -> tuple[int, int]:
    """Returns (amountOut, fee) or (0, fee) on failure."""
    try:
        r = w3.eth.contract(
            address=Web3.to_checksum_address(QUOTER_V2), abi=QUOTER_ABI
        ).functions.quoteExactInputSingle({
            "tokenIn":           Web3.to_checksum_address(tin),
            "tokenOut":          Web3.to_checksum_address(tout),
            "amountIn":          ain,
            "fee":               fee,
            "sqrtPriceLimitX96": 0,
        }).call()
        return r[0], fee
    except:
        return 0, fee

def quote_best(w3, tin: str, tout: str, ain: int) -> tuple[int, int]:
    """Quote across all fee tiers concurrently. Returns (best_out, best_fee)."""
    best_out, best_fee = 0, 0
    with ThreadPoolExecutor(max_workers=len(FEE_TIERS)) as ex:
        futs = {ex.submit(quote_single, w3, tin, tout, ain, f): f for f in FEE_TIERS}
        for fut in as_completed(futs):
            out, fee = fut.result()
            if out > best_out:
                best_out, best_fee = out, fee
    return best_out, best_fee


# ── Gas cost in WETH (wei) ────────────────────────────────────────────────────
def gas_cost_weth(w3, n_hops: int) -> int:
    """
    Estimate gas cost in WETH wei.
    n_hops: number of swaps in the execution (2 for direct, 4 for bridge).
    Each swap ~150k gas, flashloan overhead ~80k.
    """
    try:
        base_fee     = w3.eth.get_block("latest")["baseFeePerGas"]
        priority_fee = w3.eth.max_priority_fee
        gas_price    = base_fee + priority_fee
        gas_units    = 80_000 + (n_hops * 150_000)
        return gas_price * gas_units
    except:
        return 0

def weth_to_token(w3, weth_amount: int, token: str) -> int:
    """Convert WETH amount to token amount via V3 quote. Returns 0 on failure."""
    if token.lower() == WETH: return weth_amount
    out, _ = quote_best(w3, WETH, token, weth_amount)
    return out


# ── Aave fee ──────────────────────────────────────────────────────────────────
def aave_fee(amount: int) -> int:
    """0.09% of flashloan amount."""
    return amount * AAVE_FEE_BP // 10_000


# ── Decoder ───────────────────────────────────────────────────────────────────
def decode_swap(w3, tx: dict) -> tuple[str, str, int] | None:
    """
    Decode swap calldata.
    Returns (token_in, token_out, amount_in) all lowercase, or None.
    """
    data = to_hex(tx.get("input") or b"")
    if len(data) < 10:
        return None

    try:
        sel = bytes.fromhex(data[2:10])
    except ValueError:
        return None

    flavor = SELECTORS.get(sel)
    if flavor is None:
        log(f"  [decode] unknown sel 0x{data[2:10]}")
        return None

    raw = bytes.fromhex(data[10:])

    try:
        if flavor == "v2":
            dec  = w3.codec.decode(["uint256","uint256","address[]","address","uint256"], raw)
            path = dec[2]
            if len(path) < 2: return None
            return path[0].lower(), path[-1].lower(), dec[0]

        if flavor == "v2_eth_in":
            dec  = w3.codec.decode(["uint256","address[]","address","uint256"], raw)
            path = dec[1]
            if len(path) < 2: return None
            return WETH, path[-1].lower(), int(tx.get("value", 0))

        if flavor == "v3_single":
            dec = w3.codec.decode(
                ["(address,address,uint24,address,uint256,uint256,uint256,uint160)"], raw)
            p   = dec[0]
            amt = p[5] if p[5] > 0 else p[6]
            return p[0].lower(), p[1].lower(), amt

        if flavor == "v3_multi":
            dec = w3.codec.decode(["(bytes,address,uint256,uint256,uint256)"], raw)
            pb  = dec[0][0]
            if len(pb) < 43: return None
            tin  = "0x" + pb[:20].hex()
            tout = "0x" + pb[-20:].hex()
            amt  = dec[0][3] if dec[0][3] > 0 else dec[0][4]
            return tin.lower(), tout.lower(), amt

        # universal router: too complex without full command parsing
        return None

    except Exception as e:
        log(f"  [decode] ABI error flavor={flavor}: {e}")
        return None


# ── Core evaluator ────────────────────────────────────────────────────────────
def evaluate(w3, tx: dict) -> dict | None:
    """
    Full two-leg profit calculation with Aave flashloan routing.

    DIRECT (tokenIn in Aave):
      flashloan tokenIn
      → swap tokenIn → tokenOut  (backrun the victim)
      → swap tokenOut → tokenIn  (close position)
      → repay tokenIn + 0.09%
      profit = tokenIn_returned - tokenIn_borrowed - aave_fee - gas

    BRIDGE (tokenIn NOT in Aave):
      flashloan USDC (or best bridge token)
      → swap bridge → tokenIn    (acquire tokenIn)
      → swap tokenIn → tokenOut  (backrun the victim)
      → swap tokenOut → tokenIn  (close position)
      → swap tokenIn → bridge    (return to bridge token)
      → repay bridge + 0.09%
      profit = bridge_returned - bridge_borrowed - aave_fee - gas

    Returns opportunity dict or None.
    """
    decoded = decode_swap(w3, tx)
    if decoded is None:
        return None

    tin, tout, ain = decoded
    if ain == 0: return None
    if tout.lower() in (NULL_ADDR, ETH_ADDR, tin.lower()): return None

    with stats_lock: stats["decoded"] += 1

    tin_in_aave = tin.lower() in AAVE_ASSETS_LOWER

    # ── DIRECT PATH ──────────────────────────────────────────────────────────
    if tin_in_aave:
        # Leg 1: buy tokenOut with tokenIn (same direction as victim, after victim)
        buy_out, buy_fee = quote_best(w3, tin, tout, ain)
        if buy_out == 0: return None

        # Leg 2: sell tokenOut back to tokenIn
        sell_out, sell_fee = quote_best(w3, tout, tin, buy_out)
        if sell_out == 0: return None

        # Costs
        gas_weth   = gas_cost_weth(w3, n_hops=2)
        gas_in_tin = weth_to_token(w3, gas_weth, tin)
        loan_fee   = aave_fee(ain)
        total_cost = ain + loan_fee + gas_in_tin   # what we owe
        net_raw    = sell_out - total_cost          # profit in tokenIn units

        if net_raw <= MIN_PROFIT:
            return None

        with stats_lock: stats["profitable"] += 1

        dec_in  = get_decimals(w3, tin)
        dec_out = get_decimals(w3, tout)
        sym_in  = get_symbol(w3, tin)
        sym_out = get_symbol(w3, tout)

        return {
            "path_type":   "DIRECT",
            "tx_hash":     to_hex(tx.get("hash", b"")),
            "router":      ROUTERS.get((tx.get("to") or "").lower(), "?"),
            "flashloan_token":  tin,
            "flashloan_amount": ain,
            "flashloan_fee":    loan_fee,
            "leg1_in":    tin,  "leg1_out":   tout, "leg1_fee": buy_fee,
            "leg1_ain":   ain,  "leg1_aout":  buy_out,
            "leg2_in":    tout, "leg2_out":   tin,  "leg2_fee": sell_fee,
            "leg2_ain":   buy_out, "leg2_aout": sell_out,
            "gas_weth":   gas_weth,
            "gas_in_tin": gas_in_tin,
            "net_raw":    net_raw,
            "profit_token": tin,
            "dec_profit": dec_in,
            "sym_profit": sym_in,
            "dec_in":  dec_in, "dec_out": dec_out,
            "sym_in":  sym_in, "sym_out": sym_out,
        }

    # ── BRIDGE PATH ──────────────────────────────────────────────────────────
    # Find best bridge token: first in BRIDGE_PRIORITY that has V3 pool with tokenIn
    bridge_token = None
    bridge_to_tin_out = 0
    bridge_to_tin_fee = 0

    for candidate in BRIDGE_PRIORITY:
        if candidate.lower() == tin.lower():
            continue
        # How much tokenIn we get for 1 unit of bridge (for sizing)
        # We'll properly size below
        test_out, test_fee = quote_best(w3, candidate, tin, 10**get_decimals(w3, candidate))
        if test_out > 0:
            bridge_token      = candidate
            bridge_to_tin_fee = test_fee
            break

    if bridge_token is None:
        log(f"  [bridge] no bridge path found for {tin[:10]}")
        return None

    dec_bridge = get_decimals(w3, bridge_token)
    sym_bridge = get_symbol(w3, bridge_token)

    # Size the flashloan: we need `ain` of tokenIn
    # Quote bridge→tokenIn for ain units:
    # We need to find how much bridge gives us `ain` tokenIn.
    # Approximate: quote bridge→tin with a reference amount, extrapolate.
    ref_bridge = 10 ** dec_bridge  # 1 unit of bridge
    ref_tin_out, _ = quote_best(w3, bridge_token, tin, ref_bridge)
    if ref_tin_out == 0: return None

    # bridge_needed ≈ ain * ref_bridge / ref_tin_out
    bridge_needed = ain * ref_bridge // ref_tin_out
    if bridge_needed == 0: return None

    # Now quote all 4 legs properly
    # Leg 0: bridge → tokenIn (acquire tokenIn)
    leg0_out, leg0_fee = quote_best(w3, bridge_token, tin, bridge_needed)
    if leg0_out == 0: return None

    # Leg 1: tokenIn → tokenOut (the backrun)
    leg1_out, leg1_fee = quote_best(w3, tin, tout, leg0_out)
    if leg1_out == 0: return None

    # Leg 2: tokenOut → tokenIn (close tokenOut position)
    leg2_out, leg2_fee = quote_best(w3, tout, tin, leg1_out)
    if leg2_out == 0: return None

    # Leg 3: tokenIn → bridge (return to bridge token)
    leg3_out, leg3_fee = quote_best(w3, tin, bridge_token, leg2_out)
    if leg3_out == 0: return None

    # Costs in bridge token
    gas_weth      = gas_cost_weth(w3, n_hops=4)
    gas_in_bridge = weth_to_token(w3, gas_weth, bridge_token)
    loan_fee      = aave_fee(bridge_needed)
    total_cost    = bridge_needed + loan_fee + gas_in_bridge
    net_raw       = leg3_out - total_cost

    if net_raw <= MIN_PROFIT:
        return None

    with stats_lock: stats["profitable"] += 1

    dec_in  = get_decimals(w3, tin)
    dec_out = get_decimals(w3, tout)
    sym_in  = get_symbol(w3, tin)
    sym_out = get_symbol(w3, tout)

    return {
        "path_type":   "BRIDGE",
        "tx_hash":     to_hex(tx.get("hash", b"")),
        "router":      ROUTERS.get((tx.get("to") or "").lower(), "?"),
        "flashloan_token":  bridge_token,
        "flashloan_amount": bridge_needed,
        "flashloan_fee":    loan_fee,
        "leg0_in":  bridge_token, "leg0_out": tin,  "leg0_fee": leg0_fee,
        "leg0_ain": bridge_needed,"leg0_aout": leg0_out,
        "leg1_in":  tin,          "leg1_out": tout, "leg1_fee": leg1_fee,
        "leg1_ain": leg0_out,     "leg1_aout": leg1_out,
        "leg2_in":  tout,         "leg2_out": tin,  "leg2_fee": leg2_fee,
        "leg2_ain": leg1_out,     "leg2_aout": leg2_out,
        "leg3_in":  tin,          "leg3_out": bridge_token, "leg3_fee": leg3_fee,
        "leg3_ain": leg2_out,     "leg3_aout": leg3_out,
        "gas_weth":      gas_weth,
        "gas_in_bridge": gas_in_bridge,
        "net_raw":       net_raw,
        "profit_token":  bridge_token,
        "dec_profit":    dec_bridge,
        "sym_profit":    sym_bridge,
        "dec_in":  dec_in, "dec_out": dec_out,
        "sym_in":  sym_in, "sym_out": sym_out,
        "sym_bridge": sym_bridge, "dec_bridge": dec_bridge,
    }


# ── Printer ───────────────────────────────────────────────────────────────────
def print_opportunity(opp: dict):
    dp   = opp["dec_profit"]
    sp   = opp["sym_profit"]
    net  = opp["net_raw"] / 10**dp

    print(f"\n{'═'*62}", flush=True)
    print(f"{ts()} ✓ PROFITABLE  [{opp['path_type']}]", flush=True)
    print(f"  victim tx : {opp['tx_hash']}", flush=True)
    print(f"  router    : {opp['router']}", flush=True)
    print(f"  pair      : {opp['sym_in']} → {opp['sym_out']}", flush=True)
    print(f"  ─── flashloan ───", flush=True)

    fl_sym = get_symbol_cached(opp["flashloan_token"])
    fl_dec = opp.get("dec_bridge", opp["dec_in"])
    fl_amt = opp["flashloan_amount"] / 10**fl_dec
    fl_fee = opp["flashloan_fee"]    / 10**fl_dec
    print(f"  loan      : {fl_amt:.6f} {fl_sym}  fee={fl_fee:.6f} {fl_sym}", flush=True)

    print(f"  ─── execution path ───", flush=True)

    if opp["path_type"] == "DIRECT":
        d_in  = opp["dec_in"]
        d_out = opp["dec_out"]
        print(f"  leg 1     : {opp['sym_in']}→{opp['sym_out']}"
              f"  in={opp['leg1_ain']/10**d_in:.6f}"
              f"  out={opp['leg1_aout']/10**d_out:.6f}"
              f"  fee={opp['leg1_fee']}", flush=True)
        print(f"  leg 2     : {opp['sym_out']}→{opp['sym_in']}"
              f"  in={opp['leg2_ain']/10**d_out:.6f}"
              f"  out={opp['leg2_aout']/10**d_in:.6f}"
              f"  fee={opp['leg2_fee']}", flush=True)
        gc = opp["gas_in_tin"] / 10**d_in
        print(f"  gas cost  : {gc:.6f} {opp['sym_in']}", flush=True)

    else:  # BRIDGE
        db = opp["dec_bridge"]
        di = opp["dec_in"]
        do = opp["dec_out"]
        sb = opp["sym_bridge"]
        si = opp["sym_in"]
        so = opp["sym_out"]
        print(f"  leg 0     : {sb}→{si}"
              f"  in={opp['leg0_ain']/10**db:.6f}"
              f"  out={opp['leg0_aout']/10**di:.6f}"
              f"  fee={opp['leg0_fee']}", flush=True)
        print(f"  leg 1     : {si}→{so}"
              f"  in={opp['leg1_ain']/10**di:.6f}"
              f"  out={opp['leg1_aout']/10**do:.6f}"
              f"  fee={opp['leg1_fee']}", flush=True)
        print(f"  leg 2     : {so}→{si}"
              f"  in={opp['leg2_ain']/10**do:.6f}"
              f"  out={opp['leg2_aout']/10**di:.6f}"
              f"  fee={opp['leg2_fee']}", flush=True)
        print(f"  leg 3     : {si}→{sb}"
              f"  in={opp['leg3_ain']/10**di:.6f}"
              f"  out={opp['leg3_aout']/10**db:.6f}"
              f"  fee={opp['leg3_fee']}", flush=True)
        gc = opp["gas_in_bridge"] / 10**db
        print(f"  gas cost  : {gc:.6f} {sb}", flush=True)

    print(f"  ─── result ───", flush=True)
    print(f"  NET PROFIT: {net:.6f} {sp}", flush=True)
    print(f"{'═'*62}\n", flush=True)

def get_symbol_cached(token: str) -> str:
    t = token.lower()
    with _cache_lock:
        if t in SYMBOLS: return SYMBOLS[t]
    return t[:8]


# ── Tx handler ────────────────────────────────────────────────────────────────
def handle_tx(w3, tx_hash_hex: str):
    try:
        tx = w3.eth.get_transaction(tx_hash_hex)
    except Exception:
        return

    to = (tx.get("to") or "").lower()
    if to not in ROUTER_SET:
        return

    with stats_lock: stats["router_hit"] += 1

    # Normalise HexBytes fields
    tx = dict(tx)
    tx["input"] = to_hex(tx.get("input") or b"")

    try:
        opp = evaluate(w3, tx)
    except Exception as e:
        log(f"  [eval error] {e}")
        with stats_lock: stats["errors"] += 1
        return

    if opp is not None:
        with stats_lock: stats["quoted"] += 1
        print_opportunity(opp)


# ── IPC subscription (push) ───────────────────────────────────────────────────
def ipc_subscribe(w3):
    """
    Opens a raw Unix socket to the IPC path and subscribes to
    newPendingTransactions via eth_subscribe.
    Pushes each tx hash to the thread pool as it arrives.
    Reconnects automatically on socket error.
    """
    buf = b""
    while True:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(IPC_PATH)
            log("IPC socket connected")

            # Subscribe
            sub_req = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method":  "eth_subscribe",
                "params":  ["newPendingTransactions"]
            })
            sock.sendall(sub_req.encode() + b"\n")

            # First response = subscription id
            resp_raw = b""
            while b"\n" not in resp_raw:
                chunk = sock.recv(4096)
                if not chunk: break
                resp_raw += chunk
            sub_resp = json.loads(resp_raw.split(b"\n")[0])
            sub_id   = sub_resp.get("result", "?")
            log(f"Subscribed to newPendingTransactions  id={sub_id}")

            buf = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    log("IPC socket closed — reconnecting")
                    break
                buf += chunk
                # IPC sends one JSON object per line
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    with stats_lock: stats["seen"] += 1

                    tx_hash = msg.get("params", {}).get("result")
                    if tx_hash:
                        executor.submit(handle_tx, w3, tx_hash)

        except Exception as e:
            log(f"IPC error: {e} — reconnecting in 2s")
            time.sleep(2)


# ── Stats ─────────────────────────────────────────────────────────────────────
def print_stats():
    while True:
        time.sleep(30)
        with stats_lock: s = dict(stats)
        log(f"[stats] seen={s['seen']} router_hit={s['router_hit']} "
            f"decoded={s['decoded']} quoted={s['quoted']} "
            f"profitable={s['profitable']} errors={s['errors']}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 62, flush=True)
    print("Backrun Scanner — observe only, no execution", flush=True)
    print(f"IPC       : {IPC_PATH}", flush=True)
    print(f"Routers   : {len(ROUTERS)}", flush=True)
    print(f"Fee tiers : {FEE_TIERS}", flush=True)
    print(f"Aave fee  : {AAVE_FEE_BP/100:.2f}%", flush=True)
    print(f"Min profit: {MIN_PROFIT} (any positive)", flush=True)
    print("=" * 62, flush=True)

    w3 = Web3(Web3.IPCProvider(IPC_PATH))
    if not w3.is_connected():
        print(f"ERROR: cannot connect to {IPC_PATH}", flush=True)
        return

    log(f"web3 connected — block {w3.eth.block_number:,}")

    try:
        pool = w3.geth.txpool.status()
        log(f"txpool: pending={pool.get('pending','?')} queued={pool.get('queued','?')}")
    except Exception as e:
        log(f"txpool status: {e}")

    threading.Thread(target=print_stats, daemon=True).start()

    # IPC subscription runs in main thread — blocks forever
    ipc_subscribe(w3)


if __name__ == "__main__":
    main()