/** Cryptographic protocol tests; injected appraiser/evidence are synthetic. */
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import {createServiceIdentity} from './service_identity.mjs';
import {verifyServiceResponse,validateServiceClaims,serviceReportData} from '../dfl/node_server/dist/service_attestation.js';
import {createAdmissionServer} from './chain_server.mjs';
import {createMemoryWorkerRoots} from './worker_roots.mjs';
const policy={version:1,appId:'0x'+'11'.repeat(20),composeHash:'22'.repeat(32),serviceImage:'example/service@sha256:'+'33'.repeat(32),
    origin:'https://keys.example',platformMeasurements:[Object.fromEntries(['mrtd','rtmr0','rtmr1','rtmr2'].map(k=>[k,'44'.repeat(48)]))]};
const scope={chainId:'31337',registry:'0x'+'55'.repeat(20),policy:'0x'+'66'.repeat(20),serviceId:'77'.repeat(32),codeHash:'0x'+'88'.repeat(32)};
const nonce=()=>crypto.randomBytes(32).toString('hex');
test('fresh quote binds service signer; responses bind nonce, operation, status, epoch and exact payload',async()=>{
    const identity=createServiceIdentity({scope,policy,evidence:async report=>({quote:report.toString('hex'),eventLog:'[]',appCompose:'synthetic'}),
        appraise:async(claims,evidence)=>assert.equal(evidence.quote,serviceReportData(claims).toString('hex'))});
    const requestId=nonce(),proof=await identity.attest(requestId),binding=identity.binding();
    validateServiceClaims(proof.claims,policy,requestId,scope);
    assert.throws(()=>validateServiceClaims(proof.claims,policy,nonce(),scope));
    assert.throws(()=>validateServiceClaims(proof.claims,{...policy,composeHash:'99'.repeat(32)},requestId,scope));
    const expected={...binding,operation:'/v3/release',requestId:nonce()};
    const response=identity.sign(expected.operation,expected.requestId,200,{ciphertext:'secret ciphertext',tag:'bound'});
    assert.deepEqual(verifyServiceResponse(response,expected,200),response.payload);
    for(const changed of [{...response,payload:{ciphertext:'substituted'}},{...response,status:403},
        {...response,signature:'00'.repeat(64)}])assert.throws(()=>verifyServiceResponse(changed,expected,200));
    for(const changed of [{...expected,requestId:nonce()},{...expected,operation:'/v3/challenge'},
        {...expected,serviceInstanceId:nonce()},{...expected,servicePublicKey:nonce()}])
        assert.throws(()=>verifyServiceResponse(response,changed,200));
    const refusal=identity.sign(expected.operation,expected.requestId,403,{error:'denied'});
    assert.throws(()=>verifyServiceResponse(refusal,expected,403),{statusCode:403});
    assert.throws(()=>verifyServiceResponse({...refusal,signature:'00'.repeat(64)},expected,403),/signature/);
    identity.destroy();assert.throws(()=>identity.sign(expected.operation,expected.requestId,200,{}),/closed/);
});
test('failed or expired self-authorisation closes the root-use gate; no configuration bypass exists',async()=>{
    let clock=1700000000,accept=false,checks=0;
    const identity=createServiceIdentity({scope,policy,now:()=>clock,evidence:async()=>({quote:'synthetic'}),
        appraise:async()=>{checks++;if(!accept)throw new Error('synthetic self-appraisal rejected');}});
    await assert.rejects(identity.ensureAuthorised(),/rejected/);
    accept=true;await identity.ensureAuthorised();await identity.ensureAuthorised();assert.equal(checks,2);
    clock+=61;accept=false;await assert.rejects(identity.ensureAuthorised(),/rejected/);
    await assert.rejects(identity.attest(nonce()),/rejected/);
    identity.destroy();await assert.rejects(identity.ensureAuthorised(),/closed/);
});
test('HTTP admission refuses before using roots or chain when service self-appraisal fails',async t=>{
    let calls=0;
    const identity=createServiceIdentity({scope,policy,evidence:async()=>({quote:'synthetic'}),
        appraise:async()=>{throw new Error('synthetic disallowed service');}});
    const roots=createMemoryWorkerRoots(scope);
    const server=createAdmissionServer({workerRoots:roots,serviceId:scope.serviceId,serviceIdentity:identity,
        backend:{prepare:async()=>{calls++;throw new Error('must not execute');}}});
    await new Promise(r=>server.listen(0,'127.0.0.1',r));
    t.after(async()=>{server.closeAllConnections();await new Promise(r=>server.close(r));roots.destroy();identity.destroy();});
    const binding=identity.binding(),requestId=nonce(),operation='/v3/challenge';
    const reply=await fetch('http://127.0.0.1:'+server.address().port+operation,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({...binding,requestId,payload:{}})});
    assert.equal(reply.status,403);
    const refusal=await reply.json();
    assert.throws(()=>verifyServiceResponse(refusal,{...binding,operation,requestId},reply.status),{statusCode:403});
    assert.equal(roots.size,0);assert.equal(calls,0);
});
