import test from "node:test";
import assert from "node:assert/strict";
import {actionFundingConfig} from "../dist/action_funding.js";

const kms = {DOCKER: "phala", PARTICIPANT_KEY_PROVIDER: "kms"};
test("KMS deployments cannot inherit Anvil's large funding defaults", () => {
    assert.throws(() => actionFundingConfig(kms), /Explicit ACTION_KEY/);
    assert.deepEqual(actionFundingConfig({...kms, ACTION_KEY_MIN_BALANCE_WEI: "100", ACTION_KEY_TARGET_BALANCE_WEI: "200"}),
        {minimum: 100n, target: 200n});
});
test("funding limits reject invalid numbers and inverted balances", () => {
    for (const [minimum, target] of [["-1", "2"], ["1.5", "2"], ["0", "2"], ["3", "2"]]) {
        assert.throws(() => actionFundingConfig({...kms, ACTION_KEY_MIN_BALANCE_WEI: minimum, ACTION_KEY_TARGET_BALANCE_WEI: target}));
    }
    assert.equal(actionFundingConfig({}).target, 10n ** 19n);
});
