import time
import threading
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from web3 import Web3

from config import (
    IPC_PATH, UNISWAPX_API, POLL_INTERVAL,
    ORDER_LIMIT, FILLERBOT_ADDR, EXECUTOR_KEY
)
from evaluator import evaluate
from executor  import execute_fill, wait_for_receipt


def log(msg: str):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def fetch_open_orders() -> list:
    """Fetch open orders from UniswapX API across all supported order types."""
    orders = []
    for order_type in ("Dutch_V2", "Limit"):
        try:
            r = requests.get(
                UNISWAPX_API,
                params={
                    "orderStatus": "open",
                    "chainId":     1,
                    "limit":       ORDER_LIMIT,
                    "orderType":   order_type,
                },
                timeout=10,
            )
            if r.status_code != 200:
                log(f"  API {r.status_code} ({order_type}): {r.text[:80]}")
                continue
            orders.extend(r.json().get("orders", []))
        except Exception as e:
            log(f"  fetch error ({order_type}): {e}")
    return orders


def evaluate_batch(w3: Web3, orders: list, verbose: bool = False) -> list[dict]:
    """
    Evaluate all orders concurrently.
    Returns list of fill-param dicts where v3_quote > required_out.
    """
    if not orders:
        return []

    results = []
    workers = 1 if verbose else min(len(orders), 20)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(evaluate, w3, o, verbose): o for o in orders}
        for fut in as_completed(futures):
            result = fut.result()
            if result is not None:
                results.append(result)

    return results


def _submit_and_watch(w3: Web3, fill: dict, fill_fn, fill_count: list):
    """
    Submit a fill and wait for receipt in a background thread.
    Never blocks the main poll loop.
    """
    order_hash = fill.get("order_hash", "?")
    try:
        tx_hash = fill_fn(w3, fill)
        log(f"    tx: {tx_hash}  order={order_hash}")
        receipt = wait_for_receipt(w3, tx_hash, timeout=60)
        if receipt and receipt["status"] == 1:
            fill_count[0] += 1
            log(f"    FILLED ✓ order={order_hash}"
                f" gas={receipt['gasUsed']:,}"
                f" total_fills={fill_count[0]}")
        elif receipt:
            log(f"    REVERTED order={order_hash}"
                f" gas={receipt['gasUsed']:,}")
        else:
            log(f"    TIMEOUT order={order_hash} — no receipt in 60s")
    except Exception as e:
        log(f"    execute error order={order_hash}: {e}")


def run_loop(w3: Web3, fill_fn=None, verbose: bool = False):
    """
    Main polling loop. Runs forever until KeyboardInterrupt.
    Fill submission and receipt watching are non-blocking — run in background threads.
    """
    if fill_fn is None:
        fill_fn = execute_fill

    log(f"Node connected — block {w3.eth.block_number:,}")
    log("Watching for orders...")
    log("")

    seen       = set()
    fill_count = [0]   # list so background threads can mutate it
    skip_count = 0

    while True:
        try:
            orders = fetch_open_orders()
            if not orders:
                time.sleep(POLL_INTERVAL)
                continue

            # Filter already-seen order hashes
            new_orders = [o for o in orders
                          if o.get("orderHash", "") not in seen]
            for o in orders:
                seen.add(o.get("orderHash", ""))

            if not new_orders:
                time.sleep(POLL_INTERVAL)
                continue

            log(f"Fetched {len(orders)} orders | {len(new_orders)} new")

            # Evaluate all new orders concurrently
            t0         = time.time()
            fillable   = evaluate_batch(w3, new_orders, verbose=verbose)
            eval_ms    = int((time.time() - t0) * 1000)
            skip_count += len(new_orders) - len(fillable)

            log(f"  Evaluated {len(new_orders)} in {eval_ms}ms"
                f" | fillable={len(fillable)}")

            # Submit fills — each in its own background thread, never blocks loop
            for fill in sorted(fillable, key=lambda x: -x["surplus_raw"]):
                log(f"  ATTEMPTING {fill['token_in'][:10]}->{fill['token_out'][:10]}"
                    f" order={fill['order_hash']}"
                    f" surplus_raw={fill['surplus_raw']}"
                    f" v3_quote={fill['v3_quote']}"
                    f" required_out={fill['required_out']}"
                    f" fee={fill['pool_fee']}")
                t = threading.Thread(
                    target=_submit_and_watch,
                    args=(w3, fill, fill_fn, fill_count),
                    daemon=True,
                )
                t.start()

            # Bound seen set to avoid unbounded memory growth
            if len(seen) > 10_000:
                seen = set(list(seen)[-5_000:])

        except KeyboardInterrupt:
            log("")
            log(f"Stopped — fills={fill_count[0]} skipped={skip_count}")
            break
        except Exception as e:
            log(f"Loop error: {e}")

        time.sleep(POLL_INTERVAL)


def main():
    if not EXECUTOR_KEY:
        log("ERROR: EXECUTOR_PRIVATE_KEY not set")
        return
    if not FILLERBOT_ADDR:
        log("ERROR: FILLERBOT_ADDRESS not set")
        return

    log("=" * 55)
    log("UniswapX FillerBot")
    log(f"  IPC:      {IPC_PATH}")
    log(f"  Contract: {FILLERBOT_ADDR}")
    log(f"  Poll:     every {POLL_INTERVAL}s")
    log("=" * 55)

    w3 = Web3(Web3.IPCProvider(IPC_PATH))
    if not w3.is_connected():
        log(f"ERROR: cannot connect to {IPC_PATH}")
        return

    run_loop(w3)


if __name__ == "__main__":
    main()