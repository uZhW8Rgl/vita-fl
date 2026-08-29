import assert from 'node:assert/strict';
import test from 'node:test';

import {
    isExplicitlyFinalizedSourceRound,
} from '../dist/finalization_recovery.js';

test('UPDATING recovery accepts only a published and completed source round', () => {
    assert.equal(isExplicitlyFinalizedSourceRound({
        published: true,
        completed: true,
    }), true);
    assert.equal(isExplicitlyFinalizedSourceRound({
        published: false,
        completed: false,
    }), false);
    assert.equal(isExplicitlyFinalizedSourceRound({
        published: true,
        completed: false,
    }), false, 'an aborted round may retain publication metadata but is not finalized');
    assert.equal(isExplicitlyFinalizedSourceRound({
        published: false,
        completed: true,
    }), false);
});

test('duplicate update reconciliation rejects published metadata from an aborted round', () => {
    assert.equal(isExplicitlyFinalizedSourceRound({
        published: true,
        completed: false,
    }), false);
});
