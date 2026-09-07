import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Execute the shipped worker's event emission without starting its chain loop.
const source = readFileSync(new URL("../dist/server.js", import.meta.url), "utf8");
const emission = source.match(/await runtimeEvent\("aggregator\.global_model_evaluation", \{[\s\S]*?\n\s*\}\);/);
assert.ok(emission, "built worker must emit global model evaluations");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const emitEvaluation = new AsyncFunction(
    "runtimeEvent", "metrics", "globalModelRound", "currentRound", "participantCount", "count", "expected",
    emission[0],
);

async function attributes(metrics) {
    const events = [];
    await emitEvaluation((name, values) => events.push({ name, values }), metrics, 2, 1, 3, 2, 2);
    assert.equal(events.length, 1);
    assert.equal(events[0].name, "aggregator.global_model_evaluation");
    assert.equal(events[0].values.global_model_round, 2);
    return events[0].values;
}

test("round evaluations transmit micro F1 and exact match without changing their scales", async () => {
    for (const metrics of [
        { micro_f1: 0.7345, exact_match_percent: 21.5 },
        { micro_f1: 0, exact_match_percent: 0 },
    ]) {
        const values = await attributes(metrics);
        assert.equal(values.micro_f1, metrics.micro_f1);
        assert.equal(values.exact_match_percent, metrics.exact_match_percent);
    }
});

test("missing or invalid evaluation values are not reported as measured zeroes", async () => {
    for (const metrics of [
        {},
        { micro_f1: null, exact_match_percent: null },
        { micro_f1: NaN, exact_match_percent: Infinity },
    ]) {
        const values = await attributes(metrics);
        assert.equal(Object.hasOwn(values, "micro_f1"), false);
        assert.equal(Object.hasOwn(values, "exact_match_percent"), false);
    }
});
