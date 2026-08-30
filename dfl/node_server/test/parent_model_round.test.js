import assert from "node:assert/strict";
import test from "node:test";

import {
    requireAbortedAggregatorAttemptGap,
    skippedAggregatorAttemptRounds,
} from "../dist/parent_model_round.js";

test("current finalized model round is accepted without an abort gap", () => {
    assert.deepEqual(
        skippedAggregatorAttemptRounds({ modelRound: 4, currentRound: 4 }),
        [],
    );
    assert.deepEqual(
        requireAbortedAggregatorAttemptGap({
            modelRound: 4,
            currentRound: 4,
            abortedAttemptRounds: [],
        }),
        [],
    );
});

test("older finalized model is accepted only across explicitly aborted attempts", () => {
    assert.deepEqual(
        skippedAggregatorAttemptRounds({ modelRound: 1, currentRound: 4 }),
        [1, 2, 3],
    );
    assert.deepEqual(
        requireAbortedAggregatorAttemptGap({
            modelRound: 1,
            currentRound: 4,
            abortedAttemptRounds: [1, 2, 3],
        }),
        [1, 2, 3],
    );
});

test("older finalized model fails closed when an intervening attempt is unresolved", () => {
    assert.throws(
        () => requireAbortedAggregatorAttemptGap({
            modelRound: 1,
            currentRound: 4,
            abortedAttemptRounds: [1, 3],
        }),
        /round\(s\) 2 are not marked aborted/,
    );
});

test("future and invalid model rounds fail closed", () => {
    assert.throws(
        () => skippedAggregatorAttemptRounds({
            modelRound: 5,
            currentRound: 4,
        }),
        /from the future/,
    );
    for (const modelRound of [0, -1, Number.NaN, 1.5]) {
        assert.throws(
            () => skippedAggregatorAttemptRounds({
                modelRound,
                currentRound: 4,
            }),
            /positive safe integer/,
        );
    }
    for (const currentRound of [0, -1, Number.NaN, 1.5]) {
        assert.throws(
            () => skippedAggregatorAttemptRounds({
                modelRound: 1,
                currentRound,
            }),
            /positive safe integer/,
        );
    }
});
