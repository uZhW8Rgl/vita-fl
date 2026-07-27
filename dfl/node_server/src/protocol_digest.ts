import Web3 from "web3";

const web3 = new Web3();

const EIP712_DOMAIN_TYPEHASH = web3.utils.keccak256(
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)",
);
const MODEL_SUBMISSION_TYPEHASH = web3.utils.keccak256(
    "ModelSubmission(uint256 round,address aggregator,address worker,bytes32 parentModelHash,bytes32 modelHash,bytes32 packageHash,uint256 nonce)",
);
const AGGREGATION_STATEMENT_TYPEHASH = web3.utils.keccak256(
    "AggregationStatement(uint256 round,address aggregator,bytes32 inputRoot,uint256 inputCount,bytes32 algorithmHash,bytes32 policyHash,bytes32 outputModelHash,bytes32 outputBundleHash,bytes32 publicationHash,uint256 nonce)",
);
const GM_STORAGE_NAME_HASH = web3.utils.keccak256("VITA-FL GMStorage");
const GM_STORAGE_VERSION_HASH = web3.utils.keccak256("1");

export const FEDERATED_AVERAGING_V1_HASH = web3.utils.keccak256(
    "VITA-FL:fedavg:torch-state-dict:float64:equal-weight:v1",
) as `0x${string}`;
export const BOOTSTRAP_ROLLOVER_V1_HASH = web3.utils.keccak256(
    "VITA-FL:bootstrap-model-rollover:v1",
) as `0x${string}`;

const uintString = (value: bigint | number | string, label: string) => {
    let parsed: bigint;
    try {
        parsed = BigInt(value);
    } catch {
        throw new Error(`${label} must be an unsigned integer.`);
    }
    if (parsed < 0n) throw new Error(`${label} must be an unsigned integer.`);
    return parsed.toString();
};

const address = (value: string, label: string) => {
    if (!/^0x[0-9a-fA-F]{40}$/.test(String(value || ""))) {
        throw new Error(`${label} must be a 20-byte Ethereum address.`);
    }
    return value;
};

const bytes32 = (value: string, label: string) => {
    if (!/^0x[0-9a-fA-F]{64}$/.test(String(value || ""))) {
        throw new Error(`${label} must be exactly 32 bytes.`);
    }
    return value;
};

const nonEmptyString = (value: string, label: string) => {
    if (typeof value !== "string" || value.length === 0) {
        throw new Error(`${label} must be a non-empty string.`);
    }
    return value;
};

const hashEncoded = (types: string[], values: unknown[]) =>
    web3.utils.keccak256(web3.eth.abi.encodeParameters(types, values));

const gmStorageDomainSeparator = (
    chainId: bigint | number | string,
    verifyingContract: string,
) => hashEncoded(
    ["bytes32", "bytes32", "bytes32", "uint256", "address"],
    [
        EIP712_DOMAIN_TYPEHASH,
        GM_STORAGE_NAME_HASH,
        GM_STORAGE_VERSION_HASH,
        uintString(chainId, "chainId"),
        address(verifyingContract, "verifyingContract"),
    ],
);

const typedDataDigest = (domainSeparator: string, structHash: string) =>
    web3.utils.keccak256(
        `0x1901${domainSeparator.slice(2)}${structHash.slice(2)}`,
    ) as `0x${string}`;

export const deriveModelSubmissionDigest = ({
    chainId,
    verifyingContract,
    round,
    aggregator,
    worker,
    parentModelHash,
    modelHash,
    packageHash,
    nonce,
}: {
    chainId: bigint | number | string;
    verifyingContract: string;
    round: bigint | number | string;
    aggregator: string;
    worker: string;
    parentModelHash: string;
    modelHash: string;
    packageHash: string;
    nonce: bigint | number | string;
}): `0x${string}` => {
    const domainSeparator = gmStorageDomainSeparator(chainId, verifyingContract);
    const structHash = hashEncoded(
        [
            "bytes32",
            "uint256",
            "address",
            "address",
            "bytes32",
            "bytes32",
            "bytes32",
            "uint256",
        ],
        [
            MODEL_SUBMISSION_TYPEHASH,
            uintString(round, "round"),
            address(aggregator, "aggregator"),
            address(worker, "worker"),
            bytes32(parentModelHash, "parentModelHash"),
            bytes32(modelHash, "modelHash"),
            bytes32(packageHash, "packageHash"),
            uintString(nonce, "nonce"),
        ],
    );
    return typedDataDigest(domainSeparator, structHash);
};

export const deriveAggregationStatementDigest = ({
    chainId,
    verifyingContract,
    round,
    aggregator,
    inputRoot,
    inputCount,
    algorithmHash,
    policyHash,
    outputModelHash,
    outputBundleHash,
    publicationHash,
    nonce,
}: {
    chainId: bigint | number | string;
    verifyingContract: string;
    round: bigint | number | string;
    aggregator: string;
    inputRoot: string;
    inputCount: bigint | number | string;
    algorithmHash: string;
    policyHash: string;
    outputModelHash: string;
    outputBundleHash: string;
    publicationHash: string;
    nonce: bigint | number | string;
}): `0x${string}` => {
    const domainSeparator = gmStorageDomainSeparator(chainId, verifyingContract);
    const structHash = hashEncoded(
        [
            "bytes32",
            "uint256",
            "address",
            "bytes32",
            "uint256",
            "bytes32",
            "bytes32",
            "bytes32",
            "bytes32",
            "bytes32",
            "uint256",
        ],
        [
            AGGREGATION_STATEMENT_TYPEHASH,
            uintString(round, "round"),
            address(aggregator, "aggregator"),
            bytes32(inputRoot, "inputRoot"),
            uintString(inputCount, "inputCount"),
            bytes32(algorithmHash, "algorithmHash"),
            bytes32(policyHash, "policyHash"),
            bytes32(outputModelHash, "outputModelHash"),
            bytes32(outputBundleHash, "outputBundleHash"),
            bytes32(publicationHash, "publicationHash"),
            uintString(nonce, "nonce"),
        ],
    );
    return typedDataDigest(domainSeparator, structHash);
};

export const derivePublicationHash = ({
    modelCid,
    signatureCid,
    keyBundleCid,
}: {
    modelCid: string;
    signatureCid: string;
    keyBundleCid: string;
}): `0x${string}` => hashEncoded(
    ["string", "string", "string"],
    [
        nonEmptyString(modelCid, "modelCid"),
        nonEmptyString(signatureCid, "signatureCid"),
        nonEmptyString(keyBundleCid, "keyBundleCid"),
    ],
) as `0x${string}`;
