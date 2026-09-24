import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import { once } from 'node:events';
import { createDrainingUploadServer } from '../dist/upload_drain.js';

function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
}

function post(port, path, body, { unfinished = false } = {}) {
    const request = http.request({
        host: '127.0.0.1', port, path, method: 'POST', agent: false,
        headers: { 'Content-Length': unfinished ? 1000 : Buffer.byteLength(body) },
    });
    const result = new Promise((resolve, reject) => {
        request.once('error', reject);
        request.once('response', response => {
            const chunks = [];
            response.on('data', chunk => chunks.push(chunk));
            response.once('error', reject);
            response.once('end', () => resolve({
                status: response.statusCode,
                body: Buffer.concat(chunks).toString(),
            }));
        });
    });
    // Keep rejected socket results observed while other clients are set up.
    void result.catch(() => {});
    if (unfinished) request.write(body);
    else request.end(body);
    return { request, result };
}

test('collection cutoff aborts incomplete HTTP bodies but drains complete queued uploads', { timeout: 5000 }, async t => {
    const partialSeen = deferred();
    const queuedBodyReceived = deferred();
    const finishQueuedCommit = deferred();
    const committed = [];
    const errors = [];
    const { server, drain } = createDrainingUploadServer(async (request, response) => {
        if (request.url === '/partial') partialSeen.resolve();
        const chunks = [];
        for await (const chunk of request) chunks.push(chunk);
        const body = Buffer.concat(chunks).toString();
        if (request.url === '/queued') {
            queuedBodyReceived.resolve();
            // Models that arrived completely before the cutoff may still be
            // queued behind decryption or an on-chain commitment.
            await finishQueuedCommit.promise;
        }
        committed.push(body);
        response.end('accepted');
    }, error => { errors.push(error); });
    const clients = [];
    t.after(async () => {
        finishQueuedCommit.resolve();
        for (const client of clients) client.destroy();
        server.closeAllConnections();
        await drain();
    });
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    const port = server.address().port;

    const accepted = post(port, '/ready', 'model-one');
    clients.push(accepted.request);
    assert.equal((await accepted.result).status, 200);
    const queued = post(port, '/queued', 'model-two');
    clients.push(queued.request);
    await queuedBodyReceived.promise;
    const partial = post(port, '/partial', 'unfinished-model', { unfinished: true });
    clients.push(partial.request);
    await partialSeen.promise;
    // A connection with incomplete headers has no active request handler yet.
    const headersOnly = net.connect({ host: '127.0.0.1', port });
    clients.push(headersOnly);
    headersOnly.on('error', () => {});
    await once(headersOnly, 'connect');
    headersOnly.write('POST /partial-headers HTTP/1.1\r\nHost: localhost\r\n');

    let drained = false;
    const closed = drain().then(() => { drained = true; });
    assert.strictEqual(drain(), drain(), 'concurrent stop calls share the same drain');
    await assert.rejects(partial.result);
    assert.equal(drained, false, 'the completed queued model must be allowed to finish');
    const late = post(port, '/late', 'late-model');
    clients.push(late.request);
    await assert.rejects(late.result);
    assert.deepEqual(committed, ['model-one']);
    finishQueuedCommit.resolve();
    assert.deepEqual(await queued.result, { status: 200, body: 'accepted' });
    await closed;
    assert.deepEqual(committed, ['model-one', 'model-two']);
    assert.equal(errors.length, 1, 'only the intentionally aborted body reaches the error handler');
    await drain();
});
