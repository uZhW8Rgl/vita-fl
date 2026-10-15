import test from 'node:test';
import assert from 'node:assert/strict';
import {submitSigned,minedReceipt} from './rpc.mjs';

test('complete JSON-RPC request and unwrap confirmed hash', async()=>{
    const hash='0x'+'12'.repeat(32);
    const provider={request:async request=>{
        assert.equal(request.jsonrpc,'2.0'); assert.ok(Number.isInteger(request.id));
        assert.equal(request.method,'eth_sendRawTransaction');assert.deepEqual(request.params,['0x1234']);
        return {jsonrpc:'2.0',id:request.id,result:hash};
    }};
    assert.equal(await submitSigned(provider,'0x1234'),hash);
});
test('pending null receipt is polled without claiming an outcome', async()=>{
    let calls=0;const hash='0x'+'12'.repeat(32);
    const provider={request:async request=>({jsonrpc:'2.0',id:request.id,result:++calls===1?null:{transactionHash:hash,status:'0x0'}})};
    assert.equal(await minedReceipt(provider,hash),null);
    assert.deepEqual(await minedReceipt(provider,hash),{transactionHash:hash,status:'0x0'});
});
test('error, mismatched id and missing envelope never become a mined transaction hash', async()=>{
    for(const make of [r=>({jsonrpc:'2.0',id:r.id,error:{code:-32000}}),r=>({jsonrpc:'2.0',id:r.id+1,result:'0x'+'12'.repeat(32)}),()=> '0x'+'12'.repeat(32)]) {
        await assert.rejects(submitSigned({request:async r=>make(r)},'0x1234'),/RPC response rejected/);
    }
});
