import test from 'node:test';
import assert from 'node:assert/strict';
import {createMemoryWorkerRoots} from './worker_roots.mjs';
const scope={chainId:'31337',policy:'0x'+'11'.repeat(20),registry:'0x'+'22'.repeat(20),serviceId:'33'.repeat(32)};
const claims={...scope,version:1,participant:'0x'+'44'.repeat(20),appId:'0x'+'55'.repeat(20),composeHash:'66'.repeat(32),challenge:'77'.repeat(32),recipientPublicKey:'88'.repeat(32)};
test('independent in-memory roots restore keys within the same service lifetime',async()=>{
    const store=createMemoryWorkerRoots(scope);
    try{
        const first=await store.applicationKey(claims,{restoring:false});
        const another=await store.applicationKey({...claims,participant:'0x'+'99'.repeat(20)},{restoring:false});
        assert.notDeepEqual(first,another);assert.equal(store.size,2);
        assert.deepEqual(await store.applicationKey({...claims,challenge:'aa'.repeat(32),recipientPublicKey:'bb'.repeat(32)},{restoring:true}),first);
        assert.notDeepEqual(await store.applicationKey({...claims,composeHash:'cc'.repeat(32)},{restoring:true}),first);
        await assert.rejects(store.applicationKey(claims,{restoring:false}),/already exists/);
        assert.deepEqual(await store.applicationKey(claims,{restoring:true}),first);
        await assert.rejects(store.applicationKey({...claims,policy:'0x'+'ee'.repeat(20)},{restoring:true}),/scope/);
    }finally{store.destroy();}
});
test('a new service process cannot restore a lost root or substitute a new one',async()=>{
    const old=createMemoryWorkerRoots(scope);
    await old.applicationKey(claims,{restoring:false});old.destroy();
    assert.equal(old.size,0);
    await assert.rejects(old.applicationKey(claims,{restoring:true}),/closed/);
    const fresh=createMemoryWorkerRoots(scope);
    await assert.rejects(fresh.applicationKey(claims,{restoring:true}),/no longer available/);
    assert.equal(fresh.size,0);fresh.destroy();
});
