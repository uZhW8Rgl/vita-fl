// @ts-nocheck
const DEFAULT_MODEL_SUBMISSION_DEADLINE_MS = 60 * 1000;
const DEFAULT_GM_UPDATE_TIMEOUT_MS = 30 * 1000;
const DEFAULT_GM_UPDATE_TIMEOUT_LOOPS = 3;
const DEFAULT_AGGREGATION_UPDATE_ESTIMATE_MS = 30 * 1000;
const DEFAULT_GM_UPDATE_POLL_MS = 5 * 1000;
const DEFAULT_MODEL_TRANSFER_TIMEOUT_MS = 20 * 1000;
const DEFAULT_MODEL_TRANSFER_RETRY_DELAY_MS = 5 * 1000;
function parsePositiveInteger(value, fallback) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed) || parsed <= 0)
        return fallback;
    return Math.floor(parsed);
}
export function deriveTimingConfig(env = {}) {
    return {
        modelSubmissionDeadlineMs: parsePositiveInteger(env.MODEL_SUBMISSION_DEADLINE_MS, DEFAULT_MODEL_SUBMISSION_DEADLINE_MS),
        gmUpdateTimeoutMs: parsePositiveInteger(env.GM_UPDATE_TIMEOUT_MS, DEFAULT_GM_UPDATE_TIMEOUT_MS),
        gmUpdateTimeoutLoops: parsePositiveInteger(env.GM_UPDATE_TIMEOUT_LOOPS, DEFAULT_GM_UPDATE_TIMEOUT_LOOPS),
        aggregationUpdateEstimateMs: parsePositiveInteger(env.AGGREGATION_UPDATE_ESTIMATE_MS, DEFAULT_AGGREGATION_UPDATE_ESTIMATE_MS),
        gmUpdatePollMs: parsePositiveInteger(env.GM_UPDATE_POLL_MS, DEFAULT_GM_UPDATE_POLL_MS),
        modelTransferTimeoutMs: parsePositiveInteger(env.MODEL_TRANSFER_TIMEOUT_MS, DEFAULT_MODEL_TRANSFER_TIMEOUT_MS),
        modelTransferRetryDelayMs: parsePositiveInteger(env.MODEL_TRANSFER_RETRY_DELAY_MS, DEFAULT_MODEL_TRANSFER_RETRY_DELAY_MS),
    };
}
export function gmUpdateWaitBudgetMs(config) {
    return config.gmUpdateTimeoutMs * config.gmUpdateTimeoutLoops;
}
export function recommendedGMUpdateBudgetMs(config) {
    return config.modelSubmissionDeadlineMs
        + config.aggregationUpdateEstimateMs
        + config.gmUpdatePollMs;
}
export function validateTimingConfig(config) {
    const warnings = [];
    const waitBudget = gmUpdateWaitBudgetMs(config);
    const recommendedBudget = recommendedGMUpdateBudgetMs(config);
    if (waitBudget < recommendedBudget) {
        warnings.push(`GM update wait budget is ${waitBudget}ms, but at least ${recommendedBudget}ms is recommended (` +
            `MODEL_SUBMISSION_DEADLINE_MS + AGGREGATION_UPDATE_ESTIMATE_MS + GM_UPDATE_POLL_MS).`);
    }
    if (config.gmUpdateTimeoutMs < config.aggregationUpdateEstimateMs) {
        warnings.push(`GM_UPDATE_TIMEOUT_MS (${config.gmUpdateTimeoutMs}ms) is lower than ` +
            `AGGREGATION_UPDATE_ESTIMATE_MS (${config.aggregationUpdateEstimateMs}ms).`);
    }
    if (config.modelSubmissionDeadlineMs < config.gmUpdatePollMs) {
        warnings.push(`MODEL_SUBMISSION_DEADLINE_MS (${config.modelSubmissionDeadlineMs}ms) is lower than ` +
            `GM_UPDATE_POLL_MS (${config.gmUpdatePollMs}ms).`);
    }
    return warnings;
}
export function shouldStartAggregation({ expectedModels, presentModels, elapsedMs, deadlineMs, deadlineExpired = elapsedMs >= deadlineMs, }) {
    // Only the bootstrap may close an empty input set. The client target is
    // an early-start trigger; a timed-out training round still needs input.
    if (expectedModels === 0)
        return true;
    if (presentModels <= 0)
        return false;
    return presentModels >= expectedModels || deadlineExpired;
}
export function canCloseRoundInputs(policy, presentModels, nowMs = Date.now()) {
    if (!policy.opened || policy.closed)
        return false;
    // Every accepted commitment must have its authenticated local model file.
    if (presentModels < policy.acceptedSubmissions)
        return false;
    return shouldStartAggregation({
        expectedModels: policy.requiredSubmissions,
        presentModels: policy.acceptedSubmissions,
        // The admitted aggregator owns the timer. A stalled latest block
        // must not prevent a locally expired window from being processed.
        deadlineExpired: nowMs >= policy.deadline * 1000,
    });
}
export async function waitForRoundInputs({ round, readPolicy, countModels, pollMs = 2000, nowMs = () => Date.now(), sleep = ms => new Promise(resolve => setTimeout(resolve, ms)), }) {
    while (true) {
        const [present, policy] = await Promise.all([countModels(), readPolicy(round)]);
        if (!policy.opened)
            throw new Error(`Round ${round} submission window is not open.`);
        const now = nowMs();
        if (canCloseRoundInputs(policy, present, now) || policy.closed || now >= policy.deadline * 1000) {
            return { present, policy };
        }
        // Local wall-clock countdown, independent of block production. Also
        // wake precisely at the cutoff rather than overshooting by a poll.
        await sleep(Math.min(pollMs, Math.max(1, policy.deadline * 1000 - now)));
    }
}
export function requireClosedRoundInputs(policy, round) {
    if (!policy.opened || !policy.closed) {
        throw new Error(`Round ${round} aggregation inputs are not immutably closed.`);
    }
    const count = policy.acceptedSubmissions;
    if (!Number.isSafeInteger(count) || count < 0 || (Number(round) > 0 && count === 0)) {
        throw new Error(`Round ${round} has no valid nonempty aggregation input set.`);
    }
    // The admitted aggregator chose when to close. The ledger fixes the
    // actual closed count, not the early-start target, for publication.
    return count;
}
export function shouldDeferAggregatorTimeout(policy, aggregationGraceMs, nowMs = Date.now()) {
    if (!policy.opened)
        return false;
    if (!policy.closed && nowMs < policy.deadline * 1000)
        return true;
    // UI-selected windows can outlast the fixed progress polling budget.
    // Give a deadline-triggered aggregation its normal processing allowance.
    return nowMs >= policy.deadline * 1000
        && nowMs < policy.deadline * 1000 + aggregationGraceMs;
}
export function nextGMTimeoutState({ missedLoops, maxLoops }) {
    const nextMissedLoops = missedLoops + 1;
    return {
        missedLoops: nextMissedLoops >= maxLoops ? 0 : nextMissedLoops,
        shouldReportTimeout: nextMissedLoops >= maxLoops,
    };
}
export function nextAggregatorTimeoutTracker({ tracker = {}, expectedRound, expectedAggregator, maxLoops, }) {
    const round = Number(expectedRound);
    const aggregator = String(expectedAggregator || "").toLowerCase();
    if (!Number.isSafeInteger(round) || round < 0) {
        throw new Error(`Invalid timeout-report round: ${expectedRound}`);
    }
    if (!/^0x[0-9a-f]{40}$/.test(aggregator)) {
        throw new Error(`Invalid timeout-report aggregator: ${expectedAggregator}`);
    }
    const contextKey = `${round}:${aggregator}`;
    const priorMissedLoops = tracker.contextKey === contextKey
        ? Number(tracker.missedLoops || 0)
        : 0;
    const next = nextGMTimeoutState({ missedLoops: priorMissedLoops, maxLoops });
    return {
        contextKey,
        missedLoops: next.missedLoops,
        failureCount: priorMissedLoops + 1,
        shouldReportTimeout: next.shouldReportTimeout,
        expectedRound: round,
        expectedAggregator: aggregator,
    };
}
export function requiredTimeoutReports({ eligibleReporters, thresholdPercent }) {
    if (eligibleReporters <= 0)
        return 0;
    return Math.max(1, Math.ceil((eligibleReporters * thresholdPercent) / 100));
}
export function selectionGapRecoveryNeeded({ state, observedRound, lastSelectionRound, completedRounds, targetRounds, }) {
    const round = Number(observedRound);
    const selectedRound = Number(lastSelectionRound);
    const completed = Number(completedRounds);
    const target = Number(targetRounds);
    return String(state) === "UPDATING"
        && Number.isSafeInteger(round)
        && Number.isSafeInteger(selectedRound)
        && Number.isSafeInteger(completed)
        && Number.isSafeInteger(target)
        && round >= 0
        && selectedRound >= 0
        && completed >= 0
        && target > 0
        && completed < target
        && round > selectedRound;
}
