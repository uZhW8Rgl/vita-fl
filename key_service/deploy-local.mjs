/** Isolated Anvil bootstrap. No public-chain transactions or participant secrets. */
import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const {Web3}=createRequire(new URL('../dfl/node_server/package.json',import.meta.url))('web3');
const rpc=process.env.KEY_SERVICE_RPC_URL||'http://127.0.0.1:19545';
const parsed=new URL(rpc);
if(!['127.0.0.1','localhost','[::1]'].includes(parsed.hostname))throw new Error('Explicit local Anvil RPC required');
const web3=new Web3(rpc);
if(await web3.eth.getChainId()!==31337n)throw new Error('Anvil 31337 required');
const [admin]=await web3.eth.getAccounts();
const deploy=async(name,args)=>{
    const artifact=JSON.parse(await fs.readFile(new URL(`../smart_contracts/out/${name}.sol/${name}.json`,import.meta.url),'utf8'));
    return new web3.eth.Contract(artifact.abi).deploy({data:artifact.bytecode.object,arguments:args}).send({from:admin,gas:'25000000'});
};
const registry=await deploy('DeviceRegistry',[web3.utils.keccak256('independent attested key service experiment')]);
const policy=await deploy('AttestedKeyPolicy',[registry.options.address]);
const settings={rpcUrl:rpc,policy:policy.options.address.toLowerCase(),registry:registry.options.address.toLowerCase(),
    codeHash:web3.utils.keccak256(await web3.eth.getCode(policy.options.address)),serviceId:crypto.randomBytes(32).toString('hex')};
if(!process.argv[2])throw new Error('Explicit output path required');
await fs.writeFile(process.argv[2],JSON.stringify(settings,null,2)+'\n',{flag:'wx',mode:0o600});
console.log('Isolated Anvil policy deployed; public pins written to requested file.');
