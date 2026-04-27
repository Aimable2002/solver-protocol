// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import "../src/FillerBot.sol";

/// @notice Deploys FillerBot to Ethereum Mainnet.
///
/// BEFORE RUNNING:
/// ════════════════════════════════════════════════════════════════
/// 1. Set environment variables:
///
///    export EXECUTOR_ADDRESS=0xYourHotWalletAddress
///    export PRIVATE_KEY=0xYourDeployerPrivateKey
///    export MAINNET_RPC=http://localhost:8545   (your local node)
///
/// 2. Make sure deployer wallet has ETH for gas (~0.01 ETH enough)
///
/// RUN:
/// ════════════════════════════════════════════════════════════════
///    forge script script/Deploy.s.sol \
///      --rpc-url $MAINNET_RPC \
///      --broadcast \
///      --verify \
///      -vvvv
///
/// DRY RUN (no broadcast, no cost):
/// ════════════════════════════════════════════════════════════════
///    forge script script/Deploy.s.sol \
///      --rpc-url $MAINNET_RPC \
///      -vvvv
///
/// AFTER DEPLOYMENT:
/// ════════════════════════════════════════════════════════════════
/// Save the deployed contract address printed in the output.
/// Fund the executor wallet with ETH for gas.
/// Start the bot pointing at the deployed contract address.

contract Deploy is Script {

    // ── Mainnet addresses — do not change ─────────────────────────────────────
    address constant DUTCH_REACTOR = 0x00000011F84B9aa48e5f8aA8B9897600006289Be;
    address constant SWAP_ROUTER   = 0xE592427A0AEce92De3Edee1F18E0157C05861564;

    function run() external {
        // Load from environment
        address executor   = vm.envAddress("EXECUTOR_ADDRESS");
        uint256 privateKey = vm.envUint("PRIVATE_KEY");

        // Validate before spending gas
        require(executor != address(0), "EXECUTOR_ADDRESS not set");
        require(privateKey != 0,        "PRIVATE_KEY not set");

        console.log("Deploying FillerBot...");
        console.log("  Deployer  :", vm.addr(privateKey));
        console.log("  Executor  :", executor);
        console.log("  Reactor   :", DUTCH_REACTOR);
        console.log("  SwapRouter:", SWAP_ROUTER);
        console.log("  Network   : Ethereum Mainnet");

        vm.startBroadcast(privateKey);

        FillerBot bot = new FillerBot(executor);

        vm.stopBroadcast();

        console.log("");
        console.log("FillerBot deployed:");
        console.log("  Address   :", address(bot));
        console.log("  Owner     :", bot.owner());
        console.log("  Executor  :", bot.executor());
        console.log("");
        console.log("NEXT STEPS:");
        console.log("  1. Save contract address:", address(bot));
        console.log("  2. Fund executor wallet with ETH for gas");
        console.log("  3. Set FILLERBOT_ADDRESS env var in your bot");
        console.log("  4. Run bot pointing at this contract");
        console.log("");
        console.log("OPTIONAL — set minimum profit threshold:");
        console.log("  bot.setMinProfit(500000000000000)");
        console.log("  (= 0.0005 ETH ~ $1.50 at $3000/ETH)");
        console.log("  Keep at 0 during initial testing.");
    }
}
