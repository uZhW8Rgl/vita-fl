import crypto from 'node:crypto';

/** Complete JSON-RPC envelope; HttpProvider.request does not unwrap result. */
export async function submitSigned(provider, raw) {
    if (!/^0x[0-9a-f]+$/i.test(raw)) throw new Error('Signed transaction bytes required');
    const id=crypto.randomInt(1,2**30);
    const reply=await provider.request({jsonrpc:'2.0',id,method:'eth_sendRawTransaction',params:[raw]});
    if (reply?.jsonrpc!=='2.0'||reply?.id!==id||reply?.error||!/^0x[0-9a-f]{64}$/i.test(reply?.result||'')) {
        throw new Error('Signed transaction RPC response rejected');
    }
    return reply.result;
}

export async function minedReceipt(provider, hash) {
    const id=crypto.randomInt(1,2**30);
    const reply=await provider.request({jsonrpc:'2.0',id,method:'eth_getTransactionReceipt',params:[hash]});
    if (reply?.jsonrpc!=='2.0'||reply?.id!==id||reply?.error||!Object.hasOwn(reply,'result')) {
        throw new Error('Receipt RPC response rejected');
    }
    // null is the normal pending state; Web3's high-level helper throws here.
    return reply.result;
}
