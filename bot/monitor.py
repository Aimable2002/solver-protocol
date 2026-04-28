# ─────────────────────────────────────────────────────────────────────────────
# monitor.py — main polling loop with concurrent order evaluation
#
# USAGE:
#   export EXECUTOR_PRIVATE_KEY=0x...
#   export FILLERBOT_ADDRESS=0x...
#   export IPC_PATH=/bsc/reth/reth.ipc
#   python3 monitor.py
#
# HOW IT WORKS:
#   1. Poll UniswapX API every POLL_INTERVAL seconds for open orders
#   2. Filter unseen orders, then evaluate ALL of them concurrently using
#      a ThreadPoolExecutor (each order needs 3 QuoterV2 calls — one per
#      fee tier — so parallelism matters)
#   3. For each profitable result: call FillerBot.fillOrder() via executor
#   4. Log result and continue
#
# CONCURRENCY MODEL:
#   evaluate() is a blocking function (web3 calls). We submit one Future
#   per order to a ThreadPoolExecutor, then collect results. Execution
#   (filling) is still sequential to avoid nonce collisions.
# ─────────────────────────────────────────────────────────────────────────────
import time
import asyncio
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from web3 import Web3

from config import (
    IPC_PATH, UNISWAPX_API, POLL_INTERVAL,
    ORDER_LIMIT, FILLERBOT_ADDR, EXECUTOR_KEY,
    MAX_EVAL_WORKERS,
)
from evaluator import evaluate, get_eth_price_usd, QuoteError
from executor  import execute_fill, wait_for_receipt


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ts} {msg}", flush=True)


def fetch_open_orders() -> list:
    """Fetch open UniswapX Dutch V2 orders from the API."""
    try:
        r = requests.get(
            UNISWAPX_API,
            params={
                "orderStatus": "open",
                "chainId":     1,
                "limit":       ORDER_LIMIT,
                "orderType":   "Dutch_V2",
            },
            timeout=10,
        )
        if r.status_code != 200:
            log(f"  API error: {r.status_code} {r.text[:100]}")
            return []
        return r.json().get("orders", [])
    except Exception as exc:
        log(f"  fetch_open_orders error: {exc}")
        return []


def evaluate_orders_concurrently(
    w3: Web3,
    orders: list,
    eth_price: float,
    executor: ThreadPoolExecutor,
) -> list[dict]:
    """
    Submit all orders for concurrent profitability evaluation.

    Returns a list of profitable fill-param dicts, in the order results
    arrive (fastest-resolving first). Unprofitable or erroring orders
    produce None and are filtered out.
    """
    # Map future → order_hash so we can log which order errored
    future_to_hash = {
        executor.submit(evaluate, w3, order, eth_price): order.get("orderHash", "?")
        for order in orders
    }

    profitable = []
    for future in as_completed(future_to_hash):
        order_hash = future_to_hash[future]
        try:
            result = future.result()
            if result is not None:
                profitable.append(result)
        except Exception as exc:
            # evaluate() itself shouldn't raise (it returns None on errors),
            # but guard anyway so one bad order can't crash the whole batch.
            log(f"  eval exception [{order_hash[:10]}]: {exc}")

    return profitable


