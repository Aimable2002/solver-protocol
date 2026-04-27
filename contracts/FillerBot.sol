// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./interfaces/ISwapRouter.sol";
import "./interfaces/IUniswapXReactor.sol";
import "./lib/OrderLib.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @title FillerBot
/// @notice Fills UniswapX Dutch V2 orders using Uniswap V3 as the exit pool.
///
/// HOW IT WORKS (read this before touching anything):
/// ═══════════════════════════════════════════════════════════════
///
/// 1. User signs an order off-chain:
///    "I want to sell 1 WETH, I want at least 3000 USDC back"
///    This sits in the UniswapX API. Nothing has moved yet.
///
/// 2. Our bot sees the order, decides it is profitable, calls fillOrder().
///    The executor wallet pays gas. That is ALL the wallet needs.
///
/// 3. fillOrder() calls reactor.executeWithCallback().
///
/// 4. The reactor pulls tokenIn (1 WETH) from the USER's wallet
///    using Permit2 and transfers it to THIS CONTRACT.
///    This is safe for the reactor because everything is atomic —
///    if we fail to return tokenOut the whole tx reverts.
///
/// 5. The reactor calls reactorCallback() on this contract.
///    Now we hold 1 WETH.
///
/// 6. Inside reactorCallback():
///    a. We swap 1 WETH → USDC on Uniswap V3.
///       Say V3 gives us 3020 USDC.
///    b. We approve the reactor to pull 3000 USDC (user's minimum).
///    c. We return. Reactor pulls 3000 USDC and sends to user.
///
/// 7. 20 USDC surplus stays in this contract as profit.
///    Owner calls withdraw() periodically to collect it.
///
/// WHY NO FLASH LOAN:
/// ═══════════════════════════════════════════════════════════════
/// The reactor funds us with tokenIn in step 4.
/// We already have the capital we need before the swap.
/// Flash loans are for protocols where you must source capital
/// yourself with no prior transfer. That is not the case here.
///
/// WHAT EXECUTOR WALLET NEEDS:
/// ═══════════════════════════════════════════════════════════════
/// Only ETH for gas (~$1-3 per fill on Mainnet). No token balance.
///
/// PROFIT COLLECTION:
/// ═══════════════════════════════════════════════════════════════
/// Profit accumulates in this contract as tokenOut surplus.
/// Owner calls withdrawAll(tokenAddress) to collect.

