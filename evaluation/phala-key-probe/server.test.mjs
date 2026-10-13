import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {once} from 'node:events';
import {serve,observation} from './server.mjs';
test('public observations bind the real process/boot identifiers into quote REPORTDATA',async()=>{
    const challenge='ab'.repeat(32), claims={schema_version:1,challenge,key_outcome:'KEY_RECOVERED'};
    let report;
    const r=await observation(challenge,{processInstance:'proc',bootId:'boot',probe:async()=>({claims,app_compose:'{}'}),client:{getQuote:async bytes=>{report=bytes;return {quote:'q',event_log:'e'};}}});
    assert.equal(r.claims.schema_version,2);assert.equal(r.claims.process_instance_id,'proc');
    assert.equal(r.claims.system_boot_id,'boot');
    assert.deepEqual(report,crypto.createHash('sha512').update('VITA-FL:key-evaluation:v1\0').update(r.claims_json).digest());
    assert.deepEqual(Object.keys(r),['claims','claims_json','quote','event_log','app_compose']);
});
test('four-key measurements and custody facts are bound to the fresh quote',async()=>{
    process.env.EVALUATION_ALL_KEYS='true';
    try {
        const fields={action_public_key_sha256:'a'.repeat(64),sello_public_key_sha256:'b'.repeat(64),
            air_public_key_sha256:'c'.repeat(64),python_keys_match:true,no_swap:true};
        let quoted;
        const result=await observation('ab'.repeat(32),{processInstance:'process',bootId:'boot',
            allKeyObservation:async()=>fields,
            probe:async()=>({claims:{schema_version:3,rsa_public_key_sha256:'d'.repeat(64)},app_compose:'{}'}),
            client:{getQuote:async report=>{quoted=report;return {quote:'synthetic',event_log:'synthetic'};}}});
        assert.equal(result.claims.schema_version,5);
        for(const [name,value] of Object.entries(fields)) assert.equal(result.claims[name],value);
        assert.deepEqual(quoted,crypto.createHash('sha512').update('VITA-FL:key-evaluation:v1\0').update(result.claims_json).digest());
    }finally{delete process.env.EVALUATION_ALL_KEYS;}
});
test('lifecycle operations require authorization and restore ciphertext without exporting it',async()=>{
    process.env.EVALUATION_GATES_ENABLED='true';
    const dir=await fs.mkdtemp(path.join(os.tmpdir(),'probe-'));const state=path.join(dir,'sealed.json');
    await fs.writeFile(state,'encrypted fixture');let exits=0;
    const token='a'.repeat(64);const server=serve({token,statePath:state,port:0,exit:()=>exits++});await once(server,'listening');
    const origin=`http://127.0.0.1:${server.address().port}`;
    try{
        assert.equal((await fetch(origin+'/v1/evaluation/state/reset',{method:'POST'})).status,401);
        const post=endpoint=>fetch(origin+endpoint,{method:'POST',headers:{Authorization:'Bearer '+token}});
        assert.equal((await post('/v1/evaluation/state/backup')).status,200);
        assert.equal((await post('/v1/evaluation/state/reset')).status,200);
        await assert.rejects(fs.readFile(state));
        assert.deepEqual(await (await post('/v1/evaluation/state/restore')).json(),{restored:true});
        assert.equal(await fs.readFile(state,'utf8'),'encrypted fixture');
        assert.equal((await post('/v1/evaluation/process-restart')).status,200);
        await new Promise(resolve=>setTimeout(resolve,150));assert.equal(exits,1);
    }finally{server.close();await fs.rm(dir,{recursive:true,force:true});}
});
