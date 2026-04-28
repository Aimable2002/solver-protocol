# ─────────────────────────────────────────────────────────────────────────────
# fork_test.py — end-to-end test against anvil mainnet fork
#
# USAGE:
#   # Start fork first:
#   anvil --fork-url /bsc/reth/reth.ipc --fork-block-number 24974000 --port 8547
#
#   # Then run:
#   export FILLERBOT_ADDRESS=0x659E0981563DF0fE69603c5555Adda9547C36781
#   export EXECUTOR_PRIVATE_KEY=0x...
#   python3 fork_test.py
#
# WHAT IT DOES:
#   1. Connects to anvil fork on 8547
#   2. Funds executor wallet with ETH via anvil_setBalance
#   3. Fetches real open UniswapX orders
#   4. Runs evaluate() on each — prints full math breakdown
#   5. Executes fillOrder() on the fork for profitable orders
#   6. Reads contract balance before/after to confirm real profit
# ─────────────────────────────────────────────────────────────────────────────
import os, time, requests
from datetime import datetime
from web3 import Web3
from eth_account import Account

# ── Override IPC to point at anvil fork ──────────────────────────────────────
os.environ["IPC_PATH"] = "http://127.0.0.1:8547"  # anvil HTTP

from config import FILLERBOT_ADDR, EXECUTOR_KEY, UNISWAPX_API, ORDER_LIMIT
from evaluator import evaluate, quote_best, get_decimals, token_to_usd, get_gas_price_usd
from executor  import FILLERBOT_ABI, execute_fill, wait_for_receipt

FORK_RPC = "http://127.0.0.1:8547"

