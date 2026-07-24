import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
    deriveTimingConfig,
    gmUpdateWaitBudgetMs,
    nextGMTimeoutState,
    nextAggregatorTimeoutTracker,
    recommendedGMUpdateBudgetMs,
    requiredTimeoutReports,
    selectionGapRecoveryNeeded,
    shouldStartAggregation,
    validateTimingConfig,
} from '../dist/state_timing.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const dockerEnvPath = path.resolve(__dirname, '../../../.env');
const hasDockerEnv = fs.existsSync(dockerEnvPath);

function readEnvFile(filePath) {
    const env = {};
    const content = fs.readFileSync(filePath, 'utf8');

    for (const line of content.split(/\r?\n/)) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#')) continue;

        const equalsAt = trimmed.indexOf('=');
        if (equalsAt === -1) continue;

        const key = trimmed.slice(0, equalsAt).trim();
        const value = trimmed.slice(equalsAt + 1).trim();
        env[key] = value.replace(/^['"]|['"]$/g, '');
    }

    return env;
}

test('deriveTimingConfig uses safe defaults for invalid env values', () => {
    const config = deriveTimingConfig({
        MODEL_SUBMISSION_DEADLINE_MS: 'bad',
        GM_UPDATE_TIMEOUT_MS: '-1',
        GM_UPDATE_TIMEOUT_LOOPS: '0',
    });

    assert.equal(config.modelSubmissionDeadlineMs, 60000);
    assert.equal(config.gmUpdateTimeoutMs, 30000);
    assert.equal(config.gmUpdateTimeoutLoops, 3);
    assert.equal(config.aggregationUpdateEstimateMs, 30000);
    assert.equal(config.modelTransferTimeoutMs, 20000);
    assert.equal(config.modelTransferRetryDelayMs, 5000);
});

test('deriveTimingConfig accepts integer timing values from env', () => {
    const config = deriveTimingConfig({
        MODEL_SUBMISSION_DEADLINE_MS: '20000',
        GM_UPDATE_TIMEOUT_MS: '10000',
        GM_UPDATE_TIMEOUT_LOOPS: '3',
        AGGREGATION_UPDATE_ESTIMATE_MS: '15000',
        GM_UPDATE_POLL_MS: '5000',
        MODEL_TRANSFER_TIMEOUT_MS: '12000',
        MODEL_TRANSFER_RETRY_DELAY_MS: '3000',
    });

    assert.equal(config.modelSubmissionDeadlineMs, 20000);
    assert.equal(config.gmUpdateTimeoutMs, 10000);
    assert.equal(config.gmUpdateTimeoutLoops, 3);
    assert.equal(config.aggregationUpdateEstimateMs, 15000);
    assert.equal(config.modelTransferTimeoutMs, 12000);
    assert.equal(config.modelTransferRetryDelayMs, 3000);
    assert.equal(gmUpdateWaitBudgetMs(config), 30000);
    assert.equal(recommendedGMUpdateBudgetMs(config), 40000);
});

test('validateTimingConfig flags a worker failover window that is too short', () => {
    const config = deriveTimingConfig({
        MODEL_SUBMISSION_DEADLINE_MS: '20000',
        GM_UPDATE_TIMEOUT_MS: '10000',
        GM_UPDATE_TIMEOUT_LOOPS: '3',
        AGGREGATION_UPDATE_ESTIMATE_MS: '15000',
        GM_UPDATE_POLL_MS: '5000',
    });

    const warnings = validateTimingConfig(config);

    assert.equal(warnings.length, 2);
    assert.match(warnings[0], /GM update wait budget is 30000ms/);
    assert.match(warnings[1], /GM_UPDATE_TIMEOUT_MS \(10000ms\) is lower/);
});

test('validateTimingConfig accepts a wider timing budget', () => {
    const config = deriveTimingConfig({
        MODEL_SUBMISSION_DEADLINE_MS: '60000',
        GM_UPDATE_TIMEOUT_MS: '30000',
        GM_UPDATE_TIMEOUT_LOOPS: '4',
        AGGREGATION_UPDATE_ESTIMATE_MS: '30000',
        GM_UPDATE_POLL_MS: '5000',
    });

    assert.deepEqual(validateTimingConfig(config), []);
});

test(
    'docker .env timing profile has enough worker failover budget',
    { skip: !hasDockerEnv },
    () => {
    const config = deriveTimingConfig(readEnvFile(dockerEnvPath));
    const warnings = validateTimingConfig(config);

    assert.deepEqual(
        warnings,
        [],
        `.env timing is too tight:\n${warnings.join('\n')}`,
    );
    assert.ok(
        gmUpdateWaitBudgetMs(config) >= recommendedGMUpdateBudgetMs(config),
        'GM update wait budget should cover model submission deadline, aggregation/update estimate and one poll interval.',
    );
    },
);

test('shouldStartAggregation waits until all models arrive or the deadline expires', () => {
    assert.equal(shouldStartAggregation({
        expectedModels: 2,
        presentModels: 1,
        elapsedMs: 19999,
        deadlineMs: 20000,
    }), false);

    assert.equal(shouldStartAggregation({
        expectedModels: 2,
        presentModels: 2,
        elapsedMs: 5000,
        deadlineMs: 20000,
    }), true);

    assert.equal(shouldStartAggregation({
        expectedModels: 2,
        presentModels: 1,
        elapsedMs: 20000,
        deadlineMs: 20000,
    }), true);
});

test('nextGMTimeoutState reports only after the configured number of missed loops', () => {
    assert.deepEqual(nextGMTimeoutState({ missedLoops: 0, maxLoops: 3 }), {
        missedLoops: 1,
        shouldReportTimeout: false,
    });
    assert.deepEqual(nextGMTimeoutState({ missedLoops: 2, maxLoops: 3 }), {
        missedLoops: 0,
        shouldReportTimeout: true,
    });
});

test('timeout tracking preserves the observed round and aggregator instead of retargeting a successor', () => {
    const aggregatorA = '0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
    const aggregatorB = '0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
    let tracker = {};

    let result = nextAggregatorTimeoutTracker({
        tracker,
        expectedRound: 4,
        expectedAggregator: aggregatorA,
        maxLoops: 3,
    });
    tracker = result;
    result = nextAggregatorTimeoutTracker({
        tracker,
        expectedRound: 4,
        expectedAggregator: aggregatorA,
        maxLoops: 3,
    });
    tracker = result;
    result = nextAggregatorTimeoutTracker({
        tracker,
        expectedRound: 4,
        expectedAggregator: aggregatorA,
        maxLoops: 3,
    });

    assert.equal(result.shouldReportTimeout, true);
    assert.equal(result.expectedRound, 4);
    assert.equal(result.expectedAggregator, aggregatorA);

    const successorResult = nextAggregatorTimeoutTracker({
        tracker: result,
        expectedRound: 5,
        expectedAggregator: aggregatorB,
        maxLoops: 3,
    });
    assert.equal(successorResult.failureCount, 1);
    assert.equal(successorResult.shouldReportTimeout, false);
    assert.equal(successorResult.expectedAggregator, aggregatorB);
});

test('model-transfer failures use the same bounded timeout progression as missing model updates', () => {
    const aggregator = '0xcccccccccccccccccccccccccccccccccccccccc';
    const first = nextAggregatorTimeoutTracker({
        tracker: {},
        expectedRound: 2,
        expectedAggregator: aggregator,
        maxLoops: 2,
    });
    const second = nextAggregatorTimeoutTracker({
        tracker: first,
        expectedRound: 2,
        expectedAggregator: aggregator,
        maxLoops: 2,
    });

    assert.equal(first.shouldReportTimeout, false);
    assert.equal(second.shouldReportTimeout, true);
    assert.equal(second.expectedRound, 2);
    assert.equal(second.expectedAggregator, aggregator);
});

test('requiredTimeoutReports mirrors the on-chain percentage threshold', () => {
    assert.equal(requiredTimeoutReports({ eligibleReporters: 2, thresholdPercent: 50 }), 1);
    assert.equal(requiredTimeoutReports({ eligibleReporters: 2, thresholdPercent: 51 }), 2);
    assert.equal(requiredTimeoutReports({ eligibleReporters: 3, thresholdPercent: 50 }), 2);
    assert.equal(requiredTimeoutReports({ eligibleReporters: 0, thresholdPercent: 50 }), 0);
});

test('selection-gap recovery is attempted only after a completed round advanced beyond its selection marker', () => {
    assert.equal(selectionGapRecoveryNeeded({
        state: 'UPDATING',
        observedRound: 4,
        lastSelectionRound: 3,
        completedRounds: 4,
        targetRounds: 5,
    }), true);
    assert.equal(selectionGapRecoveryNeeded({
        state: 'UPDATING',
        observedRound: 4,
        lastSelectionRound: 4,
        completedRounds: 4,
        targetRounds: 5,
    }), false);
    assert.equal(selectionGapRecoveryNeeded({
        state: 'AGGREGATING',
        observedRound: 4,
        lastSelectionRound: 3,
        completedRounds: 4,
        targetRounds: 5,
    }), false);
    assert.equal(selectionGapRecoveryNeeded({
        state: 'UPDATING',
        observedRound: 5,
        lastSelectionRound: 4,
        completedRounds: 5,
        targetRounds: 5,
    }), false);
});
