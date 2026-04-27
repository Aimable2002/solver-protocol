// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

struct OrderInfo {
    address reactor;
    address swapper;
    uint256 nonce;
    uint256 deadline;
    address additionalValidationContract;
    bytes   additionalValidationData;
}

struct InputToken {
    address token;
    uint256 amount;
    uint256 maxAmount;
}

struct OutputToken {
    address token;
    uint256 amount;
    address recipient;
}

struct ResolvedOrder {
    OrderInfo     info;
    InputToken    input;
    OutputToken[] outputs;
    bytes         sig;
    bytes32       hash;
}

struct SignedOrder {
    bytes order;
    bytes sig;
}

/// @notice Must be implemented by all filler contracts
/// @dev The reactor transfers tokenIn to the filler, then calls this.
///      The filler must swap tokenIn → tokenOut and approve tokenOut
///      to the reactor before returning. The reactor then pulls tokenOut
///      and delivers it to the user. Everything is atomic.
interface IReactorCallback {
    function reactorCallback(
        ResolvedOrder[] calldata resolvedOrders,
        bytes calldata callbackData
    ) external;
}

interface IUniswapXReactor {
    /// @notice Fill using callback — reactor sends tokenIn to filler,
    ///         filler must produce and approve tokenOut inside reactorCallback
    function executeWithCallback(
        SignedOrder calldata order,
        bytes calldata callbackData
    ) external payable;

    function executeBatchWithCallback(
        SignedOrder[] calldata orders,
        bytes calldata callbackData
    ) external payable;
}
