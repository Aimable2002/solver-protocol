// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

library OrderLib {
    struct DutchOutput {
        address token;
        uint256 startAmount;
        uint256 endAmount;
        address recipient;
    }

    struct DutchInput {
        address token;
        uint256 startAmount;
        uint256 endAmount;
    }

    /// @notice Calculate the current decayed output amount at this block.
    ///         This is the minimum tokenOut we must deliver to the user.
    ///         The gap between what V3 gives us and this number is our profit.
    function currentOutput(
        uint256 startAmount,
        uint256 endAmount,
        uint256 decayStartTime,
        uint256 decayEndTime
    ) internal view returns (uint256) {
        if (block.timestamp <= decayStartTime) return startAmount;
        if (block.timestamp >= decayEndTime)   return endAmount;
        uint256 elapsed  = block.timestamp - decayStartTime;
        uint256 duration = decayEndTime - decayStartTime;
        uint256 decay    = (startAmount - endAmount) * elapsed / duration;
        return startAmount - decay;
    }
}
