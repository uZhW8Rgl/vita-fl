import test from "node:test";
import assert from "node:assert/strict";

import {
    formatDecimalUnits,
    gweiRateToWei,
    parseDecimalRate,
    transactionCostAmounts,
} from "../dist/transaction_cost.js";

test("transaction costs retain exact wei and decimal values", () => {
    const amounts = transactionCostAmounts(
        21_000n,
        1_234_567_890n,
        parseDecimalRate("3000.25", "ETH_EUR_PRICE"),
        parseDecimalRate("3500.5", "ETH_USD_PRICE"),
    );

    assert.deepEqual(amounts, {
        costWei: "25925925690000",
        costGwei: "25925.92569",
        costEth: "0.00002592592569",
        costEur: "0.0777842585514225",
        costUsd: "0.090753702877845",
        effectiveGasPriceGwei: "1.23456789",
    });
});

test("large integer values are never rounded through JavaScript Number", () => {
    assert.equal(
        formatDecimalUnits(123456789012345678901234567890n, 18),
        "123456789012.34567890123456789",
    );
});

test("exchange rates reject exponent notation and signed values", () => {
    assert.throws(() => parseDecimalRate("3e3", "ETH_EUR_PRICE"), /non-negative decimal/);
    assert.throws(() => parseDecimalRate("-1", "ETH_EUR_PRICE"), /non-negative decimal/);
});

test("a fractional Gwei reference price converts exactly to integer wei", () => {
    assert.equal(
        gweiRateToWei(parseDecimalRate("0.9291", "REFERENCE_MAINNET_GAS_PRICE_GWEI")),
        929_100_000n,
    );
    assert.throws(
        () => gweiRateToWei(parseDecimalRate("0.1234567891", "REFERENCE_MAINNET_GAS_PRICE_GWEI")),
        /at most nine decimal places/,
    );
});
