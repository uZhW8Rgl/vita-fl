const requirePositiveRound = (value, label) => {
    if (!Number.isSafeInteger(value) || value <= 0) {
        throw new Error(`${label} must be a positive safe integer; got ${value}.`);
    }
    return value;
};
/**
 * Return contract-attempt rounds that must have been aborted for an older
 * finalized model to remain the active parent of the current attempt.
 *
 * A finalized source round r publishes model round r + 1. Consequently, when
 * model round m is active during contract round c, rounds m..c-1 are failed
 * attempts that must be explicitly marked aborted. A model from the future is
 * never usable.
 */
export const skippedAggregatorAttemptRounds = ({ modelRound, currentRound, }) => {
    const finalizedModelRound = requirePositiveRound(modelRound, "Active global-model round");
    const aggregationRound = requirePositiveRound(currentRound, "Current aggregation round");
    if (finalizedModelRound > aggregationRound) {
        throw new Error(`Active global-model round ${finalizedModelRound} is from the future `
            + `relative to aggregation round ${aggregationRound}.`);
    }
    return Array.from({ length: aggregationRound - finalizedModelRound }, (_unused, index) => finalizedModelRound + index);
};
export const requireAbortedAggregatorAttemptGap = ({ modelRound, currentRound, abortedAttemptRounds, }) => {
    const skippedRounds = skippedAggregatorAttemptRounds({
        modelRound,
        currentRound,
    });
    const aborted = new Set();
    for (const round of abortedAttemptRounds) {
        if (!Number.isSafeInteger(round) || round <= 0) {
            throw new Error(`Aborted contract-attempt round must be a positive safe integer; got ${round}.`);
        }
        aborted.add(round);
    }
    const unresolved = skippedRounds.filter((round) => !aborted.has(round));
    if (unresolved.length > 0) {
        throw new Error(`Active global-model round ${modelRound} predates aggregation round `
            + `${currentRound}, but contract attempt round(s) `
            + `${unresolved.join(", ")} are not marked aborted.`);
    }
    return skippedRounds;
};