def main():
    # ── Startup checks ────────────────────────────────────────────────────────
    if not EXECUTOR_KEY:
        log("ERROR: EXECUTOR_PRIVATE_KEY not set")
        return
    if not FILLERBOT_ADDR:
        log("ERROR: FILLERBOT_ADDRESS not set")
        return

    log("=" * 55)
    log("UniswapX FillerBot starting")
    log(f"  IPC:        {IPC_PATH}")
    log(f"  Contract:   {FILLERBOT_ADDR}")
    log(f"  Poll:       every {POLL_INTERVAL}s")
    log(f"  Eval workers: {MAX_EVAL_WORKERS}")
    log("=" * 55)

    # ── Connect to local node ─────────────────────────────────────────────────
    w3 = Web3(Web3.IPCProvider(IPC_PATH))
    if not w3.is_connected():
        log(f"ERROR: cannot connect to node at {IPC_PATH}")
        return

    block = w3.eth.block_number
    log(f"Connected to node — block {block:,}")

    # ── Initial ETH price ─────────────────────────────────────────────────────
    # Hard-fail on startup if we can't get a real price.
    try:
        eth_price = get_eth_price_usd(w3)
    except QuoteError as exc:
        log(f"ERROR: cannot fetch ETH price on startup: {exc}")
        return

    eth_price_ts  = time.time()
    ETH_PRICE_TTL = 60  # seconds between refreshes

    log(f"ETH price: ${eth_price:,.2f}")
    log("Watching for orders...")
    log("")

    # ── Counters ──────────────────────────────────────────────────────────────
    seen        = set()
    fill_count  = 0
    skip_count  = 0

    # ── Shared thread pool for concurrent evaluation ──────────────────────────
    with ThreadPoolExecutor(max_workers=MAX_EVAL_WORKERS) as executor:
        while True:
            try:
                # ── Refresh ETH price periodically ───────────────────────────
                if time.time() - eth_price_ts > ETH_PRICE_TTL:
                    try:
                        eth_price    = get_eth_price_usd(w3)
                        eth_price_ts = time.time()
                        log(f"ETH price refreshed: ${eth_price:,.2f}")
                    except QuoteError as exc:
                        # Keep using stale price rather than crashing the loop,
                        # but log loudly so the operator knows.
                        log(f"  WARNING: ETH price refresh failed ({exc}) — using stale ${eth_price:,.2f}")

                # ── Fetch orders ──────────────────────────────────────────────
                orders = fetch_open_orders()
                if not orders:
                    time.sleep(POLL_INTERVAL)
                    continue

                log(f"Fetched {len(orders)} open orders")

                # ── Filter already-seen orders ────────────────────────────────
                new_orders = []
                for order in orders:
                    h = order.get("orderHash", "")
                    if h in seen:
                        continue
                    seen.add(h)
                    new_orders.append(order)

                if not new_orders:
                    time.sleep(POLL_INTERVAL)
                    continue

                skip_count += len(orders) - len(new_orders)
                log(f"  {len(new_orders)} new orders to evaluate")

                # ── Concurrent evaluation ─────────────────────────────────────
                profitable = evaluate_orders_concurrently(
                    w3, new_orders, eth_price, executor
                )
                skip_count += len(new_orders) - len(profitable)

                if not profitable:
                    time.sleep(POLL_INTERVAL)
                    continue

                log(f"  {len(profitable)} profitable order(s) found")

                # ── Sequential execution (one at a time to avoid nonce races) ─
                for fill in profitable:
                    log(
                        f"  PROFITABLE: {fill['token_in'][:10]}->{fill['token_out'][:10]}"
                        f" | profit=${fill['profit_usd']:.4f}"
                        f" | surplus=${fill['surplus_usd']:.4f}"
                        f" | gas=${fill['gas_cost_usd']:.4f}"
                        f" | fee={fill['pool_fee']}"
                    )

                    try:
                        tx_hash = execute_fill(w3, fill)
                        log(f"  TX submitted: {tx_hash}")

                        receipt = wait_for_receipt(w3, tx_hash, timeout=60)
                        if receipt and receipt["status"] == 1:
                            fill_count += 1
                            log(
                                f"  FILLED ✓ gas_used={receipt['gasUsed']:,}"
                                f" | total_fills={fill_count}"
                            )
                        elif receipt:
                            log(f"  TX REVERTED — gas_used={receipt['gasUsed']:,}")
                        else:
                            log(f"  TX timeout — no receipt within 60s")

                    except Exception as exc:
                        log(f"  execute error: {exc}")

                # ── Bound the seen set ────────────────────────────────────────
                if len(seen) > 10_000:
                    seen = set(list(seen)[-5_000:])

            except KeyboardInterrupt:
                log("")
                log(f"Stopped. fills={fill_count} skipped={skip_count}")
                break
            except Exception as exc:
                log(f"Loop error: {exc}")

            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()