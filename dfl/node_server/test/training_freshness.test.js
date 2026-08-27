import test from 'node:test';
import assert from 'node:assert/strict';

import { compareTrainingContexts } from '../dist/training_freshness.js';

const expected = {
    round: 7,
    state: 'TRAINING',
    aggregator: '0xA000000000000000000000000000000000000001',
    parentModelCid: 'bafy-parent',
};

test('training context remains valid when round, state, aggregator and parent are unchanged', () => {
    const current = {
        ...expected,
        aggregator: expected.aggregator.toLowerCase(),
    };
    assert.deepEqual(compareTrainingContexts(expected, current), { valid: true, reasons: [] });
});

for (const [name, replacement, reason] of [
    ['round', { round: 8 }, 'round_changed'],
    ['state', { state: 'AGGREGATING' }, 'state_changed'],
    ['aggregator', { aggregator: '0xB000000000000000000000000000000000000001' }, 'aggregator_changed'],
    ['parent model', { parentModelCid: 'bafy-new-parent' }, 'parent_changed'],
]) {
    test(`training context rejects a changed ${name}`, () => {
        const comparison = compareTrainingContexts(expected, { ...expected, ...replacement });
        assert.equal(comparison.valid, false);
        assert.ok(comparison.reasons.includes(reason));
    });
}

test('training context fails closed for malformed round and empty identifiers', () => {
    const comparison = compareTrainingContexts(expected, {
        round: Number.NaN,
        state: 'TRAINING',
        aggregator: '',
        parentModelCid: '',
    });
    assert.equal(comparison.valid, false);
    assert.deepEqual(
        new Set(comparison.reasons),
        new Set(['round_changed', 'aggregator_changed', 'parent_changed']),
    );
});
