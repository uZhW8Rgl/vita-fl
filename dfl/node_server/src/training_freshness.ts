export type TrainingContext = {
    round: number;
    state: string;
    aggregator: string;
    parentModelCid: string;
};

export type TrainingContextComparison = {
    valid: boolean;
    reasons: string[];
};

function normalizedAddress(value: unknown): string {
    return String(value || '').trim().toLowerCase();
}

export function compareTrainingContexts(
    expected: TrainingContext,
    current: TrainingContext,
): TrainingContextComparison {
    const reasons: string[] = [];
    if (!Number.isInteger(expected.round) || expected.round < 0 || current.round !== expected.round) {
        reasons.push('round_changed');
    }
    if (expected.state !== 'TRAINING' || current.state !== expected.state) {
        reasons.push('state_changed');
    }
    if (
        !normalizedAddress(expected.aggregator) ||
        normalizedAddress(current.aggregator) !== normalizedAddress(expected.aggregator)
    ) {
        reasons.push('aggregator_changed');
    }
    if (!expected.parentModelCid || current.parentModelCid !== expected.parentModelCid) {
        reasons.push('parent_changed');
    }
    return { valid: reasons.length === 0, reasons };
}
