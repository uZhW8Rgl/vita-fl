// @ts-nocheck
import crypto from "crypto";
import { sign } from "web3-eth-accounts";

const telemetryUrl = String(process.env.DFL_TELEMETRY_URL || "").replace(/\/+$/, "");
const privateKey = String(process.env.PRIVATE_KEY || "");
const accountAddress = String(process.env.ACCOUNT_ADDRESS || "").toLowerCase();
const deviceId = String(process.env.DEVICE_ID || "");

function canonicalValue(value) {
    if (Array.isArray(value)) return value.map(canonicalValue);
    if (value && typeof value === "object") {
        return Object.fromEntries(
            Object.keys(value).sort().map((key) => [key, canonicalValue(value[key])])
        );
    }
    return value;
}

function endpointUrl() {
    if (telemetryUrl.endsWith("/api/telemetry/events")) return telemetryUrl;
    return `${telemetryUrl}/api/telemetry/events`;
}

export async function emitTelemetryEvent(event, attributes = {}) {
    if (!telemetryUrl || !privateKey || !/^0x[0-9a-f]{40}$/i.test(accountAddress)) return;
    const payload = {
        account: accountAddress,
        attributes: canonicalValue(attributes),
        device_id: deviceId,
        event: String(event),
        nonce: crypto.randomUUID(),
        timestamp_unix_ms: Date.now(),
        version: 1,
    };
    const canonicalPayload = JSON.stringify(canonicalValue(payload));
    const signature = sign(canonicalPayload, privateKey).signature;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3000);
    try {
        const response = await fetch(endpointUrl(), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ payload, signature }),
            signal: controller.signal,
        });
        if (!response.ok) {
            console.warn(`Telemetry event ${event} was rejected with HTTP ${response.status}.`);
        }
    } catch (error) {
        console.warn(`Telemetry event ${event} could not be delivered:`, error?.message || String(error));
    } finally {
        clearTimeout(timeout);
    }
}
