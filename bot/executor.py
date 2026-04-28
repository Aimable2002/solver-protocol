# ─────────────────────────────────────────────────────────────────────────────
# executor.py — submits fill transaction to FillerBot contract
# ─────────────────────────────────────────────────────────────────────────────
from web3 import Web3
from eth_account import Account
from config import FILLERBOT_ADDR, EXECUTOR_KEY

FILLERBOT_ABI = [
    {
        "inputs": [
            {"name": "encodedOrder", "type": "bytes"},
            {"name": "sig",          "type": "bytes"},
            {"name": "tokenIn",      "type": "address"},
            {"name": "tokenOut",     "type": "address"},
            {"name": "amountIn",     "type": "uint256"},
            {"name": "requiredOut",  "type": "uint256"},
            {"name": "poolFee",      "type": "uint24"},
        ],
        "name": "fillOrder",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "token",  "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "token", "type": "address"}],
        "name": "withdrawAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


def execute_fill(w3: Web3, fill: dict) -> str:
    """
    Submit fillOrder() tx to FillerBot contract.
    Uses eth_estimateGas for exact gas limit.
    Uses EIP-1559 fee model (maxFeePerGas + maxPriorityFeePerGas).
    Raises on any failure — caller handles logging.
    """
    if not EXECUTOR_KEY:
        raise ValueError("EXECUTOR_PRIVATE_KEY not set")
    if not FILLERBOT_ADDR:
        raise ValueError("FILLERBOT_ADDRESS not set")

    account  = Account.from_key(EXECUTOR_KEY)
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(FILLERBOT_ADDR),
        abi=FILLERBOT_ABI
    )

    # Build call args
    call_args = (
        bytes.fromhex(fill["encoded_order"].replace("0x", "")),
        bytes.fromhex(fill["signature"].replace("0x", "")),
        Web3.to_checksum_address(fill["token_in"]),
        Web3.to_checksum_address(fill["token_out"]),
        fill["amount_in"],
        fill["required_out"],
        fill["pool_fee"],
    )

    # ── EIP-1559 fee model ────────────────────────────────────────────────────
    # Get latest block to read baseFee
    latest       = w3.eth.get_block("latest")
    base_fee     = latest["baseFeePerGas"]           # current base fee in wei

    # Priority fee (tip) — 1 gwei is enough for a filler bot, adjust if needed
    priority_fee = w3.eth.max_priority_fee            # node's suggested tip

    # maxFeePerGas = 2x baseFee + tip (covers 1 full base fee doubling)
    max_fee      = (2 * base_fee) + priority_fee

    # ── Exact gas estimate from node ──────────────────────────────────────────
    gas_limit = contract.functions.fillOrder(*call_args).estimate_gas({
        "from": account.address,
    })

    nonce = w3.eth.get_transaction_count(account.address)

    tx = contract.functions.fillOrder(*call_args).build_transaction({
        "from":                 account.address,
        "gas":                  gas_limit,
        "maxFeePerGas":         max_fee,
        "maxPriorityFeePerGas": priority_fee,
        "nonce":                nonce,
        "chainId":              1,
    })

    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


def wait_for_receipt(w3: Web3, tx_hash: str, timeout: int = 60) -> dict | None:
    """Wait for tx receipt. Returns receipt or None on timeout."""
    try:
        return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    except Exception:
        return None