# ─────────────────────────────────────────────────────────────────────────────
# executor.py — submits fill transaction to FillerBot contract
# ─────────────────────────────────────────────────────────────────────────────
from web3 import Web3
from eth_account import Account
from config import FILLERBOT_ADDR, EXECUTOR_KEY, GAS_ESTIMATE

# FillerBot ABI — only fillOrder function needed
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


def execute_fill(w3: Web3, fill: dict) -> str | None:
    """
    Submit fillOrder() tx to FillerBot contract.

    fill dict comes from evaluator.evaluate() and contains:
        encoded_order, signature, token_in, token_out,
        amount_in, required_out, pool_fee

    Returns tx hash string on success, None on failure.
    """
    if not EXECUTOR_KEY:
        raise ValueError("EXECUTOR_PRIVATE_KEY env var not set")
    if not FILLERBOT_ADDR:
        raise ValueError("FILLERBOT_ADDRESS env var not set")

    account = Account.from_key(EXECUTOR_KEY)
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(FILLERBOT_ADDR),
        abi=FILLERBOT_ABI
    )

    # Build transaction
    try:
        gas_price = w3.eth.gas_price
    except Exception:
        gas_price = 5 * 10**9  # 5 gwei fallback

    nonce = w3.eth.get_transaction_count(account.address)

    tx = contract.functions.fillOrder(
        bytes.fromhex(fill["encoded_order"].replace("0x", "")),
        bytes.fromhex(fill["signature"].replace("0x", "")),
        Web3.to_checksum_address(fill["token_in"]),
        Web3.to_checksum_address(fill["token_out"]),
        fill["amount_in"],
        fill["required_out"],
        fill["pool_fee"],
    ).build_transaction({
        "from":     account.address,
        "gas":      GAS_ESTIMATE + 50_000,  # buffer
        "gasPrice": gas_price,
        "nonce":    nonce,
        "chainId":  1,
    })

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


def wait_for_receipt(w3: Web3, tx_hash: str, timeout: int = 60) -> dict | None:
    """Wait for tx receipt. Returns receipt or None on timeout."""
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        return receipt
    except Exception:
        return None
