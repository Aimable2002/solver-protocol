# backrun_scanner.py — observe only, no execution
import time, threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from web3 import Web3

IPC_PATH   = "/bsc/reth/reth.ipc"
MIN_PROFIT = 0
FEE_TIERS  = [100, 500, 3000, 10000]

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6231488ccd050ce3eba90c95bdc17b4"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
DAI  = "0x6b175474e89094c44da98b954eedeac495271d0f"
WBTC = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"

DECIMALS: dict[str, int] = {WETH:18, WBTC:8, USDC:6, USDT:6, DAI:18}
SYMBOLS:  dict[str, str]  = {WETH:"WETH", WBTC:"WBTC", USDC:"USDC", USDT:"USDT", DAI:"DAI"}

QUOTER_V2  = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
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

SELECTORS = {
    bytes.fromhex("38ed1739"): "v2",
    bytes.fromhex("8803dbee"): "v2",
    bytes.fromhex("18cbafe5"): "v2",
    bytes.fromhex("7ff36ab5"): "v2_eth_in",
    bytes.fromhex("b6f9de95"): "v2_eth_in",
    bytes.fromhex("414bf389"): "v3_single",
    bytes.fromhex("db3e2198"): "v3_single",
    bytes.fromhex("c04b8d59"): "v3_multi",
    bytes.fromhex("f28c0498"): "v3_multi",
    bytes.fromhex("3593564c"): "universal",
    bytes.fromhex("24856bc3"): "universal",
}

stats      = {"seen":0,"decoded":0,"quoted":0,"profitable":0,"errors":0}
stats_lock = threading.Lock()
_dec_lock  = threading.Lock()
NULL_ADDR  = "0x0000000000000000000000000000000000000000"
ETH_ADDR   = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"


def ts():
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]

def log(msg):
    print(f"{ts()} {msg}", flush=True)

def to_hex(data) -> str:
    if isinstance(data, (bytes, bytearray)):
        return "0x" + data.hex()
    s = str(data) if data else "0x"
    return s if s.startswith("0x") else "0x" + s

def get_decimals(w3, token):
    t = token.lower()
    with _dec_lock:
        if t in DECIMALS: return DECIMALS[t]
    try:
        d = w3.eth.contract(address=Web3.to_checksum_address(t), abi=ERC20_ABI).functions.decimals().call()
        with _dec_lock: DECIMALS[t] = d
        return d
    except: return 18

def get_symbol(w3, token):
    t = token.lower()
    with _dec_lock:
        if t in SYMBOLS: return SYMBOLS[t]
    try:
        s = w3.eth.contract(address=Web3.to_checksum_address(t), abi=ERC20_ABI).functions.symbol().call()
    except: s = t[:8]
    with _dec_lock: SYMBOLS[t] = s
    return s

def quote_single(w3, tin, tout, ain, fee):
    try:
        r = w3.eth.contract(address=Web3.to_checksum_address(QUOTER_V2), abi=QUOTER_ABI)\
            .functions.quoteExactInputSingle({
                "tokenIn": Web3.to_checksum_address(tin),
                "tokenOut": Web3.to_checksum_address(tout),
                "amountIn": ain, "fee": fee, "sqrtPriceLimitX96": 0,
            }).call()
        return r[0]
    except: return 0

def quote_best(w3, tin, tout, ain):
    best_out, best_fee = 0, 0
    with ThreadPoolExecutor(max_workers=len(FEE_TIERS)) as ex:
        futs = {ex.submit(quote_single, w3, tin, tout, ain, f): f for f in FEE_TIERS}
        for fut in as_completed(futs):
            out = fut.result()
            if out > best_out:
                best_out, best_fee = out, futs[fut]
    return best_out, best_fee

def gas_cost_in_token(w3, token_out):
    try:
        base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
        eth_cost = base_fee * 300_000
        tl = token_out.lower()
        if tl in (USDC, USDT, DAI):
            eth_in_stable, _ = quote_best(w3, WETH, USDC, 10**18)
            if eth_in_stable == 0: return 0
            return eth_cost * eth_in_stable // 10**18
        if tl == WETH: return eth_cost
        weth_out, _ = quote_best(w3, WETH, token_out, 10**18)
        if weth_out == 0: return 0
        return eth_cost * weth_out // 10**18
    except: return 0


def decode_swap(w3, tx):
    data = to_hex(tx.get("input") or b"")
    if len(data) < 10:
        return None

    sel_hex = data[2:10]
    try:
        sel = bytes.fromhex(sel_hex)
    except ValueError:
        log(f"  [decode] bad hex sel: {sel_hex}")
        return None

    flavor = SELECTORS.get(sel)
    if flavor is None:
        log(f"  [decode] unknown sel: 0x{sel_hex}")
        return None

    raw = bytes.fromhex(data[10:])

    try:
        if flavor == "v2":
            dec = w3.codec.decode(["uint256","uint256","address[]","address","uint256"], raw)
            path = dec[2]
            if len(path) < 2: return None
            return path[0].lower(), path[-1].lower(), dec[0]

        if flavor == "v2_eth_in":
            dec = w3.codec.decode(["uint256","address[]","address","uint256"], raw)
            path = dec[1]
            if len(path) < 2: return None
            return WETH, path[-1].lower(), tx.get("value", 0)

        if flavor == "v3_single":
            dec = w3.codec.decode(["(address,address,uint24,address,uint256,uint256,uint256,uint160)"], raw)
            p = dec[0]
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

        # universal router — skip, too complex without full command parsing
        log(f"  [decode] universal router — skip")
        return None

    except Exception as e:
        log(f"  [decode] ABI error flavor={flavor}: {e}")
        return None


