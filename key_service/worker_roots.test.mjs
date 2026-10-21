import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {initialiseWorkerRoots,openWorkerRoots} from './worker_roots.mjs';
const scope={chainId:'31337',policy:'0x'+'11'.repeat(20),registry:'0x'+'22'.repeat(20),serviceId:'33'.repeat(32)};
const claims={...scope,version:1,participant:'0x'+'44'.repeat(20),appId:'0x'+'55'.repeat(20),composeHash:'66'.repeat(32),challenge:'77'.repeat(32),recipientPublicKey:'88'.repeat(32)};
test('independent worker roots persist; transport challenges do not change the application key',async()=>{
    const dir=await fs.mkdtemp(path.join(os.tmpdir(),'worker-roots-')),storeDir=path.join(dir,'workers');
    try{
        await initialiseWorkerRoots(storeDir,scope);
        const store=await openWorkerRoots(storeDir,scope),first=await store.applicationKey(claims,{restoring:false});
        const another=await store.applicationKey({...claims,participant:'0x'+'99'.repeat(20)},{restoring:false});
        assert.notDeepEqual(first,another);
        const reopened=await openWorkerRoots(storeDir,scope);
        assert.deepEqual(await reopened.applicationKey({...claims,challenge:'aa'.repeat(32),recipientPublicKey:'bb'.repeat(32)},{restoring:true}),first);
        assert.notDeepEqual(await reopened.applicationKey({...claims,composeHash:'cc'.repeat(32)},{restoring:true}),first);
        const records=await Promise.all((await fs.readdir(storeDir)).filter(x=>x!=='scope.json').map(async x=>JSON.parse(await fs.readFile(path.join(storeDir,x),'utf8'))));
        assert.equal(records.length,2);assert.notEqual(records[0].root,records[1].root);
        await assert.rejects(store.applicationKey(claims,{restoring:false}),{code:'EEXIST'});
        assert.deepEqual(await store.applicationKey(claims,{restoring:true}),first);
        await assert.rejects(openWorkerRoots(storeDir,{...scope,serviceId:'dd'.repeat(32)}),/scope/);
        await assert.rejects(store.applicationKey({...claims,policy:'0x'+'ee'.repeat(20)},{restoring:true}),/scope/);
    }finally{await fs.rm(dir,{recursive:true,force:true});}
});
test('lost, corrupt and permissive worker-root state fails closed instead of creating replacement secrets',async()=>{
    const dir=await fs.mkdtemp(path.join(os.tmpdir(),'worker-roots-')),storeDir=path.join(dir,'workers');
    try{
        await initialiseWorkerRoots(storeDir,scope);const store=await openWorkerRoots(storeDir,scope);
        await assert.rejects(store.applicationKey(claims,{restoring:true}),{code:'ENOENT'});
        assert.deepEqual(await fs.readdir(storeDir),['scope.json']);
        await store.applicationKey(claims,{restoring:false});
        const filename=path.join(storeDir,(await fs.readdir(storeDir)).find(x=>x!=='scope.json'));
        await fs.chmod(filename,0o644);await assert.rejects(store.applicationKey(claims,{restoring:true}),/Unsafe/);
        await fs.chmod(filename,0o600);await fs.writeFile(filename,'corrupt');
        await assert.rejects(store.applicationKey(claims,{restoring:true}));
        await fs.unlink(filename);await fs.symlink(path.join(storeDir,'scope.json'),filename);
        await assert.rejects(store.applicationKey(claims,{restoring:true}));
    }finally{await fs.rm(dir,{recursive:true,force:true});}
});
