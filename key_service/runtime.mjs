/** Persistent service bootstrap inside the trusted contract-runtime CVM. */
import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import path from 'node:path';
import {createRequire} from 'node:module';
import {setTimeout as delay} from 'node:timers/promises';
import {initialiseWorkerRoots,openWorkerRoots} from './worker_roots.mjs';
import {createAdmissionBackend,createAdmissionServer} from './chain_server.mjs';
const {Web3}=createRequire(new URL('../dfl/node_server/package.json',import.meta.url))('web3');
const directory=process.env.KEY_SERVICE_STATE_DIRECTORY||'/state';
const settingsFile=path.join(directory,'settings.json'),rootsDirectory=path.join(directory,'workers');
const rpcUrl=process.env.KEY_SERVICE_RPC_URL||'http://anvil:8545';
if(process.argv[2]==='init'){
    const web3=new Web3(rpcUrl),registry=String(process.env.KEY_SERVICE_REGISTRY||'').toLowerCase();
    if(!/^0x[a-f0-9]{40}$/.test(registry))throw new Error('Explicit DeviceRegistry required');
    const registryAbi=[{type:'function',name:'keyAdmissionPolicy',stateMutability:'view',inputs:[],outputs:[{type:'address'}]},
        {type:'function',name:'setKeyAdmissionPolicy',stateMutability:'nonpayable',inputs:[{type:'address',name:'policy'}],outputs:[]}];
    const contract=new web3.eth.Contract(registryAbi,registry);
    let ready=false;
    for(let i=0;i<180;i++){
        try{if(await web3.eth.getChainId()!==31337n)throw new Error('Anvil 31337 required');
            if(await web3.eth.getCode(registry)!=='0x'){await contract.methods.keyAdmissionPolicy().call();ready=true;break;}}
        catch{}await delay(3000);
    }
    if(!ready)throw new Error('Compatible DeviceRegistry unavailable');
    await fs.mkdir(directory,{recursive:true,mode:0o700});
    let settings;
    try{settings=JSON.parse(await fs.readFile(settingsFile,'utf8'));}
    catch(error){if(error.code!=='ENOENT')throw error;}
    if(settings){
        if(settings.registry!==registry)throw new Error('Persisted registry changed');
        // A lost chain/root must not create a different service under the same identity.
        if(settings.rootMode!=='per-worker-random-v1')throw new Error('Explicit per-worker root state required');
        // The unprivileged serving process performs the owner/mode checks.
        const stat=await fs.lstat(rootsDirectory);if(!stat.isDirectory()||stat.isSymbolicLink()||(stat.mode&0o077))throw new Error('Persisted roots unsafe');
        await createAdmissionBackend({...settings,rpcUrl});
    }else{
        const entries=await fs.readdir(directory);
        if(entries.length)throw new Error('Incomplete service state: manual recovery required');
        const key=process.env.ETH_WALLET_PRIVATE_KEY;
        const admin=web3.eth.accounts.privateKeyToAccount(key||'');web3.eth.accounts.wallet.add(admin);
        const serviceId=crypto.randomBytes(32).toString('hex');
        const artifact=JSON.parse(await fs.readFile(new URL('./artifacts/AttestedKeyAdmission.json',import.meta.url),'utf8'));
        const policy=await new web3.eth.Contract(artifact.abi).deploy({data:artifact.bytecode.object,arguments:[registry,'0x'+serviceId]})
            .send({from:admin.address,gas:'20000000'});
        await contract.methods.setKeyAdmissionPolicy(policy.options.address).send({from:admin.address,gas:'300000'});
        settings={rpcUrl,registry,policy:policy.options.address.toLowerCase(),serviceId,chainId:'31337',rootMode:'per-worker-random-v1',
            codeHash:web3.utils.keccak256(await web3.eth.getCode(policy.options.address))};
        await initialiseWorkerRoots(rootsDirectory,settings);
        await fs.writeFile(settingsFile,JSON.stringify(settings,null,2)+'\n',{flag:'wx',mode:0o600});
    }
    if(process.getuid()===0){
        await fs.chown(path.join(rootsDirectory,'scope.json'),10001,10001);await fs.chown(rootsDirectory,10001,10001);
        await fs.chown(settingsFile,10001,10001);await fs.chown(directory,10001,10001);
    }
    console.log('Persistent on-chain key service initialised; no secret material exported.');
}else{
    const settings=JSON.parse(await fs.readFile(settingsFile,'utf8'));
    if(settings.rootMode!=='per-worker-random-v1')throw new Error('Explicit per-worker root state required');
    const workerRoots=await openWorkerRoots(rootsDirectory,settings),backend=await createAdmissionBackend({...settings,rpcUrl});
    const server=createAdmissionServer({workerRoots,backend,serviceId:settings.serviceId});
    server.listen(8000,'0.0.0.0',()=>console.log('Key service ready: onchain-v2, one DCAP appraisal per admission.'));
    for(const signal of ['SIGTERM','SIGINT'])process.on(signal,()=>server.close(()=>process.exit(0)));
}
