import crypto from 'crypto';
const endpoint = process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT || "";
let warned = false;
const SpanKind = {
    INTERNAL: 1,
    SERVER: 2,
    CLIENT: 3,
};
function nowUnixNano() {
    return String(BigInt(Date.now()) * 1000000n);
}
function traceIdForRound(round) {
    return crypto
        .createHash('sha256')
        .update(`dfl-round:${round}`)
        .digest('hex')
        .slice(0, 32);
}
function spanId() {
    return crypto.randomBytes(8).toString('hex');
}
function workerServiceName() {
    const deviceId = String(process.env.DEVICE_ID || "").trim();
    return deviceId ? `dfl-worker-${deviceId}` : "dfl-worker";
}
function serviceNameForRole(role) {
    if (role === "aggregator")
        return "dfl-aggregator";
    if (role === "worker")
        return workerServiceName();
    return process.env.OTEL_SERVICE_NAME || "dfl-node";
}
function attributeValue(value) {
    if (typeof value === 'number' && Number.isFinite(value)) {
        return Number.isInteger(value)
            ? { intValue: String(value) }
            : { doubleValue: value };
    }
    if (typeof value === 'boolean')
        return { boolValue: value };
    return { stringValue: String(value ?? "") };
}
function attributes(values) {
    return Object.entries(values)
        .filter(([, value]) => value !== undefined && value !== null)
        .map(([key, value]) => ({
        key,
        value: attributeValue(value),
    }));
}
async function exportSpan(span, resourceAttributes = {}) {
    if (!endpoint)
        return;
    const payload = {
        resourceSpans: [
            {
                resource: {
                    attributes: attributes({
                        "service.name": process.env.OTEL_SERVICE_NAME || "dfl-node",
                        "deployment.environment": process.env.DOCKER || "local",
                        "dfl.account": process.env.ACCOUNT_ADDRESS || "",
                        "dfl.device_id": process.env.DEVICE_ID || "",
                        ...resourceAttributes,
                    }),
                },
                scopeSpans: [
                    {
                        scope: {
                            name: "dfl-node-state-machine",
                            version: "1.0.0",
                        },
                        spans: [span],
                    },
                ],
            },
        ],
    };
    try {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 1000);
        const response = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            signal: controller.signal,
        });
        clearTimeout(timeout);
        if (!response.ok && !warned) {
            warned = true;
            console.warn(`OTel trace export failed (${response.status})`);
        }
    }
    catch (error) {
        if (!warned) {
            warned = true;
            console.warn("OTel trace export failed:", error);
        }
    }
}
async function exportPairedServiceEdge({ round, source, target, name, startTimeMs, endTimeMs, status = "OK", spanAttributes = {}, }) {
    const start = Number.isFinite(startTimeMs) ? Number(startTimeMs) : Date.now();
    const end = Number.isFinite(endTimeMs) ? Number(endTimeMs) : Date.now();
    const traceId = traceIdForRound(round);
    const clientSpanId = spanId();
    const serverSpanId = spanId();
    const startTimeUnixNano = String(BigInt(Math.floor(start)) * 1000000n);
    const endTimeUnixNano = String(BigInt(Math.floor(Math.max(end, start))) * 1000000n);
    const baseAttributes = {
        "dfl.round": round,
        "dfl.synthetic_edge": true,
        "dfl.duration_ms": Math.max(0, Math.floor(end - start)),
        ...spanAttributes,
    };
    await exportSpan({
        traceId,
        spanId: clientSpanId,
        name,
        kind: SpanKind.CLIENT,
        startTimeUnixNano,
        endTimeUnixNano,
        attributes: attributes({
            ...baseAttributes,
            "peer.service": target,
        }),
        status: {
            code: status === "ERROR" ? 2 : 1,
        },
    }, {
        "service.name": source,
    });
    await exportSpan({
        traceId,
        spanId: serverSpanId,
        parentSpanId: clientSpanId,
        name,
        kind: SpanKind.SERVER,
        startTimeUnixNano,
        endTimeUnixNano,
        attributes: attributes(baseAttributes),
        status: {
            code: status === "ERROR" ? 2 : 1,
        },
    }, {
        "service.name": target,
    });
}
async function recordWorkflowEdge(name, options) {
    const worker = workerServiceName();
    const workflowEdges = {
        "worker.fetch_global_model": [
            [worker, "smart-contracts"],
            [worker, "ipfs-kubo"],
        ],
        "worker.training": [[worker, "python-ml-service"]],
        "worker.model_transfer": [[worker, "dfl-aggregator"]],
        "worker.submit_model": [[worker, "smart-contracts"]],
        "worker.set_contribution": [[worker, "smart-contracts"]],
        "aggregator.wait_for_models": [["dfl-aggregator", "dfl-workers"]],
        "aggregator.aggregation": [["dfl-aggregator", "python-ml-service"]],
        "aggregator.update_global_model": [
            ["dfl-aggregator", "ipfs-kubo"],
            ["dfl-aggregator", "smart-contracts"],
        ],
        "aggregator.selection": [["dfl-aggregator", "smart-contracts"]],
    };
    const edges = workflowEdges[name];
    if (!edges)
        return;
    for (const [source, target] of edges) {
        await exportPairedServiceEdge({
            ...options,
            source,
            target,
            name: `service.${name}`,
            spanAttributes: {
                "dfl.operation": name,
                ...options.attributes,
            },
        });
    }
}
export async function recordRoundEvent(name, { round = 0, role = "", attributes: eventAttributes = {} } = {}) {
    const timestamp = nowUnixNano();
    await exportSpan({
        traceId: traceIdForRound(round),
        spanId: spanId(),
        name,
        kind: SpanKind.INTERNAL,
        startTimeUnixNano: timestamp,
        endTimeUnixNano: timestamp,
        attributes: attributes({
            "dfl.round": round,
            "dfl.role": role,
            ...eventAttributes,
        }),
    }, {
        "service.name": serviceNameForRole(role),
    });
}
export async function recordRoundSpan(name, { round = 0, role = "", startTimeMs, endTimeMs, status = "OK", attributes: spanAttributes = {}, } = {}) {
    const start = Number.isFinite(startTimeMs) ? Number(startTimeMs) : Date.now();
    const end = Number.isFinite(endTimeMs) ? Number(endTimeMs) : Date.now();
    await exportSpan({
        traceId: traceIdForRound(round),
        spanId: spanId(),
        name,
        kind: SpanKind.INTERNAL,
        startTimeUnixNano: String(BigInt(Math.floor(start)) * 1000000n),
        endTimeUnixNano: String(BigInt(Math.floor(Math.max(end, start))) * 1000000n),
        attributes: attributes({
            "dfl.round": round,
            "dfl.role": role,
            "dfl.duration_ms": Math.max(0, Math.floor(end - start)),
            ...spanAttributes,
        }),
        status: {
            code: status === "ERROR" ? 2 : 1,
        },
    }, {
        "service.name": serviceNameForRole(role),
    });
    await recordWorkflowEdge(name, {
        round,
        startTimeMs: start,
        endTimeMs: end,
        status,
        attributes: spanAttributes,
    });
}
