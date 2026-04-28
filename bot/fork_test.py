# ─────────────────────────────────────────────────────────────────────────────
# fork_test.py — continuous fork harness
#
# USAGE:
#   # Start anvil fork first:
#   anvil --fork-url /bsc/reth/reth.ipc --fork-block-number 24974000 --port 8547
#
#   # Then run:
#   export FILLERBOT_ADDRESS=0x659E0981563DF0fE69603c5555Adda9547C36781
#   export EXECUTOR_PRIVATE_KEY=0x...
#   python3 fork_test.py
#
# WHAT THIS FILE DOES:
#   1. Connects Web3 to the anvil fork instead of the IPC node
#   2. Funds the executor wallet on the fork via anvil_setBalance
#   3. Calls monitor.run_loop() — the exact same loop as production
#
# WHAT THIS FILE DOES NOT DO:
#   - No evaluation logic (that's evaluator.py)
#   - No order fetching logic (that's monitor.py)
#   - No execution logic (that's executor.py)
#   The only fork-specific thing here is the Web3 connection and the
#   anvil_setBalance call. Everything else is the live production code.
# ─────────────────────────────────────────────────────────────────────────────
from web3 import Web3
from eth_account import Account
from config  import EXECUTOR_KEY, FILLERBOT_ADDR
from monitor import run_loop, log

FORK_RPC = "http://127.0.0.1:8547"


def fund_executor(w3: Web3, address: str):
    """Give executor 10 ETH on the fork via anvil_setBalance."""
    w3.provider.make_request("anvil_setBalance", [address, hex(10 * 10**18)])
    bal = w3.eth.get_balance(address)
    log(f"Executor funded: {bal / 1e18:.2f} ETH")


def main():
    if not EXECUTOR_KEY:
        log("ERROR: EXECUTOR_PRIVATE_KEY not set")
        return
    if not FILLERBOT_ADDR:
        log("ERROR: FILLERBOT_ADDRESS not set")
        return

    w3 = Web3(Web3.HTTPProvider(FORK_RPC))
    if not w3.is_connected():
        log(f"ERROR: cannot connect to fork at {FORK_RPC}")
        log("Start anvil: anvil --fork-url /bsc/reth/reth.ipc --port 8547")
        return

    log("=" * 55)
    log("UniswapX FillerBot — FORK MODE")
    log(f"  Fork RPC:  {FORK_RPC}")
    log(f"  Block:     {w3.eth.block_number:,}")
    log(f"  Contract:  {FILLERBOT_ADDR}")
    log("=" * 55)

    account = Account.from_key(EXECUTOR_KEY)
    log(f"Executor: {account.address}")
    fund_executor(w3, account.address)

    # Run the exact same production loop — verbose=True so every order shows full math
    run_loop(w3, verbose=True)


if __name__ == "__main__":
    main()