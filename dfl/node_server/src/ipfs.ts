import axios from "axios";
import type { KeyObject } from "crypto";
import crypto from "crypto";
import fs from "fs";
import FormData from "form-data";

import {
  getActiveModelBundle,
  getAuthorizedDevices,
  createAggregationStatement,
  finalizeRoundWithAggregation,
  getDevicePublicKey,
  getRound,
  isGlobalModelPublished,
} from "./bc_client.js";
import {
  buildEncryptedGlobalModelArtifacts,
  decryptEncryptedGlobalModelArtifacts,
} from "./gm_crypto.js";

export const pinFile = async (filePath: string) => {
    try {
      const formData = new FormData();
      const file = fs.createReadStream(filePath);
      formData.append("file", file);

      if (process.env.IPFS_PROVIDER === "kubo") {
        const api = kuboApiBaseUrl();
        if (!api) {
          throw new Error("IPFS_PROVIDER=kubo requires KUBO_API");
        }

        const res = await axios.post(
          `${api}/api/v0/add?pin=true&cid-version=1&wrap-with-directory=false`,
          formData,
          {
            headers: formData.getHeaders(),
            timeout: ipfsTimeoutMs(),
          },
        );
        return res.data.Hash;
      }
  
      const pinataMetadata = JSON.stringify({
        name: "File name",
      });
      formData.append("pinataMetadata", pinataMetadata);
  
      const pinataOptions = JSON.stringify({
        cidVersion: 1,
      });
      formData.append("pinataOptions", pinataOptions);
  
      const res = await axios.post(
        "https://api.pinata.cloud/pinning/pinFileToIPFS",
        formData,
        {
          headers: {
            Authorization: `Bearer ${process.env.PINATA_JWT}`,
          },
        }
      );
      return res.data.IpfsHash;
    } catch (error) {
      console.error(`Failed to pin ${filePath} to IPFS`, error);
      throw error;
    }
  }

  const ipfsTimeoutMs = () => Number(process.env.IPFS_FETCH_TIMEOUT_MS || 30000);

  const kuboApiBaseUrl = () => (process.env.KUBO_API || "").replace(/\/+$/, "");

  const ipfsArchiveDir = () => {
    const raw = (process.env.IPFS_ARCHIVE_DIR || "/").trim();
    if (raw === "/" || raw === "") {
      return "/";
    }
    return raw.replace(/\/+$/, "");
  };

  const timestampedArtifactName = (baseName: string, round: number) => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    return `round-${round}-${stamp}-${baseName}`;
  };

  const ensureKuboMfsDir = async (dir: string) => {
    const api = kuboApiBaseUrl();
    if (!api) {
      throw new Error("IPFS_PROVIDER=kubo requires KUBO_API");
    }
    await axios.post(`${api}/api/v0/files/mkdir`, null, {
      params: {
        arg: dir,
        parents: true,
      },
      timeout: ipfsTimeoutMs(),
    });
  };

  const copyCidToKuboMfs = async (cid: string, destPath: string) => {
    const api = kuboApiBaseUrl();
    if (!api) {
      throw new Error("IPFS_PROVIDER=kubo requires KUBO_API");
    }
    await axios.post(`${api}/api/v0/files/cp`, null, {
      params: {
        arg: [`/ipfs/${cid}`, destPath],
      },
      timeout: ipfsTimeoutMs(),
      paramsSerializer: {
        serialize: (params) => {
          const values = Array.isArray(params.arg) ? params.arg : [params.arg];
          return values.map((value) => `arg=${encodeURIComponent(String(value))}`).join("&");
        },
      },
    });
  };

  const archivePinnedFileToKubo = async (cid: string, baseName: string, round: number) => {
    if (process.env.IPFS_PROVIDER !== "kubo") {
      return;
    }
    const dir = ipfsArchiveDir();
    const filename = timestampedArtifactName(baseName, round);
    const destPath = dir === "/" ? `/${filename}` : `${dir}/${filename}`;
    if (dir !== "/") {
      await ensureKuboMfsDir(dir);
    }
    await copyCidToKuboMfs(cid, destPath);
    console.log(`Archived ${baseName} in Kubo MFS: ${destPath}`);
  };
  
  const ipfsGatewayUrl = (hash: string) => {
    const configuredGateway = process.env.IPFS_PROVIDER === "kubo"
      ? process.env.KUBO_GATEWAY
      : process.env.IPFS_GATEWAY;
    const gateway = (configuredGateway || process.env.IPFS_GATEWAY || process.env.KUBO_GATEWAY || "").replace(/\/+$/, "");
    if (!gateway) {
      throw new Error("Missing IPFS gateway configuration");
    }
    return gateway.endsWith("/ipfs") ? `${gateway}/${hash}` : `${gateway}/ipfs/${hash}`;
  }

  const kuboApiCatUrl = (hash: string) => {
    const api = kuboApiBaseUrl();
    return api ? `${api}/api/v0/cat?arg=${encodeURIComponent(hash)}` : "";
  }

  const fetchIPFSBytes = async (hash: string) => {
    const catUrl = process.env.IPFS_PROVIDER === "kubo" ? kuboApiCatUrl(hash) : "";
    const url = catUrl || ipfsGatewayUrl(hash);
    try {
      const res = catUrl
        ? await axios.post(url, null, { responseType: "arraybuffer", timeout: ipfsTimeoutMs() })
        : await axios.get(url, { responseType: "arraybuffer", timeout: ipfsTimeoutMs() });
      return Buffer.from(res.data);
    } catch (error) {
      if (catUrl) {
        const gatewayUrl = ipfsGatewayUrl(hash);
        console.warn(`Kubo API cat failed for ${hash}; falling back to gateway ${gatewayUrl}`);
        const res = await axios.get(gatewayUrl, { responseType: "arraybuffer", timeout: ipfsTimeoutMs() });
        return Buffer.from(res.data);
      }
      console.error(`Failed to fetch IPFS file ${hash} from ${url}`, error);
      throw error;
    }
  }

  export const getFileFromIPFS = async (hash: string, outPath: string = "./data/gm.bin") => {
    const bytes = await fetchIPFSBytes(hash);
    fs.writeFileSync(outPath, bytes);
    console.log(`File written to ${outPath} from IPFS hash ${hash}`);
  }

  const encryptedBundlePaths = () => ({
    bundlePath: "./data/results_iid/aggregated.bundle.enc",
    bundleSignaturePath: "./data/results_iid/aggregated.bundle.enc.sig",
    keyBundlePath: "./data/results_iid/aggregated.bundle.keys.json",
  });
  
  export const updateGM = async (
    expectedModelRound: number,
    participantPrivateKey: KeyObject,
  ) => {
    const modelPath = "./data/results_iid/aggregated.bin";
    const sigPath = "./data/results_iid/aggregated.bin.sig";
    if (!Number.isSafeInteger(expectedModelRound) || expectedModelRound <= 0) {
      throw new Error(`Invalid expected global-model round: ${expectedModelRound}`);
    }
    const intendedSourceRound = expectedModelRound - 1;
    const sourceRound = Number(await getRound());
    if (
      sourceRound >= expectedModelRound
      && await isGlobalModelPublished(intendedSourceRound)
    ) {
      console.log(
        `Aggregation round ${intendedSourceRound} is already atomically finalized; ` +
        "skipping duplicate artifact publication.",
      );
      return {
        sourceRound: intendedSourceRound,
        expectedModelRound,
        reconciled: true,
      };
    }
    if (!Number.isSafeInteger(sourceRound) || sourceRound < 0 || sourceRound + 1 !== expectedModelRound) {
      throw new Error(
        `Global-model round changed before publication: expected source round ${expectedModelRound - 1}, got ${sourceRound}`,
      );
    }
    const round = expectedModelRound;

    const recipients = [];
    for (const address of await getAuthorizedDevices()) {
      const publicKeyDerHex = await getDevicePublicKey(address);
      if (!publicKeyDerHex || publicKeyDerHex === "0x") {
        console.warn(`Skipping GM encryption recipient without RSA key: ${address}`);
        continue;
      }
      recipients.push({ address, publicKeyDerHex });
    }
    const { bundlePath, bundleSignaturePath, keyBundlePath } = encryptedBundlePaths();
    const { outputBundleHash } = await buildEncryptedGlobalModelArtifacts({
      modelPath,
      signaturePath: sigPath,
      encryptedBundlePath: bundlePath,
      encryptedSignaturePath: bundleSignaturePath,
      keyBundlePath,
      recipients,
      round,
      signingPrivateKey: participantPrivateKey,
    });
    const outputModelHash =
      `0x${crypto.createHash("sha256").update(fs.readFileSync(modelPath)).digest("hex")}`;

    const modelCid = await pinFile(bundlePath);
    if (!modelCid) throw new Error("Pinning failed for encrypted model bundle, no CID returned");
    await archivePinnedFileToKubo(modelCid, "aggregated.bundle.enc", round);

    const sigCid = await pinFile(bundleSignaturePath);
    if (!sigCid) throw new Error("Pinning failed for encrypted bundle signature, no CID returned");
    await archivePinnedFileToKubo(sigCid, "aggregated.bundle.enc.sig", round);

    const keyBundleCid = await pinFile(keyBundlePath);
    if (!keyBundleCid) throw new Error("Pinning failed for encrypted key bundle, no CID returned");
    await archivePinnedFileToKubo(keyBundleCid, "aggregated.bundle.keys.json", round);

    console.log("New encrypted GM bundle CID:", modelCid);
    console.log("New encrypted GM bundle signature CID:", sigCid);
    console.log("New encrypted GM key bundle CID:", keyBundleCid);

    const aggregationStatement = await createAggregationStatement({
      sourceRound,
      modelCid,
      signatureCid: sigCid,
      keyBundleCid,
      outputModelHash,
      outputBundleHash,
    });
    await finalizeRoundWithAggregation({
      modelCid,
      signatureCid: sigCid,
      keyBundleCid,
      outputModelHash,
      outputBundleHash,
      statementSignature: aggregationStatement.statementSignature,
    });
    const observedRound = Number(await getRound());
    if (
      observedRound !== expectedModelRound
      || !(await isGlobalModelPublished(sourceRound))
    ) {
      throw new Error(
        `Atomic aggregation finalization was not observable: expected round ` +
        `${expectedModelRound}, got ${observedRound}.`,
      );
    }
    console.log(
      "Encrypted global model and TEE-signed aggregation evidence finalized atomically on-chain.",
      {
        sourceRound,
        inputRoot: aggregationStatement.policy.inputRoot,
        inputCount: aggregationStatement.policy.acceptedSubmissions,
        policyHash: aggregationStatement.policy.policyHash,
        outputModelHash,
        outputBundleHash,
        publicationHash: aggregationStatement.publicationHash,
        statementDigest: aggregationStatement.statementDigest,
      },
    );
    return {
      sourceRound,
      expectedModelRound,
      reconciled: false,
      aggregationStatement,
    };
  }
  
  export const getCurrentModel = async (participantPrivateKey: KeyObject) => {
    const activeModel = await getActiveModelBundle();
    const modelCid = String(activeModel.modelCid || "");
    const sigCid = String(activeModel.sigCid || "");
    const keyBundleCid = String(activeModel.keyBundleCid || "");
    if (!modelCid) {
      throw new Error("Missing encrypted global model bundle CID on-chain.");
    }
    if (!sigCid) {
      throw new Error("Missing encrypted global model bundle signature CID on-chain.");
    }
    if (!keyBundleCid) {
      throw new Error("Missing encrypted global model key bundle CID on-chain.");
    }
    console.log("Model CID:", modelCid);
    console.log("Sig CID:", sigCid);
    console.log("Key bundle CID:", keyBundleCid);

    const { bundlePath, bundleSignaturePath, keyBundlePath } = encryptedBundlePaths();
    fs.writeFileSync(bundlePath, await fetchIPFSBytes(modelCid));
    console.log(`Encrypted GM bundle written to ${bundlePath}`);
    fs.writeFileSync(bundleSignaturePath, await fetchIPFSBytes(sigCid));
    console.log(`Encrypted GM bundle signature written to ${bundleSignaturePath}`);
    fs.writeFileSync(keyBundlePath, await fetchIPFSBytes(keyBundleCid));
    console.log(`Encrypted GM key bundle written to ${keyBundlePath}`);
    await decryptEncryptedGlobalModelArtifacts({
      encryptedBundlePath: bundlePath,
      keyBundlePath,
      ownAddress: String(process.env.ACCOUNT_ADDRESS || ""),
      outModelPath: "./data/gm.bin",
      outSignaturePath: "./data/gm.bin.sig",
      decryptionPrivateKey: participantPrivateKey,
    });
    console.log("Encrypted global model bundle fetched + decrypted");
    return {
      modelCid,
      sigCid,
      keyBundleCid,
      publisher: activeModel.publisher,
      publisherPublicKeyDerHex: activeModel.publisherPublicKeyDerHex,
    };
  }