contract FillerBot is IReactorCallback, Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    // ── Addresses ─────────────────────────────────────────────────────────────

    /// @notice UniswapX Dutch V2 Reactor — Ethereum Mainnet
    address public constant DUTCH_REACTOR =
        0x00000011F84B9aa48e5f8aA8B9897600006289Be;

    /// @notice Uniswap V3 SwapRouter — Ethereum Mainnet
    address public constant SWAP_ROUTER =
        0xE592427A0AEce92De3Edee1F18E0157C05861564;

    // ── State ──────────────────────────────────────────────────────────────────

    /// @notice Hot wallet that calls fillOrder() — needs ETH for gas only
    address public executor;

    /// @notice Minimum surplus in raw output token units to accept a fill.
    ///         Prevents filling orders where profit does not cover gas cost.
    ///         Set via setMinProfit(). Start with 0 for testing, tune after.
    uint256 public minProfitRaw;

    // ── Fill context ───────────────────────────────────────────────────────────
    // Written by fillOrder(), read by reactorCallback().
    // Deleted after each fill. Never persists between fills.

    struct FillContext {
        address tokenIn;     // token reactor sends us (user is selling this)
        address tokenOut;    // token user wants to receive (we swap into this)
        uint256 amountIn;    // amount of tokenIn reactor will send us
        uint256 requiredOut; // minimum tokenOut user will accept
        uint24  poolFee;     // V3 fee tier: 500, 3000, or 10000
        bytes32 orderHash;   // for event logging
    }

    FillContext private _ctx;

    // ── Events ─────────────────────────────────────────────────────────────────

    event OrderFilled(
        bytes32 indexed orderHash,
        address indexed tokenIn,
        address indexed tokenOut,
        uint256 amountIn,
        uint256 amountOut,
        uint256 profit
    );
    event ExecutorUpdated(address indexed oldExecutor, address indexed newExecutor);
    event MinProfitUpdated(uint256 oldMin, uint256 newMin);
    event Withdrawn(address indexed token, uint256 amount);

    // ── Errors ─────────────────────────────────────────────────────────────────

    error NotExecutor();
    error NotReactor();
    error NotProfitable();
    error SwapFailed();
    error InvalidOrder();

    // ── Constructor ────────────────────────────────────────────────────────────

    /// @param _executor  Your bot's hot wallet address (needs ETH for gas only)
    constructor(address _executor) Ownable(msg.sender) {
        require(_executor != address(0), "zero executor");
        executor = _executor;
    }

    // ── Modifier ───────────────────────────────────────────────────────────────

    modifier onlyExecutor() {
        if (msg.sender != executor && msg.sender != owner()) revert NotExecutor();
        _;
    }

    // ── Entry point ────────────────────────────────────────────────────────────

    /// @notice Called by the bot to fill a UniswapX Dutch V2 order.
    ///
    /// @param encodedOrder  Raw order bytes from UniswapX API field "encodedOrder"
    /// @param sig           Swapper signature from UniswapX API field "signature"
    /// @param tokenIn       Token the user is selling (bot reads from order)
    /// @param tokenOut      Token the user wants to receive (bot reads from order)
    /// @param amountIn      Current decayed input amount (bot calculates off-chain)
    /// @param requiredOut   Current decayed minimum output (bot calculates off-chain)
    /// @param poolFee       Best V3 fee tier bot found: 500, 3000, or 10000
    function fillOrder(
        bytes   calldata encodedOrder,
        bytes   calldata sig,
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 requiredOut,
        uint24  poolFee
    ) external nonReentrant onlyExecutor {
        if (tokenIn     == address(0)) revert InvalidOrder();
        if (tokenOut    == address(0)) revert InvalidOrder();
        if (amountIn    == 0)          revert InvalidOrder();
        if (requiredOut == 0)          revert InvalidOrder();

        // Store context so reactorCallback can read it
        _ctx = FillContext({
            tokenIn:     tokenIn,
            tokenOut:    tokenOut,
            amountIn:    amountIn,
            requiredOut: requiredOut,
            poolFee:     poolFee,
            orderHash:   keccak256(encodedOrder)
        });

        // Hand off to reactor — reactor will call reactorCallback() on us
        IUniswapXReactor(DUTCH_REACTOR).executeWithCallback(
            SignedOrder({ order: encodedOrder, sig: sig }),
            ""  // no extra callbackData needed — context is in _ctx
        );

        // Clean up context
        delete _ctx;
    }

    // ── Reactor callback ───────────────────────────────────────────────────────

    /// @notice Called by the reactor after it transfers tokenIn to this contract.
    ///
    ///         At this point this contract holds amountIn of tokenIn.
    ///         We must:
    ///           1. Swap tokenIn → tokenOut on V3
    ///           2. Approve reactor to pull requiredOut of tokenOut
    ///         The reactor then pulls tokenOut and delivers to user.
    ///         Surplus tokenOut stays here as profit.
    function reactorCallback(
        ResolvedOrder[] calldata, /* resolvedOrders — not used, we use _ctx */
        bytes calldata            /* callbackData   — not used */
    ) external override {
        if (msg.sender != DUTCH_REACTOR) revert NotReactor();

        FillContext memory ctx = _ctx;

        // Step 1: approve V3 router to spend tokenIn
        IERC20(ctx.tokenIn).forceApprove(SWAP_ROUTER, ctx.amountIn);

        // Step 2: swap tokenIn → tokenOut on V3
        uint256 amountOut = ISwapRouter(SWAP_ROUTER).exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn:           ctx.tokenIn,
                tokenOut:          ctx.tokenOut,
                fee:               ctx.poolFee,
                recipient:         address(this),
                deadline:          block.timestamp,
                amountIn:          ctx.amountIn,
                amountOutMinimum:  ctx.requiredOut, // revert if V3 cannot meet user minimum
                sqrtPriceLimitX96: 0
            })
        );

        // Step 3: verify swap result
        if (amountOut < ctx.requiredOut) revert SwapFailed();

        // Step 4: check profit meets our minimum threshold
        uint256 profit = amountOut - ctx.requiredOut;
        if (profit < minProfitRaw) revert NotProfitable();

        // Step 5: approve reactor to pull exactly requiredOut
        // The surplus (profit) stays in this contract
        IERC20(ctx.tokenOut).forceApprove(DUTCH_REACTOR, ctx.requiredOut);

        emit OrderFilled(
            ctx.orderHash,
            ctx.tokenIn,
            ctx.tokenOut,
            ctx.amountIn,
            amountOut,
            profit
        );
    }

    // ── Admin ──────────────────────────────────────────────────────────────────

    /// @notice Withdraw profit to owner wallet
    function withdraw(address token, uint256 amount) external onlyOwner {
        if (token == address(0)) {
            payable(owner()).transfer(amount);
        } else {
            IERC20(token).safeTransfer(owner(), amount);
        }
        emit Withdrawn(token, amount);
    }

    /// @notice Withdraw full balance of a token
    function withdrawAll(address token) external onlyOwner {
        uint256 bal = token == address(0)
            ? address(this).balance
            : IERC20(token).balanceOf(address(this));
        withdraw(token, bal);
    }

    /// @notice Update executor wallet
    function setExecutor(address _executor) external onlyOwner {
        require(_executor != address(0), "zero address");
        emit ExecutorUpdated(executor, _executor);
        executor = _executor;
    }

    /// @notice Set minimum profit threshold in raw output token units.
    ///         Start with 0 during testing. Once live, set to cover gas cost.
    ///         Example: WETH output, gas costs $1.50, ETH=$3000
    ///         → minProfitRaw = 0.0005 ETH = 500000000000000
    function setMinProfit(uint256 _minProfitRaw) external onlyOwner {
        emit MinProfitUpdated(minProfitRaw, _minProfitRaw);
        minProfitRaw = _minProfitRaw;
    }

    /// @notice Emergency token rescue
    function rescue(address token) external onlyOwner {
        withdrawAll(token);
    }

    receive() external payable {}
}
