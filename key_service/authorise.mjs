/** Authorise exact public manifest bytes on Anvil. Never touches Phala provisioning. */
import fs from "node:fs/promises";
import crypto from "node:crypto";
import {createRequire} from "node:module";
const require = createRequire(new URL("../dfl/node_server/package.json",import.meta.url));
const {Web3} = require("web3");
const [manifestFile, participant, appId] = process.argv.slice(2);
if(!manifestFile || !/^0x[0-9a-fA-F]{40}$/.test(participant || "") || !/^0x[0-9a-fA-F]{40}$/.test(appId || "")) throw new Error("Usage: authorise.mjs exact-app-compose.json participant-address app-id-address");
const manifest = await fs.readFile(manifestFile);
JSON.parse(manifest.toString());
const web3 = new Web3(process.env.KEY_SERVICE_RPC_URL);
if(await web3.eth.getChainId() !== 31337n) throw new Error("Anvil only; public-chain writes forbidden");
const code = await web3.eth.getCode(process.env.KEY_SERVICE_POLICY);
if(code === "0x" || web3.utils.keccak256(code) !== process.env.KEY_SERVICE_POLICY_CODE_HASH) throw new Error("Policy code pin mismatch");
const account = web3.eth.accounts.privateKeyToAccount(process.env.KEY_POLICY_ADMIN_PRIVATE_KEY || "");
web3.eth.accounts.wallet.add(account);
const contract = new web3.eth.Contract([{type:"function",name:"authorise",stateMutability:"nonpayable",
    inputs:[{type:"address",name:"participant"},{type:"address",name:"appId"},{type:"bytes32",name:"composeHash"}],outputs:[]}],process.env.KEY_SERVICE_POLICY);
const composeHash = "0x"+crypto.createHash("sha256").update(manifest).digest("hex");
const method = contract.methods.authorise(participant,appId,composeHash);
const gas = await method.estimateGas({from:account.address});
await method.send({from:account.address,gas:(gas*130n/100n).toString()});
console.log(JSON.stringify({participant,appId,composeHash,policy:process.env.KEY_SERVICE_POLICY}));
