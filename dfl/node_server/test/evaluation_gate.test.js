import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluationUploadDecision } from '../dist/evaluation_gate.js';

const context = { round: 1, aggregator: '0x' + 'a'.repeat(40), participant: '0x' + 'b'.repeat(40) };
const env = { DFL_EVALUATION_GATE_URL: 'https://control.example.test', DFL_EVALUATION_GATE_RUN_ID: 'x'.repeat(32) };
const valid = { ...context, run_id: env.DFL_EVALUATION_GATE_RUN_ID, gate_round: 1, active: true, applies: true, allow: false };

test('disabled receiver gate performs no external call', async () => {
    assert.deepEqual(await evaluationUploadDecision(context, {env: {}, request: () => { throw Error('unexpected'); }}), {allow: true});
});

test('authenticated participant decision uses pinned origin/run and blocks without forwarding credentials', async () => {
    const result = await evaluationUploadDecision(context, { env, request: async (url, options) => {
        assert.equal(url.origin, env.DFL_EVALUATION_GATE_URL);
        assert.equal(url.searchParams.get('run_id'), env.DFL_EVALUATION_GATE_RUN_ID);
        assert.equal(options.redirect, 'error');
        assert.equal(options.headers.Authorization, undefined);
        return new Response(JSON.stringify(valid));
    }});
    assert.equal(result.allow, false);
});

test('enabled gate fails closed on unavailable, stale and mismatched decisions', async () => {
    for (const result of [{...valid, run_id: 'old'}, {...valid, round: 2}, {...valid, participant: context.aggregator},
                          {...valid, allow: 'true'}, {...valid, applies: false}, null]) {
        await assert.rejects(evaluationUploadDecision(context, {env, request: async () => result === null
            ? new Response('', {status: 503}) : new Response(JSON.stringify(result))}));
    }
    await assert.rejects(evaluationUploadDecision(context, {env: {...env, DFL_EVALUATION_GATE_RUN_ID: 'bad'}}));
    assert.deepEqual(await evaluationUploadDecision(context, {env: {...env, DFL_EVALUATION_GATE_RUN_ID: ''}}), {allow: true});
    await assert.rejects(evaluationUploadDecision(context, {env: {...env, DFL_EVALUATION_GATE_URL: 'http://control.test'}}));
});

test('release and later rounds cannot remain blocked by the round-one gate', async () => {
    for (const [ctx, reply] of [[context, {...valid, active: false, applies: false, allow: true}],
                               [{...context, round: 2}, {...valid, round: 2, applies: false, allow: true}]]) {
        assert.equal((await evaluationUploadDecision(ctx, {env, request: async () => new Response(JSON.stringify(reply))})).allow, true);
    }
});


test('later rounds perform no gate I/O even when the control service is unavailable', async () => {
    const options = {env, request: () => {throw new Error('unexpected gate I/O');}};
    assert.equal((await evaluationUploadDecision({...context, round: 2}, options)).allow, true);
    await assert.rejects(evaluationUploadDecision({...context, round: 2}, {
        ...options, env: {...env, DFL_EVALUATION_GATE_RUN_ID: 'invalid'},
    }));
});
