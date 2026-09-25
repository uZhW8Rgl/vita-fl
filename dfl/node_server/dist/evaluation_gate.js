export async function evaluationUploadDecision(context, options = {}) {
    const env = options.env || process.env;
    const configured = env.DFL_EVALUATION_GATE_URL || '';
    if (!configured)
        return { allow: true };
    const origin = new URL(configured);
    if (origin.protocol !== 'https:' || origin.username || origin.password || origin.search || origin.hash ||
        !['', '/'].includes(origin.pathname))
        throw new Error('Invalid evaluation gate HTTPS origin');
    const runId = env.DFL_EVALUATION_GATE_RUN_ID || '';
    // Evaluation-capable deployments may run ordinary scenarios without prearm.
    if (!runId)
        return { allow: true };
    if (!/^[a-zA-Z0-9_-]{32,128}$/.test(runId))
        throw new Error('Missing pinned evaluation gate generation');
    if (!Number.isSafeInteger(context.round) || context.round < 1 ||
        ![context.aggregator, context.participant].every(value => /^0x[0-9a-f]{40}$/.test(value))) {
        throw new Error('Invalid evaluation gate participant context');
    }
    // This cooperative fault is exclusively scoped to round one.
    if (context.round > 1)
        return { allow: true, run_id: runId };
    const endpoint = new URL('/api/evaluation/upload-gate/decision', origin);
    endpoint.search = new URLSearchParams({ run_id: runId, round: String(context.round),
        aggregator: context.aggregator, participant: context.participant }).toString();
    const response = await (options.request || fetch)(endpoint, {
        method: 'GET', redirect: 'error', headers: { 'Cache-Control': 'no-cache' }, signal: AbortSignal.timeout(5000),
    });
    if (response.status !== 200)
        throw new Error('Evaluation gate decision unavailable');
    const raw = await response.text();
    if (raw.length > 4096)
        throw new Error('Oversized evaluation gate decision');
    const decision = JSON.parse(raw);
    if (decision.run_id !== runId || decision.round !== context.round || decision.gate_round !== 1 ||
        decision.aggregator !== context.aggregator || decision.participant !== context.participant ||
        typeof decision.allow !== 'boolean' || typeof decision.active !== 'boolean' ||
        typeof decision.applies !== 'boolean' || decision.applies !== (decision.active && context.round === 1) ||
        (!decision.applies && !decision.allow)) {
        throw new Error('Evaluation gate decision does not match the pinned run and request');
    }
    return { allow: decision.allow, run_id: runId };
}
