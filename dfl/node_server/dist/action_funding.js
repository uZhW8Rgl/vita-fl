/** Anvil defaults must never silently become public-chain funding amounts. */
export const actionFundingConfig = (env = process.env) => {
    if (env.PARTICIPANT_KEY_PROVIDER === "broker" && env.EXPECTED_CHAIN_ID !== "31337")
        throw new Error("Broker funding is Anvil-only");
    const kms = env.DOCKER === "phala" && env.PARTICIPANT_KEY_PROVIDER === "kms";
    const minimum = env.ACTION_KEY_MIN_BALANCE_WEI || (kms ? "" : "1000000000000000000");
    const target = env.ACTION_KEY_TARGET_BALANCE_WEI || (kms ? "" : "10000000000000000000");
    if (!/^[0-9]+$/.test(minimum) || !/^[0-9]+$/.test(target)) {
        throw new Error("Explicit ACTION_KEY_MIN_BALANCE_WEI and ACTION_KEY_TARGET_BALANCE_WEI are required in KMS mode");
    }
    const result = { minimum: BigInt(minimum), target: BigInt(target) };
    if (result.minimum <= 0n || result.target < result.minimum)
        throw new Error("Invalid action-key funding limits");
    return result;
};
