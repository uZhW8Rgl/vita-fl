import test from 'node:test';
import assert from 'node:assert/strict';
import {submitSigned} from './rpc.mjs';

test('complete JSON-RPC request and unwrap confirmed hash', async()=>{
    const hash='0x'+'12'.repeat(32);
    const provider={request:async request=>{
        assert.equal(request.jsonrpc,'2.0'); assert.ok(Number.isInteger(request.id));
        assert.equal(request.method,'eth_sendRawTransaction');assert.deepEqual(request.params,['0x1234']);
        return {jsonrpc:'2.0',id:request.id,result:hash};
    }};
    assert.equal(await submitSigned(provider,'0x1234'),hash);
});
test('error, mismatched id and missing envelope never become a mined transaction hash', async()=>{
    for(const make of [r=>({jsonrpc:'2.0',id:r.id,error:{code:-32000}}),r=>({jsonrpc:'2.0',id:r.id+1,result:'0x'+'12'.repeat(32)}),()=> '0x'+'12'.repeat(32)]) {
        await assert.rejects(submitSigned({request:async r=>make(r)},'0x1234'),/RPC response rejected/);
    }
});