ERC20_ABI = [
    {"inputs":[],"name":"decimals","outputs":[{"type":"uint8"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"symbol","outputs":[{"type":"string"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf",
     "outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
]

def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def fund_executor(w3: Web3, address: str):
    """Give executor 10 ETH on the fork via anvil_setBalance."""
    w3.provider.make_request("anvil_setBalance", [
        address,
        hex(10 * 10**18)
    ])
    bal = w3.eth.get_balance(address)
    log(f"Executor funded: {bal/1e18:.2f} ETH")


def get_token_symbol(w3: Web3, token: str) -> str:
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        return c.functions.symbol().call()
    except Exception:
        return token[:10]


def get_token_balance(w3: Web3, token: str, address: str) -> int:
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        return c.functions.balanceOf(Web3.to_checksum_address(address)).call()
    except Exception:
        return 0


def fetch_open_orders() -> list:
    try:
        r = requests.get(
            UNISWAPX_API,
            params={"orderStatus":"open","chainId":1,
                    "limit":ORDER_LIMIT,"orderType":"Dutch_V2"},
            timeout=10,
        )
        if r.status_code != 200:
            log(f"API error: {r.status_code}")
            return []
        return r.json().get("orders", [])
    except Exception as e:
        log(f"fetch error: {e}")
        return []


def main():
    if not EXECUTOR_KEY:
        log("ERROR: EXECUTOR_PRIVATE_KEY not set")
        return
    if not FILLERBOT_ADDR:
        log("ERROR: FILLERBOT_ADDRESS not set")
        return

    # ── Connect to fork ───────────────────────────────────────────────────────
    w3 = Web3(Web3.HTTPProvider(FORK_RPC))
    if not w3.is_connected():
        log(f"ERROR: cannot connect to fork at {FORK_RPC}")
        log("Make sure anvil is running: anvil --fork-url /bsc/reth/reth.ipc --port 8547")
        return

    log("=" * 60)
    log(f"Fork test — block {w3.eth.block_number:,} chainId={w3.eth.chain_id}")
    log(f"Contract: {FILLERBOT_ADDR}")
    log("=" * 60)

    account = Account.from_key(EXECUTOR_KEY)
    log(f"Executor: {account.address}")

    # Fund executor on fork
    fund_executor(w3, account.address)

    # ── Fetch orders ──────────────────────────────────────────────────────────
    log("\nFetching open orders...")
    orders = fetch_open_orders()
    log(f"Got {len(orders)} open orders\n")

    if not orders:
        log("No open orders. Try again in a few seconds.")
        return

    # ── Evaluate each order with full math breakdown ──────────────────────────
    stats = {
        "evaluated": 0, "profitable": 0,
        "filled_ok": 0, "filled_revert": 0,
        "total_profit_usd": 0.0
    }

    for i, order in enumerate(orders):
        order_hash = order.get("orderHash", "")[:16]
        token_in   = order.get("input", {}).get("token", "").lower()
        outputs    = [o for o in order.get("outputs", []) if not o.get("isFeeOutput", False)]
        if not outputs:
            continue

        primary   = max(outputs, key=lambda x: int(x.get("endAmount", 0)))
        token_out = primary.get("token", "").lower()
        ETH_ADDR  = "0x0000000000000000000000000000000000000000"
        if token_out == ETH_ADDR:
            continue

        sym_in  = get_token_symbol(w3, token_in)
        sym_out = get_token_symbol(w3, token_out)

        log(f"[{i+1}/{len(orders)}] {sym_in}->{sym_out} hash={order_hash}")

        stats["evaluated"] += 1

        # ── Full math breakdown ───────────────────────────────────────────────
        try:
            # Raw amounts
            cosigner_data  = order.get("cosignerData", {})
            input_override = cosigner_data.get("inputOverride", "0") or "0"
            amount_in      = (int(input_override) if input_override != "0"
                              else int(order.get("input", {}).get("startAmount", 0)))

            from evaluator import current_required_out
            required_out, _ = current_required_out(order)

            dec_in  = get_decimals(w3, token_in)
            dec_out = get_decimals(w3, token_out)

            log(f"  amount_in:    {amount_in / 10**dec_in:.6f} {sym_in}")
            log(f"  required_out: {required_out / 10**dec_out:.6f} {sym_out}")

            # V3 quotes per fee tier
            from config import FEE_TIERS
            from evaluator import _quote_single
            best_out = 0
            best_fee = 0
            for fee in FEE_TIERS:
                q = _quote_single(w3, token_in, token_out, amount_in, fee)
                label = f"  V3 quote [{fee}]: {q / 10**dec_out:.6f} {sym_out}"
                if q > best_out:
                    best_out = q
                    best_fee = fee
                    label += "  ← best"
                log(label)

            if best_out == 0:
                log("  SKIP — no V3 liquidity\n")
                continue

            if best_out <= required_out:
                log(f"  SKIP — V3 quote {best_out/10**dec_out:.6f} <= required {required_out/10**dec_out:.6f}\n")
                continue

            surplus_raw = best_out - required_out
            surplus_usd = token_to_usd(w3, token_out, surplus_raw, dec_out)
            gas_cost_usd = get_gas_price_usd(w3, 250_000)  # estimate before simulate
            profit_usd  = surplus_usd - gas_cost_usd

            log(f"  surplus:      {surplus_raw/10**dec_out:.6f} {sym_out} = ${surplus_usd:.4f}")
            log(f"  gas_cost:     ${gas_cost_usd:.4f}")
            log(f"  profit:       ${profit_usd:.4f}")

        except Exception as e:
            log(f"  math error: {e}\n")
            continue

        # ── Evaluate via evaluator.py ─────────────────────────────────────────
        fill = evaluate(w3, order)

        if fill is None:
            log(f"  evaluate() → NOT PROFITABLE\n")
            continue

        stats["profitable"] += 1
        log(f"  evaluate() → PROFITABLE profit=${fill['profit_usd']:.4f} fee={fill['pool_fee']}")

        # ── Execute on fork ───────────────────────────────────────────────────
        # Check contract balance before
        bal_before = get_token_balance(w3, token_in, FILLERBOT_ADDR)

        log(f"  Executing fillOrder() on fork...")
        try:
            tx_hash = execute_fill(w3, fill)
            log(f"  tx: {tx_hash}")

            receipt = wait_for_receipt(w3, tx_hash, timeout=30)
            if receipt is None:
                log(f"  TIMEOUT — no receipt\n")
                stats["filled_revert"] += 1
                continue

            if receipt["status"] == 1:
                # Check contract balance after — that's the real profit
                bal_after   = get_token_balance(w3, token_in, FILLERBOT_ADDR)
                real_profit = bal_after - bal_before
                real_profit_usd = token_to_usd(w3, token_in, max(real_profit, 0), dec_in)

                stats["filled_ok"]         += 1
                stats["total_profit_usd"]  += real_profit_usd

                log(f"  FILLED ✓ gas_used={receipt['gasUsed']:,}")
                log(f"  contract balance change: {real_profit/10**dec_in:.6f} {sym_in}"
                    f" = ${real_profit_usd:.4f}")
                log(f"  predicted=${fill['profit_usd']:.4f} actual=${real_profit_usd:.4f}"
                    f" diff=${real_profit_usd - fill['profit_usd']:.4f}")
            else:
                stats["filled_revert"] += 1
                log(f"  REVERTED gas_used={receipt['gasUsed']:,}")

        except Exception as e:
            stats["filled_revert"] += 1
            log(f"  execute error: {e}")

        log("")

    # ── Summary ───────────────────────────────────────────────────────────────
    log("=" * 60)
    log("FORK TEST SUMMARY")
    log("=" * 60)
    for k, v in stats.items():
        if isinstance(v, float):
            log(f"  {k:<22}: ${v:.4f}")
        else:
            log(f"  {k:<22}: {v}")
    if stats["filled_ok"] > 0:
        log(f"  avg_profit_usd       : ${stats['total_profit_usd']/stats['filled_ok']:.4f}")


if __name__ == "__main__":
    main()