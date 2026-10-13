/** Dedicated, authenticated experiment. No private key or ciphertext is exported. */
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import http from 'node:http';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {probeKey} from '../../dfl/node_server/dist/evaluation_key_probe.js';
import {allKeyObservation,securityReport,probeParticipantKey} from './security.mjs';
const require = createRequire(new URL('../../dfl/node_server/package.json', import.meta.url));
const {DstackClient} = await import(require.resolve('@phala/dstack-sdk'));

export async function observation(challenge, options = {}) {
    const client = options.client || new DstackClient('/var/run/dstack.sock');
    const additional = process.env.EVALUATION_ALL_KEYS === 'true' ? await (options.allKeyObservation||allKeyObservation)(challenge) : {};
    const base = await (options.probe || probeKey)(challenge, {
        ...options.probeOptions,
        ...(process.env.EVALUATION_ALL_KEYS === 'true' ? {initializeKey:probeParticipantKey} : {}),
        quote: async () => ({quote: '', event_log: ''}),
    });
    const claims = {...base.claims, ...additional, schema_version: process.env.EVALUATION_ALL_KEYS === 'true' ? 5 : base.claims.schema_version >= 3 ? 4 : 2,
        process_instance_id: options.processInstance,
        system_boot_id: options.bootId || (await fs.readFile('/proc/sys/kernel/random/boot_id', 'utf8')).trim()};
    const claimsJson = JSON.stringify(claims);
    const report = crypto.createHash('sha512').update('VITA-FL:key-evaluation:v1\0').update(claimsJson).digest();
    const quote = await client.getQuote(report);
    return {claims, claims_json: claimsJson, quote: quote.quote,
        event_log: quote.event_log, app_compose: base.app_compose};
}

const baseEphemeral = () => process.env.PARTICIPANT_KEY_PROVIDER === 'ephemeral';

export function serve(options = {}) {
    const token = options.token || process.env.PRIVATE_KEY;
    if (!token || token.length < 32 || process.env.EVALUATION_GATES_ENABLED !== 'true') {
        throw new Error('Dedicated authenticated evaluation configuration required');
    }
    const state = options.statePath || '/var/lib/vita-fl/participant-rsa.v1.sealed.json';
    const processInstance = crypto.randomBytes(16).toString('hex');
    const exit = options.exit || (() => process.exit(0));
    let busy = false;
    const server = http.createServer(async (req, res) => {
        const send = (status, value) => res.writeHead(status, {'Content-Type':'application/json'}).end(JSON.stringify(value));
        if (req.method === 'GET' && req.url === '/health') {send(200, {ready:true, process_instance_id:processInstance}); return;}
        const expected = crypto.createHash('sha256').update('Bearer '+token).digest();
        const actual = crypto.createHash('sha256').update(req.headers.authorization || '').digest();
        if (!crypto.timingSafeEqual(expected, actual)) {send(401, {error:'EVALUATION_AUTH_REQUIRED'}); return;}
        if (req.method !== 'POST') {send(404, {}); return;}
        if (busy) {send(409, {error:'BUSY'}); return;}
        busy = true;
        try {
            if (req.url === '/v1/evaluation/key' || req.url === '/v1/evaluation/security') {
                const chunks=[]; let size=0;
                for await (const chunk of req) {size+=chunk.length; if(size>64) throw new Error('size'); chunks.push(chunk);}
                const challenge=Buffer.concat(chunks).toString('ascii');
                const value = await observation(challenge, {...options,processInstance});
                if(req.url === '/v1/evaluation/security') {
                    const report=await securityReport(challenge);
                    const digest=crypto.createHash('sha256').update(JSON.stringify(report)).digest('hex');
                    const claims={...value.claims,security_report_sha256:digest};
                    const claims_json=JSON.stringify(claims);
                    const client=options.client||new DstackClient('/var/run/dstack.sock');
                    const quote=await client.getQuote(crypto.createHash('sha512').update('VITA-FL:key-evaluation:v1\0').update(claims_json).digest());
                    send(200,{...value,claims,claims_json,quote:quote.quote,event_log:quote.event_log,security_report:report});
                    return;
                }
                send(200, value);
            } else if (req.url === '/v1/evaluation/process-restart') {
                send(200, {restarting:true, process_instance_id:processInstance});
                setTimeout(exit, 100);
            } else if (req.url.startsWith('/v1/evaluation/state/') && baseEphemeral()) {
                send(409, {error:'EPHEMERAL_KEYS_HAVE_NO_PERSISTENT_STATE'});
            } else if (req.url === '/v1/evaluation/state/backup') {
                // Baseline is retained inside this test TEE, never downloaded.
                await fs.copyFile(state, state+'.baseline', fs.constants.COPYFILE_EXCL).catch(async err => {
                    if(err.code !== 'EEXIST') throw err;
                });
                send(200, {saved:true});
            } else if (req.url === '/v1/evaluation/state/reset') {
                await fs.access(state+'.baseline');
                await fs.rm(state);
                send(200, {reset:true});
            } else if (req.url === '/v1/evaluation/state/restore') {
                await fs.copyFile(state+'.baseline',state);
                send(200, {restored:true});
            } else {send(404, {});}
        } catch {send(503, {error:'EVALUATION_UNAVAILABLE'});}
        finally {busy=false;}
    });
    server.requestTimeout=30000;
    server.listen(options.port ?? 8001,'0.0.0.0');
    return server;
}
if(process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) serve();