def evaluate_backrun(w3, tx):
    decoded = decode_swap(w3, tx)
    if decoded is None:
        return None

    tin, tout, ain = decoded
    if ain == 0:
        log(f"  [eval] amount_in=0")
        return None
    if tout.lower() in (NULL_ADDR, ETH_ADDR):
        log(f"  [eval] ETH output — skip")
        return None

    v3_out, fee = quote_best(w3, tin, tout, ain)
    if v3_out == 0:
        log(f"  [eval] no V3 liquidity {tin[:10]}->{tout[:10]}")
        return None

    dec_in  = get_decimals(w3, tin)
    dec_out = get_decimals(w3, tout)
    gas     = gas_cost_in_token(w3, tout)
    net     = v3_out - gas

    return {
        "tx_hash": to_hex(tx.get("hash", b"")),
        "router":  ROUTERS.get((tx.get("to") or "").lower(), "?"),
        "token_in": tin, "token_out": tout,
        "amount_in": ain, "v3_out": v3_out,
        "gas_cost": gas, "net_raw": net,
        "fee": fee, "dec_in": dec_in, "dec_out": dec_out,
    }


def handle_pending_tx(w3, tx_hash):
    try:
        tx = w3.eth.get_transaction(tx_hash)
    except Exception:
        return

    to = (tx.get("to") or "").lower()
    if to not in ROUTER_SET:
        return

    tx = dict(tx)
    tx["input"] = to_hex(tx.get("input") or b"")

    router_name = ROUTERS.get(to, to[:12])
    sel = tx["input"][2:10] if len(tx["input"]) >= 10 else "????"
    log(f"  [hit] {router_name} | sel=0x{sel} | inputlen={len(tx['input'])}")

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
    ain_h   = opp["amount_in"] / 10**opp["dec_in"]
    out_h   = opp["v3_out"]    / 10**opp["dec_out"]
    gas_h   = opp["gas_cost"]  / 10**opp["dec_out"]
    net_h   = opp["net_raw"]   / 10**opp["dec_out"]

    if opp["net_raw"] > MIN_PROFIT:
        with stats_lock:
            stats["profitable"] += 1
        print(f"\n{'─'*60}", flush=True)
        print(f"{ts()} ✓ PROFITABLE", flush=True)
        print(f"  tx:        {opp['tx_hash']}", flush=True)
        print(f"  router:    {opp['router']}", flush=True)
        print(f"  pair:      {sym_in} → {sym_out}  fee={opp['fee']}", flush=True)
        print(f"  amount_in: {ain_h:.6f} {sym_in}", flush=True)
        print(f"  v3_out:    {out_h:.6f} {sym_out}", flush=True)
        print(f"  gas_cost:  {gas_h:.6f} {sym_out}", flush=True)
        print(f"  NET:       {net_h:.6f} {sym_out}", flush=True)
        print(f"{'─'*60}\n", flush=True)
    else:
        sign = "+" if net_h >= 0 else ""
        log(f"  skip {sym_in}->{sym_out} net={sign}{net_h:.4f} {sym_out} "
            f"v3={out_h:.4f} gas={gas_h:.4f}")


def print_stats():
    while True:
        time.sleep(30)
        with stats_lock: s = dict(stats)
        log(f"[stats] seen={s['seen']} decoded={s['decoded']} "
            f"quoted={s['quoted']} profitable={s['profitable']} errors={s['errors']}")


def main():
    print("=" * 60, flush=True)
    print("Backrun Scanner v2 — observe only", flush=True)
    print(f"IPC:     {IPC_PATH}", flush=True)
    print(f"Routers: {len(ROUTERS)}", flush=True)
    print(f"Fees:    {FEE_TIERS}", flush=True)
    print("=" * 60, flush=True)

    w3 = Web3(Web3.IPCProvider(IPC_PATH))
    if not w3.is_connected():
        print(f"ERROR: cannot connect to {IPC_PATH}", flush=True)
        return

    print(f"Connected — block {w3.eth.block_number:,}", flush=True)

    try:
        pool = w3.geth.txpool.status()
        log(f"txpool: pending={pool.get('pending','?')} queued={pool.get('queued','?')}")
    except Exception as e:
        log(f"txpool status unavailable ({e})")

    print("Subscribing to pending txs...\n", flush=True)
    threading.Thread(target=print_stats, daemon=True).start()
    executor = ThreadPoolExecutor(max_workers=32)
    sub = w3.eth.filter("pending")
    log("Filter active. Polling every 100ms...")

    try:
        while True:
            try:
                hashes = sub.get_new_entries()
                if hashes:
                    log(f"  [poll] {len(hashes)} new hashes")
                for h in hashes:
                    executor.submit(handle_pending_tx, w3, h)
            except Exception as e:
                with stats_lock: stats["errors"] += 1
                log(f"filter error: {e}")
                try: sub = w3.eth.filter("pending")
                except: pass
            time.sleep(0.1)

    except KeyboardInterrupt:
        with stats_lock: s = dict(stats)
        print(f"\nStopped | seen={s['seen']} decoded={s['decoded']} "
              f"quoted={s['quoted']} profitable={s['profitable']}", flush=True)
        executor.shutdown(wait=False)


if __name__ == "__main__":
    main()