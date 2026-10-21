/** Evaluation-only common role-policy setup. Never preauthorises a participant. */
import fs from 'node:fs/promises';
import {createRequire} from 'node:module';
import {KEY_ADMISSION_ABI} from '../dfl/node_server/dist/key_admission_abi.js';
const {Web3}=createRequire(new URL('../dfl/node_server/package.json',import.meta.url))('web3');
const [settingsFile,manifestFile,participant,appId,composeHash]=process.argv.slice(2);
if(!settingsFile||!manifestFile||![participant,appId].every(x=>/^0x[a-f0-9]{40}$/.test(x))||!/^[a-f0-9]{64}$/.test(composeHash))throw new Error('Explicit settings, reviewed manifest, participant, app and measured compose hash required');
const settings=JSON.parse(await fs.readFile(settingsFile,'utf8')),manifest=JSON.parse(await fs.readFile(manifestFile,'utf8'));
const url=new URL(settings.rpcUrl);
if(!((url.protocol==='http:'&&['127.0.0.1','localhost'].includes(url.hostname))||
    (url.protocol==='https:'&&new RegExp('^'+settings.runtimeAppId+'-8545\\.dstack(?:-pha)?-prod[0-9]+\\.phala\\.network$').test(url.hostname)))||url.username||url.password)throw new Error('Pinned Phala Anvil endpoint required');
const web3=new Web3(settings.rpcUrl);
if(await web3.eth.getChainId()!==31337n||web3.utils.keccak256(await web3.eth.getCode(settings.policy))!==settings.codeHash)throw new Error('Anvil policy pin mismatch');
const policy=new web3.eth.Contract(KEY_ADMISSION_ABI,settings.policy);
if(String(await policy.methods.registry().call()).toLowerCase()!==settings.registry||String(await policy.methods.serviceId().call())!=='0x'+settings.serviceId)throw new Error('Service identity changed');
const admin=web3.eth.accounts.privateKeyToAccount(process.env.KEY_POLICY_ADMIN_PRIVATE_KEY||'');
if(String(await policy.methods.administrator().call()).toLowerCase()!==admin.address.toLowerCase())throw new Error('Policy administrator mismatch');
web3.eth.accounts.wallet.add(admin);
const abi=JSON.parse(await fs.readFile(new URL('../dfl/node_server/abi/registry.json',import.meta.url),'utf8'));
const registry=new web3.eth.Contract(abi,settings.registry),bytes='0x'+Buffer.from(JSON.stringify(manifest)).toString('hex');
const workload=await registry.methods.workloadIdentity(bytes).call();
if(workload.workerImageDigest!==await registry.methods.expectedWorkerImageDigest().call())throw new Error('Probe image differs from the approved worker image');
const rolePolicy=await registry.methods.workerPolicyHash(bytes).call();
let receipt;
if(!await registry.methods.allowedWorkerPolicyHashes(rolePolicy).call()){
    receipt=await registry.methods.setWorkerPolicyHashAllowed(rolePolicy,true).send({from:admin.address,gas:'300000'});
}
// Full validation enforces the currently expected immutable image digest too.
await registry.methods.validateKmsCompose(bytes).call();
console.log(JSON.stringify({transactionHash:receipt?.transactionHash||null,rolePolicy,composeHash,
    participantPreauthorisation:false}));
