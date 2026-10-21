/** Stateless startup: Foundry deploys contracts; worker roots live only in RAM. */
import {createRequire} from 'node:module';
import {setTimeout as delay} from 'node:timers/promises';
import {createMemoryWorkerRoots} from './worker_roots.mjs';
import {createAdmissionBackend,createAdmissionServer} from './chain_server.mjs';
const {Web3}=createRequire(new URL('../dfl/node_server/package.json',import.meta.url))('web3');
const rpcUrl=process.env.KEY_SERVICE_RPC_URL||'http://anvil:8545';
const registry=String(process.env.KEY_SERVICE_REGISTRY||'').toLowerCase();
if(!/^0x[a-f0-9]{40}$/.test(registry))throw new Error('Explicit DeviceRegistry required');
const web3=new Web3(rpcUrl);
const getter=(name,type)=>[{type:'function',name,stateMutability:'view',inputs:[],outputs:[{type}]}];
const contract=new web3.eth.Contract(getter('keyAdmissionPolicy','address'),registry);
let settings,backend;
for(let i=0;i<180;i++){
    try{
        if(await web3.eth.getChainId()!==31337n)throw new Error('Anvil 31337 required');
        const policy=String(await contract.methods.keyAdmissionPolicy().call()).toLowerCase();
        const code=await web3.eth.getCode(policy);
        if(code==='0x')throw new Error('Foundry admission deployment not ready');
        const admission=new web3.eth.Contract(getter('serviceId','bytes32'),policy);
        const serviceId=String(await admission.methods.serviceId().call()).slice(2).toLowerCase();
        settings={rpcUrl,registry,policy,serviceId,chainId:'31337',codeHash:web3.utils.keccak256(code)};
        backend=await createAdmissionBackend(settings);break;
    }catch(error){if(i===179)throw error;await delay(3000);}
}
const workerRoots=createMemoryWorkerRoots(settings);
const server=createAdmissionServer({workerRoots,backend,serviceId:settings.serviceId});
server.listen(8000,'0.0.0.0',()=>console.log('Key service ready: onchain-v2, memory-only worker roots, one DCAP appraisal per admission.'));
for(const signal of ['SIGTERM','SIGINT'])process.once(signal,()=>{
    server.closeAllConnections();server.close(()=>{workerRoots.destroy();process.exit(0);});
});
