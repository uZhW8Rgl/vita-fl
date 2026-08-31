// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Strict parser and policy hasher for the canonical dstack app_compose preimage.
/// @dev This accepts only the narrow digest-pinned DFL worker profiles and intentionally is not
///      a general JSON or YAML parser.
library AppComposeImage {
    bytes private constant DOCKER_COMPOSE_FIELD = "docker_compose_file";
    bytes private constant MANIFEST_VERSION_FIELD = "manifest_version";
    bytes private constant RUNNER_FIELD = "runner";
    bytes private constant NAME_FIELD = "name";
    bytes private constant PRE_LAUNCH_SCRIPT_FIELD = "pre_launch_script";
    bytes private constant ALLOWED_ENVS_FIELD = "allowed_envs";
    bytes private constant FEATURES_FIELD = "features";
    bytes private constant GATEWAY_ENABLED_FIELD = "gateway_enabled";
    bytes private constant KMS_ENABLED_FIELD = "kms_enabled";
    bytes private constant LOCAL_KEY_PROVIDER_ENABLED_FIELD = "local_key_provider_enabled";
    bytes private constant NO_INSTANCE_ID_FIELD = "no_instance_id";
    bytes private constant PUBLIC_LOGS_FIELD = "public_logs";
    bytes private constant PUBLIC_SYSINFO_FIELD = "public_sysinfo";
    bytes private constant PUBLIC_TCBINFO_FIELD = "public_tcbinfo";
    bytes private constant SECURE_TIME_FIELD = "secure_time";
    bytes private constant STORAGE_FS_FIELD = "storage_fs";
    bytes private constant TPROXY_ENABLED_FIELD = "tproxy_enabled";
    bytes private constant DOCKER_COMPOSE_RUNNER = "docker-compose";
    bytes private constant FEATURE_KMS = "kms";
    bytes private constant FEATURE_TPROXY_NET = "tproxy-net";
    bytes private constant ENV_PRIVATE_KEY = "PRIVATE_KEY";
    bytes private constant ENV_SELLO_TOKEN_ISSUER_PUBLIC_KEY = "SELLO_TOKEN_ISSUER_PUBLIC_KEY";

    uint8 private constant OUTER_DOCKER_COMPOSE = 1;
    uint8 private constant OUTER_MANIFEST_VERSION = 2;
    uint8 private constant OUTER_RUNNER = 3;
    uint8 private constant OUTER_NAME = 4;
    uint8 private constant OUTER_PRE_LAUNCH_SCRIPT = 5;
    uint8 private constant OUTER_ALLOWED_ENVS = 6;
    uint8 private constant OUTER_FEATURES = 7;
    uint8 private constant OUTER_GATEWAY_ENABLED = 8;
    uint8 private constant OUTER_KMS_ENABLED = 9;
    uint8 private constant OUTER_LOCAL_KEY_PROVIDER_ENABLED = 10;
    uint8 private constant OUTER_NO_INSTANCE_ID = 11;
    uint8 private constant OUTER_PUBLIC_LOGS = 12;
    uint8 private constant OUTER_PUBLIC_SYSINFO = 13;
    uint8 private constant OUTER_PUBLIC_TCBINFO = 14;
    uint8 private constant OUTER_SECURE_TIME = 15;
    uint8 private constant OUTER_STORAGE_FS = 16;
    uint8 private constant OUTER_TPROXY_ENABLED = 17;

    // SHA-256 of the decoded Phala Cloud platform pre-launch script v0.0.19
    // exported in phala/app_code.txt. User-supplied pre-launch code is rejected.
    bytes32 private constant PHALA_PLATFORM_PRE_LAUNCH_V0_0_19 =
        0xcec8f68ce6185b912023d886bba20cd06386dd9751af904e6107e758b9d68983;
    bytes private constant SERVICES = "services:";
    bytes private constant WORKER_SERVICE = "dfl-worker:";
    bytes private constant PARTICIPANT_KEY_VOLUME = "participant-key-state:";
    bytes private constant IMAGE_KEY = "image:";
    bytes private constant RESTART_KEY = "restart:";
    bytes private constant SHA256_MARKER = "@sha256:";

    bytes private constant ENTRYPOINT_KEY = "entrypoint:";
    bytes private constant COMMAND_KEY = "command:";
    bytes private constant USER_KEY = "user:";
    bytes private constant VOLUMES_KEY = "volumes:";
    bytes private constant TMPFS_KEY = "tmpfs:";
    bytes private constant DEVICES_KEY = "devices:";
    bytes private constant CAP_ADD_KEY = "cap_add:";
    bytes private constant CAP_DROP_KEY = "cap_drop:";
    bytes private constant SECURITY_OPT_KEY = "security_opt:";
    bytes private constant READ_ONLY_KEY = "read_only:";
    bytes private constant PRIVILEGED_KEY = "privileged:";
    bytes private constant NETWORK_MODE_KEY = "network_mode:";
    bytes private constant NETWORKS_KEY = "networks:";
    bytes private constant PORTS_KEY = "ports:";
    bytes private constant EXPOSE_KEY = "expose:";
    bytes private constant ENVIRONMENT_KEY = "environment:";
    bytes private constant YAML_MERGE_KEY = "<<:";
    bytes private constant DSTACK_SOCKET_PATH = "/var/run/dstack.sock";
    bytes private constant TAPPD_SOCKET_PATH = "/var/run/tappd.sock";
    bytes private constant VARIABLE_ENVIRONMENT_KEYS = "|ACCOUNT_ADDRESS|DEVICE_ID|PUBLIC_IP|MSG_BROKER_IP|PRIVATE_KEY|KUBO_API|KUBO_GATEWAY|DSTACK_GATEWAY_DOMAIN|REGISTRY_ADDRESS|AGGREGATOR_ADDRESS|GM_STORAGE_ADDRESS|MEDICAL_SIGNER_REGISTRY_ADDRESS|DFL_TELEMETRY_URL|SCITT_URL|CLIENT_LIMIT|EPOCH|ROUND|MODEL_SUBMISSION_DEADLINE_MS|GM_UPDATE_TIMEOUT_MS|GM_UPDATE_TIMEOUT_LOOPS|AGGREGATION_UPDATE_ESTIMATE_MS|GM_UPDATE_POLL_MS|WORKER_COUNT|DATASET_NAME|TRAIN_IMAGES_SRC|TRAIN_LABELS_SRC|TEST_IMAGES_SRC|TEST_LABELS_SRC|TRAIN_DATA_SRC|TEST_DATA_SRC|SELLO_TOKEN_ISSUER_PUBLIC_KEY|";

    uint8 private constant FIELD_ENTRYPOINT = 1;
    uint8 private constant FIELD_COMMAND = 2;
    uint8 private constant FIELD_USER = 3;
    uint8 private constant FIELD_VOLUMES = 4;
    uint8 private constant FIELD_TMPFS = 5;
    uint8 private constant FIELD_DEVICES = 6;
    uint8 private constant FIELD_CAP_ADD = 7;
    uint8 private constant FIELD_CAP_DROP = 8;
    uint8 private constant FIELD_SECURITY_OPT = 9;
    uint8 private constant FIELD_READ_ONLY = 10;
    uint8 private constant FIELD_PRIVILEGED = 11;
    uint8 private constant FIELD_NETWORK_MODE = 12;
    uint8 private constant FIELD_NETWORKS = 13;
    uint8 private constant FIELD_PORTS = 14;
    uint8 private constant FIELD_EXPOSE = 15;
    uint8 private constant FIELD_ENVIRONMENT = 16;
    uint8 private constant POLICY_FIELD_COUNT = 15;
    uint16 private constant MAX_ENVIRONMENT_KEYS = 128;

    bytes32 private constant DOCKER_WORKER_POLICY_DOMAIN =
        keccak256("MasterThesis.AppCompose.worker-policy.v1");
    bytes32 private constant WORKER_POLICY_DOMAIN =
        keccak256("MasterThesis.AppCompose.worker-policy.v2");
    bytes32 private constant OUTER_MANIFEST_POLICY_DOMAIN =
        keccak256("MasterThesis.AppCompose.outer-manifest-policy.v2");
    bytes32 private constant ENTRYPOINT_DOMAIN = keccak256("entrypoint");
    bytes32 private constant COMMAND_DOMAIN = keccak256("command");
    bytes32 private constant USER_DOMAIN = keccak256("user");
    bytes32 private constant VOLUMES_DOMAIN = keccak256("volumes");
    bytes32 private constant TMPFS_DOMAIN = keccak256("tmpfs");
    bytes32 private constant DEVICES_DOMAIN = keccak256("devices");
    bytes32 private constant CAP_ADD_DOMAIN = keccak256("cap_add");
    bytes32 private constant CAP_DROP_DOMAIN = keccak256("cap_drop");
    bytes32 private constant SECURITY_OPT_DOMAIN = keccak256("security_opt");
    bytes32 private constant READ_ONLY_DOMAIN = keccak256("read_only");
    bytes32 private constant PRIVILEGED_DOMAIN = keccak256("privileged");
    bytes32 private constant NETWORK_MODE_DOMAIN = keccak256("network_mode");
    bytes32 private constant NETWORKS_DOMAIN = keccak256("networks");
    bytes32 private constant PORTS_DOMAIN = keccak256("ports");
    bytes32 private constant EXPOSE_DOMAIN = keccak256("expose");
    bytes32 private constant VOLUME_POLICY_DOMAIN = keccak256("volume-policy");
    bytes32 private constant CAPABILITIES_POLICY_DOMAIN = keccak256("capabilities-policy");
    bytes32 private constant SECURITY_OPTIONS_POLICY_DOMAIN = keccak256("security-options-policy");
    bytes32 private constant NETWORK_POLICY_DOMAIN = keccak256("network-policy");
    bytes32 private constant DSTACK_SOCKET_POLICY_DOMAIN = keccak256("dstack-socket-policy");
    bytes32 private constant ENVIRONMENT_POLICY_DOMAIN = keccak256("environment-safety-policy");
    bytes32 private constant VARIABLE_ENVIRONMENT_DOMAIN = keccak256("variable-environment-entry");
    bytes32 private constant FIXED_ENVIRONMENT_DOMAIN = keccak256("fixed-environment-entry");

    struct PolicyAccumulator {
        bytes32[POLICY_FIELD_COUNT] chains;
        uint32[POLICY_FIELD_COUNT] counts;
        uint256 seenFields;
        uint8 activeField;
        bytes32 dstackSocketChain;
        uint32 dstackSocketCount;
        bytes32 environmentXor;
        uint16 environmentCount;
        bytes32[MAX_ENVIRONMENT_KEYS] environmentKeys;
        bool environmentSeen;
        bool restartSeen;
    }

    struct OuterManifest {
        uint256 seenFields;
        bytes dockerCompose;
    }

    function imageDigest(bytes memory canonicalAppCompose) internal pure returns (bytes32) {
        (bytes32 digest,) = identity(canonicalAppCompose);
        return digest;
    }

    function workerPolicyHash(bytes memory canonicalAppCompose) internal pure returns (bytes32) {
        (, bytes32 policyHash) = identity(canonicalAppCompose);
        return policyHash;
    }

    function identity(bytes memory canonicalAppCompose)
        internal
        pure
        returns (bytes32 digest, bytes32 policyHash)
    {
        require(canonicalAppCompose.length != 0 && canonicalAppCompose.length <= 65_536, "invalid app compose size");
        OuterManifest memory manifest = _parseOuterManifest(canonicalAppCompose);
        bytes32 dockerPolicyHash;
        (digest, dockerPolicyHash) = _parseWorkerCompose(manifest.dockerCompose);
        policyHash = keccak256(
            abi.encode(
                WORKER_POLICY_DOMAIN,
                dockerPolicyHash,
                keccak256(
                    abi.encode(
                        OUTER_MANIFEST_POLICY_DOMAIN,
                        uint256(2),
                        keccak256(DOCKER_COMPOSE_RUNNER),
                        PHALA_PLATFORM_PRE_LAUNCH_V0_0_19
                    )
                )
            )
        );
    }

    function _parseOuterManifest(bytes memory input)
        private
        pure
        returns (OuterManifest memory manifest)
    {
        uint256 cursor = _skipJsonWhitespace(input, 0);
        require(cursor < input.length && input[cursor] == "{", "app compose must be a JSON object");
        cursor++;

        bool first = true;
        while (true) {
            cursor = _skipJsonWhitespace(input, cursor);
            require(cursor < input.length, "unterminated app compose object");
            if (input[cursor] == "}") {
                cursor++;
                break;
            }
            if (!first) {
                require(input[cursor] == ",", "invalid app compose separator");
                cursor = _skipJsonWhitespace(input, cursor + 1);
            }
            first = false;

            bytes memory key;
            (key, cursor) = _decodeJsonString(input, cursor);
            uint8 field = _outerField(key);
            require(field != 0, "unknown outer app compose field");
            uint256 fieldBit = uint256(1) << (field - 1);
            require((manifest.seenFields & fieldBit) == 0, "outer app compose field duplicated");
            manifest.seenFields |= fieldBit;

            cursor = _skipJsonWhitespace(input, cursor);
            require(cursor < input.length && input[cursor] == ":", "invalid app compose field");
            cursor = _skipJsonWhitespace(input, cursor + 1);
            cursor = _consumeOuterField(input, cursor, field, manifest);
            cursor = _skipJsonWhitespace(input, cursor);
            require(
                cursor < input.length && (input[cursor] == "," || input[cursor] == "}"),
                "invalid outer app compose value"
            );
        }

        cursor = _skipJsonWhitespace(input, cursor);
        require(cursor == input.length, "trailing app compose data");
        uint256 required = (uint256(1) << (OUTER_DOCKER_COMPOSE - 1))
            | (uint256(1) << (OUTER_MANIFEST_VERSION - 1))
            | (uint256(1) << (OUTER_RUNNER - 1));
        require((manifest.seenFields & required) == required, "required outer app compose field missing");
    }

    function _consumeOuterField(
        bytes memory input,
        uint256 cursor,
        uint8 field,
        OuterManifest memory manifest
    ) private pure returns (uint256 next) {
        if (field == OUTER_DOCKER_COMPOSE) {
            (manifest.dockerCompose, next) = _decodeJsonString(input, cursor);
            require(manifest.dockerCompose.length != 0, "empty docker compose field");
            return next;
        }
        if (field == OUTER_MANIFEST_VERSION) {
            return _consumeJsonLiteral(input, cursor, "2", "manifest_version must equal 2");
        }
        if (field == OUTER_RUNNER) {
            bytes memory runner;
            (runner, next) = _decodeJsonString(input, cursor);
            require(_bytesEqual(runner, DOCKER_COMPOSE_RUNNER), "runner must be docker-compose");
            return next;
        }
        if (field == OUTER_NAME) {
            bytes memory ignored;
            (ignored, next) = _decodeJsonString(input, cursor);
            require(ignored.length <= 256, "app compose name too long");
            return next;
        }
        if (field == OUTER_PRE_LAUNCH_SCRIPT) {
            bytes memory script;
            (script, next) = _decodeJsonString(input, cursor);
            require(
                script.length == 0 || sha256(script) == PHALA_PLATFORM_PRE_LAUNCH_V0_0_19,
                "user pre-launch script not allowed"
            );
            return next;
        }
        if (field == OUTER_ALLOWED_ENVS) {
            return _consumeAllowedEnvironmentArray(input, cursor);
        }
        if (field == OUTER_FEATURES) {
            return _consumeFeatureArray(input, cursor);
        }
        if (field == OUTER_KMS_ENABLED || field == OUTER_TPROXY_ENABLED) {
            return _consumeJsonLiteral(input, cursor, "true", "required outer feature disabled");
        }
        if (field == OUTER_LOCAL_KEY_PROVIDER_ENABLED || field == OUTER_NO_INSTANCE_ID) {
            return _consumeJsonLiteral(input, cursor, "false", "unsafe outer app compose setting");
        }
        if (
            field == OUTER_GATEWAY_ENABLED || field == OUTER_PUBLIC_LOGS
                || field == OUTER_PUBLIC_SYSINFO || field == OUTER_PUBLIC_TCBINFO
                || field == OUTER_SECURE_TIME
        ) {
            return _consumeJsonBoolean(input, cursor);
        }
        if (field == OUTER_STORAGE_FS) {
            if (_literalAt(input, cursor, "null")) {
                return _consumeJsonLiteral(input, cursor, "null", "invalid storage_fs");
            }
            bytes memory storageFs;
            (storageFs, next) = _decodeJsonString(input, cursor);
            require(
                _bytesEqual(storageFs, "zfs") || _bytesEqual(storageFs, "ext4"),
                "unsupported storage_fs"
            );
            return next;
        }
        revert("unsupported outer app compose field");
    }

    function _outerField(bytes memory key) private pure returns (uint8) {
        if (_bytesEqual(key, DOCKER_COMPOSE_FIELD)) return OUTER_DOCKER_COMPOSE;
        if (_bytesEqual(key, MANIFEST_VERSION_FIELD)) return OUTER_MANIFEST_VERSION;
        if (_bytesEqual(key, RUNNER_FIELD)) return OUTER_RUNNER;
        if (_bytesEqual(key, NAME_FIELD)) return OUTER_NAME;
        if (_bytesEqual(key, PRE_LAUNCH_SCRIPT_FIELD)) return OUTER_PRE_LAUNCH_SCRIPT;
        if (_bytesEqual(key, ALLOWED_ENVS_FIELD)) return OUTER_ALLOWED_ENVS;
        if (_bytesEqual(key, FEATURES_FIELD)) return OUTER_FEATURES;
        if (_bytesEqual(key, GATEWAY_ENABLED_FIELD)) return OUTER_GATEWAY_ENABLED;
        if (_bytesEqual(key, KMS_ENABLED_FIELD)) return OUTER_KMS_ENABLED;
        if (_bytesEqual(key, LOCAL_KEY_PROVIDER_ENABLED_FIELD)) return OUTER_LOCAL_KEY_PROVIDER_ENABLED;
        if (_bytesEqual(key, NO_INSTANCE_ID_FIELD)) return OUTER_NO_INSTANCE_ID;
        if (_bytesEqual(key, PUBLIC_LOGS_FIELD)) return OUTER_PUBLIC_LOGS;
        if (_bytesEqual(key, PUBLIC_SYSINFO_FIELD)) return OUTER_PUBLIC_SYSINFO;
        if (_bytesEqual(key, PUBLIC_TCBINFO_FIELD)) return OUTER_PUBLIC_TCBINFO;
        if (_bytesEqual(key, SECURE_TIME_FIELD)) return OUTER_SECURE_TIME;
        if (_bytesEqual(key, STORAGE_FS_FIELD)) return OUTER_STORAGE_FS;
        if (_bytesEqual(key, TPROXY_ENABLED_FIELD)) return OUTER_TPROXY_ENABLED;
        return 0;
    }

    function _consumeAllowedEnvironmentArray(bytes memory input, uint256 cursor)
        private
        pure
        returns (uint256)
    {
        require(cursor < input.length && input[cursor] == "[", "allowed_envs must be an array");
        cursor = _skipJsonWhitespace(input, cursor + 1);
        uint256 seen;
        bool first = true;
        while (cursor < input.length && input[cursor] != "]") {
            if (!first) {
                require(input[cursor] == ",", "invalid allowed_envs separator");
                cursor = _skipJsonWhitespace(input, cursor + 1);
            }
            first = false;
            bytes memory key;
            (key, cursor) = _decodeJsonString(input, cursor);
            uint256 bit;
            if (_bytesEqual(key, ENV_PRIVATE_KEY)) bit = 1;
            else if (_bytesEqual(key, ENV_SELLO_TOKEN_ISSUER_PUBLIC_KEY)) bit = 2;
            else revert("unsafe outer environment key");
            require((seen & bit) == 0, "outer environment key duplicated");
            seen |= bit;
            cursor = _skipJsonWhitespace(input, cursor);
        }
        require(cursor < input.length && input[cursor] == "]", "unterminated allowed_envs");
        return cursor + 1;
    }

    function _consumeFeatureArray(bytes memory input, uint256 cursor)
        private
        pure
        returns (uint256)
    {
        require(cursor < input.length && input[cursor] == "[", "features must be an array");
        cursor = _skipJsonWhitespace(input, cursor + 1);
        uint256 seen;
        bool first = true;
        while (cursor < input.length && input[cursor] != "]") {
            if (!first) {
                require(input[cursor] == ",", "invalid features separator");
                cursor = _skipJsonWhitespace(input, cursor + 1);
            }
            first = false;
            bytes memory feature;
            (feature, cursor) = _decodeJsonString(input, cursor);
            uint256 bit;
            if (_bytesEqual(feature, FEATURE_KMS)) bit = 1;
            else if (_bytesEqual(feature, FEATURE_TPROXY_NET)) bit = 2;
            else revert("unsupported outer app feature");
            require((seen & bit) == 0, "outer app feature duplicated");
            seen |= bit;
            cursor = _skipJsonWhitespace(input, cursor);
        }
        require(cursor < input.length && input[cursor] == "]", "unterminated features");
        return cursor + 1;
    }

    function _consumeJsonBoolean(bytes memory input, uint256 cursor)
        private
        pure
        returns (uint256)
    {
        if (_literalAt(input, cursor, "true")) {
            return _consumeJsonLiteral(input, cursor, "true", "invalid outer boolean");
        }
        return _consumeJsonLiteral(input, cursor, "false", "invalid outer boolean");
    }

    function _consumeJsonLiteral(
        bytes memory input,
        uint256 cursor,
        bytes memory literal,
        string memory errorMessage
    ) private pure returns (uint256 next) {
        require(_literalAt(input, cursor, literal), errorMessage);
        next = cursor + literal.length;
        require(
            next == input.length || input[next] == "," || input[next] == "}"
                || input[next] == 0x20 || input[next] == 0x09
                || input[next] == 0x0a || input[next] == 0x0d,
            errorMessage
        );
    }

    function _literalAt(bytes memory input, uint256 cursor, bytes memory literal)
        private
        pure
        returns (bool)
    {
        return cursor + literal.length <= input.length && _matches(input, cursor, literal);
    }

    function _decodeJsonString(bytes memory input, uint256 cursor)
        private
        pure
        returns (bytes memory output, uint256 next)
    {
        require(cursor < input.length && input[cursor] == '"', "expected JSON string");
        output = new bytes(input.length - cursor - 1);
        uint256 outLength;
        for (uint256 i = cursor + 1; i < input.length; i++) {
            bytes1 current = input[i];
            if (current == '"') {
                assembly ("memory-safe") {
                    mstore(output, outLength)
                }
                return (output, i + 1);
            }
            require(uint8(current) >= 0x20, "invalid JSON string");
            if (current != "\\") {
                output[outLength++] = current;
                continue;
            }
            require(++i < input.length, "invalid JSON escape");
            bytes1 escaped = input[i];
            if (escaped == '"' || escaped == "\\" || escaped == "/") output[outLength++] = escaped;
            else if (escaped == "b") output[outLength++] = 0x08;
            else if (escaped == "f") output[outLength++] = 0x0c;
            else if (escaped == "n") output[outLength++] = 0x0a;
            else if (escaped == "r") output[outLength++] = 0x0d;
            else if (escaped == "t") output[outLength++] = 0x09;
            else revert("unsupported JSON escape");
        }
        revert("unterminated JSON string");
    }

    function _skipJsonWhitespace(bytes memory input, uint256 cursor)
        private
        pure
        returns (uint256)
    {
        while (
            cursor < input.length
                && (
                    input[cursor] == 0x20 || input[cursor] == 0x09
                        || input[cursor] == 0x0a || input[cursor] == 0x0d
                )
        ) cursor++;
        return cursor;
    }

    function _bytesEqual(bytes memory left, bytes memory right) private pure returns (bool) {
        if (left.length != right.length) return false;
        for (uint256 i = 0; i < left.length; i++) {
            if (left[i] != right[i]) return false;
        }
        return true;
    }

    function _parseWorkerCompose(bytes memory compose)
        private
        pure
        returns (bytes32 digest, bytes32 policyHash)
    {
        bool inServices;
        bool inWorker;
        bool servicesSeen;
        bool inTopLevelVolumes;
        bool topLevelVolumesSeen;
        uint256 serviceCount;
        uint256 workerCount;
        uint256 imageCount;
        uint256 topLevelVolumeCount;
        bytes memory imageReference;
        PolicyAccumulator memory policy;

        uint256 cursor;
        while (cursor <= compose.length) {
            uint256 lineEnd = cursor;
            while (lineEnd < compose.length && compose[lineEnd] != 0x0a) lineEnd++;
            (uint256 start, uint256 end, uint256 indent) = _lineBounds(compose, cursor, lineEnd);

            if (start < end && compose[start] != "#") {
                if (indent == 0) {
                    bool services = _equals(compose, start, end, SERVICES);
                    bool volumes = _equals(compose, start, end, VOLUMES_KEY);
                    require(services || volumes, "unknown top-level compose field");
                    if (services) {
                        require(!servicesSeen, "services field duplicated");
                        servicesSeen = true;
                    }
                    if (volumes) {
                        require(!topLevelVolumesSeen, "top-level volumes field duplicated");
                        topLevelVolumesSeen = true;
                    }
                    inServices = services;
                    inTopLevelVolumes = volumes;
                    inWorker = false;
                    policy.activeField = 0;
                } else if (inServices && indent == 2) {
                    // Every non-comment line at this level is a YAML service key
                    // (including flow-style values and merge keys). Count and
                    // reject it unless it is exactly the one supported worker.
                    serviceCount++;
                    bool worker = _equals(compose, start, end, WORKER_SERVICE);
                    if (worker) workerCount++;
                    inWorker = worker;
                    policy.activeField = 0;
                } else if (inTopLevelVolumes && indent == 2) {
                    topLevelVolumeCount++;
                    require(
                        _equals(compose, start, end, PARTICIPANT_KEY_VOLUME),
                        "unsupported top-level volume"
                    );
                } else if (inTopLevelVolumes && indent > 2) {
                    revert("top-level volume options not allowed");
                } else if (inWorker && indent == 4) {
                    policy.activeField = 0;
                    require(!_startsWith(compose, start, end, YAML_MERGE_KEY), "worker YAML merge not allowed");

                    if (_startsWith(compose, start, end, IMAGE_KEY)) {
                        imageCount++;
                        imageReference = _imageValue(compose, start + IMAGE_KEY.length, end);
                    } else if (_startsWith(compose, start, end, RESTART_KEY)) {
                        require(!policy.restartSeen, "worker restart field duplicated");
                        policy.restartSeen = true;
                    } else {
                        (uint8 field, uint256 keyLength) = _policyField(compose, start, end);
                        require(field != 0, "unknown worker service field");
                        uint256 fieldBit = uint256(1) << (field - 1);
                        require((policy.seenFields & fieldBit) == 0, "worker policy field duplicated");
                        policy.seenFields |= fieldBit;
                        policy.activeField = field;
                        if (field == FIELD_ENVIRONMENT) {
                            require(!policy.environmentSeen, "worker policy field duplicated");
                            policy.environmentSeen = true;
                            require(
                                _isEmptyPolicyValue(compose, start + keyLength, end),
                                "worker environment must use mapping form"
                            );
                        } else {
                            _appendPolicyRecord(policy, field, 0, compose, start + keyLength, end);
                        }
                    }
                } else if (inWorker && indent > 4 && policy.activeField != 0) {
                    if (policy.activeField == FIELD_ENVIRONMENT) {
                        require(indent == 6, "invalid worker environment indentation");
                        _appendEnvironmentRecord(policy, compose, start, end);
                    } else {
                        _appendPolicyRecord(policy, policy.activeField, indent - 4, compose, start, end);
                    }
                } else if (inWorker && indent > 4) {
                    revert("unexpected worker service nesting");
                }
            }

            if (lineEnd == compose.length) break;
            cursor = lineEnd + 1;
        }

        require(servicesSeen && serviceCount == 1 && workerCount == 1, "worker service missing or ambiguous");
        require(!topLevelVolumesSeen || topLevelVolumeCount == 1, "invalid top-level volumes");
        require(imageCount == 1, "worker image missing or ambiguous");
        digest = _digestFromReference(imageReference);
        policyHash = _finalizePolicy(digest, policy);
    }

    function _policyField(bytes memory input, uint256 start, uint256 end)
        private
        pure
        returns (uint8 field, uint256 keyLength)
    {
        if (_startsWith(input, start, end, ENTRYPOINT_KEY)) return (FIELD_ENTRYPOINT, ENTRYPOINT_KEY.length);
        if (_startsWith(input, start, end, COMMAND_KEY)) return (FIELD_COMMAND, COMMAND_KEY.length);
        if (_startsWith(input, start, end, USER_KEY)) return (FIELD_USER, USER_KEY.length);
        if (_startsWith(input, start, end, VOLUMES_KEY)) return (FIELD_VOLUMES, VOLUMES_KEY.length);
        if (_startsWith(input, start, end, TMPFS_KEY)) return (FIELD_TMPFS, TMPFS_KEY.length);
        if (_startsWith(input, start, end, DEVICES_KEY)) return (FIELD_DEVICES, DEVICES_KEY.length);
        if (_startsWith(input, start, end, CAP_ADD_KEY)) return (FIELD_CAP_ADD, CAP_ADD_KEY.length);
        if (_startsWith(input, start, end, CAP_DROP_KEY)) return (FIELD_CAP_DROP, CAP_DROP_KEY.length);
        if (_startsWith(input, start, end, SECURITY_OPT_KEY)) return (FIELD_SECURITY_OPT, SECURITY_OPT_KEY.length);
        if (_startsWith(input, start, end, READ_ONLY_KEY)) return (FIELD_READ_ONLY, READ_ONLY_KEY.length);
        if (_startsWith(input, start, end, PRIVILEGED_KEY)) return (FIELD_PRIVILEGED, PRIVILEGED_KEY.length);
        if (_startsWith(input, start, end, NETWORK_MODE_KEY)) return (FIELD_NETWORK_MODE, NETWORK_MODE_KEY.length);
        if (_startsWith(input, start, end, NETWORKS_KEY)) return (FIELD_NETWORKS, NETWORKS_KEY.length);
        if (_startsWith(input, start, end, PORTS_KEY)) return (FIELD_PORTS, PORTS_KEY.length);
        if (_startsWith(input, start, end, EXPOSE_KEY)) return (FIELD_EXPOSE, EXPOSE_KEY.length);
        if (_startsWith(input, start, end, ENVIRONMENT_KEY)) return (FIELD_ENVIRONMENT, ENVIRONMENT_KEY.length);
        return (0, 0);
    }

    function _appendEnvironmentRecord(
        PolicyAccumulator memory policy,
        bytes memory input,
        uint256 start,
        uint256 end
    ) private pure {
        while (end > start && (input[end - 1] == 0x20 || input[end - 1] == 0x0d)) end--;
        _validatePolicyRecord(input, start, end);

        uint256 separator = type(uint256).max;
        for (uint256 i = start; i < end; i++) {
            if (input[i] == ":") {
                separator = i;
                break;
            }
        }
        require(separator != type(uint256).max && separator > start, "invalid worker environment entry");

        for (uint256 i = start; i < separator; i++) {
            uint8 character = uint8(input[i]);
            bool valid = (character >= 65 && character <= 90) || character == 95
                || (i > start && character >= 48 && character <= 57);
            require(valid, "invalid worker environment key");
        }

        uint256 valueStart = separator + 1;
        while (valueStart < end && input[valueStart] == 0x20) valueStart++;
        bytes32 keyHash = _hashSlice(input, start, separator);
        require(policy.environmentCount < MAX_ENVIRONMENT_KEYS, "too many worker environment entries");
        for (uint256 i = 0; i < policy.environmentCount; i++) {
            require(policy.environmentKeys[i] != keyHash, "worker environment key duplicated");
        }
        policy.environmentKeys[policy.environmentCount] = keyHash;

        bytes32 entryHash;
        if (_isVariableEnvironmentKey(input, start, separator)) {
            entryHash = keccak256(abi.encode(VARIABLE_ENVIRONMENT_DOMAIN, keyHash));
        } else {
            entryHash = keccak256(
                abi.encode(FIXED_ENVIRONMENT_DOMAIN, keyHash, _hashSlice(input, valueStart, end))
            );
        }
        policy.environmentXor ^= entryHash;
        policy.environmentCount++;
    }

    function _isVariableEnvironmentKey(bytes memory input, uint256 start, uint256 end)
        private
        pure
        returns (bool)
    {
        uint256 keyLength = end - start;
        bytes memory keys = VARIABLE_ENVIRONMENT_KEYS;
        for (uint256 cursor = 0; cursor + keyLength + 2 <= keys.length; cursor++) {
            if (keys[cursor] != "|") continue;
            if (keys[cursor + keyLength + 1] != "|") continue;
            bool equal = true;
            for (uint256 i = 0; i < keyLength; i++) {
                if (keys[cursor + 1 + i] != input[start + i]) {
                    equal = false;
                    break;
                }
            }
            if (equal) return true;
        }
        return false;
    }

    function _isEmptyPolicyValue(bytes memory input, uint256 start, uint256 end)
        private
        pure
        returns (bool)
    {
        while (start < end && input[start] == 0x20) start++;
        while (end > start && (input[end - 1] == 0x20 || input[end - 1] == 0x0d)) end--;
        return start == end;
    }

    function _appendPolicyRecord(
        PolicyAccumulator memory policy,
        uint8 field,
        uint256 relativeIndent,
        bytes memory input,
        uint256 start,
        uint256 end
    ) private pure {
        while (start < end && (input[start] == 0x20 || input[start] == 0x09)) start++;
        while (end > start && (input[end - 1] == 0x20 || input[end - 1] == 0x09 || input[end - 1] == 0x0d)) {
            end--;
        }
        _validatePolicyRecord(input, start, end);

        bytes32 contentHash = _hashSlice(input, start, end);
        bytes32 recordHash = keccak256(abi.encode(uint16(relativeIndent), contentHash));
        uint256 index = field - 1;
        policy.chains[index] = keccak256(abi.encode(policy.chains[index], recordHash));
        policy.counts[index]++;

        if (
            field == FIELD_VOLUMES
                && (
                    _contains(input, start, end, DSTACK_SOCKET_PATH)
                        || _contains(input, start, end, TAPPD_SOCKET_PATH)
                )
        ) {
            policy.dstackSocketChain = keccak256(abi.encode(policy.dstackSocketChain, recordHash));
            policy.dstackSocketCount++;
        }
    }

    function _validatePolicyRecord(bytes memory input, uint256 start, uint256 end) private pure {
        for (uint256 i = start; i < end; i++) {
            bytes1 current = input[i];
            require(
                current != "#" && current != "&" && current != "*" && current != 0x09,
                "unsupported worker policy YAML"
            );
        }
    }

    function _finalizePolicy(bytes32 digest, PolicyAccumulator memory policy)
        private
        pure
        returns (bytes32)
    {
        bytes32 entrypoint = _fieldHash(policy, FIELD_ENTRYPOINT, ENTRYPOINT_DOMAIN);
        bytes32 command = _fieldHash(policy, FIELD_COMMAND, COMMAND_DOMAIN);
        bytes32 user = _fieldHash(policy, FIELD_USER, USER_DOMAIN);
        bytes32 volumes = keccak256(
            abi.encode(
                VOLUME_POLICY_DOMAIN,
                _fieldHash(policy, FIELD_VOLUMES, VOLUMES_DOMAIN),
                _fieldHash(policy, FIELD_TMPFS, TMPFS_DOMAIN),
                _fieldHash(policy, FIELD_DEVICES, DEVICES_DOMAIN)
            )
        );
        bytes32 capabilities = keccak256(
            abi.encode(
                CAPABILITIES_POLICY_DOMAIN,
                _fieldHash(policy, FIELD_CAP_ADD, CAP_ADD_DOMAIN),
                _fieldHash(policy, FIELD_CAP_DROP, CAP_DROP_DOMAIN)
            )
        );
        bytes32 securityOptions = keccak256(
            abi.encode(
                SECURITY_OPTIONS_POLICY_DOMAIN,
                _fieldHash(policy, FIELD_SECURITY_OPT, SECURITY_OPT_DOMAIN),
                _fieldHash(policy, FIELD_READ_ONLY, READ_ONLY_DOMAIN),
                _fieldHash(policy, FIELD_PRIVILEGED, PRIVILEGED_DOMAIN),
                keccak256(
                    abi.encode(
                        ENVIRONMENT_POLICY_DOMAIN,
                        policy.environmentXor,
                        policy.environmentCount
                    )
                )
            )
        );
        bytes32 networkPolicy = keccak256(
            abi.encode(
                NETWORK_POLICY_DOMAIN,
                _fieldHash(policy, FIELD_NETWORK_MODE, NETWORK_MODE_DOMAIN),
                _fieldHash(policy, FIELD_NETWORKS, NETWORKS_DOMAIN),
                _fieldHash(policy, FIELD_PORTS, PORTS_DOMAIN),
                _fieldHash(policy, FIELD_EXPOSE, EXPOSE_DOMAIN)
            )
        );
        bytes32 dstackSocketPolicy =
            keccak256(abi.encode(DSTACK_SOCKET_POLICY_DOMAIN, policy.dstackSocketChain, policy.dstackSocketCount));

        return keccak256(
            abi.encode(
                DOCKER_WORKER_POLICY_DOMAIN,
                digest,
                entrypoint,
                command,
                user,
                volumes,
                capabilities,
                securityOptions,
                networkPolicy,
                dstackSocketPolicy
            )
        );
    }

    function _fieldHash(PolicyAccumulator memory policy, uint8 field, bytes32 domain)
        private
        pure
        returns (bytes32)
    {
        uint256 index = field - 1;
        return keccak256(abi.encode(domain, policy.chains[index], policy.counts[index]));
    }

    function _imageValue(bytes memory input, uint256 start, uint256 end) private pure returns (bytes memory value) {
        while (start < end && (input[start] == 0x20 || input[start] == 0x09)) start++;
        while (end > start && (input[end - 1] == 0x20 || input[end - 1] == 0x09 || input[end - 1] == 0x0d)) end--;
        require(start < end, "empty worker image");

        bytes1 quote;
        if (input[start] == '"' || input[start] == "'") {
            quote = input[start++];
            require(end > start && input[end - 1] == quote, "invalid worker image quoting");
            end--;
        }
        for (uint256 i = start; i < end; i++) {
            require(input[i] != "#" && input[i] != 0x20 && input[i] != 0x09, "invalid worker image value");
        }
        value = _slice(input, start, end);
    }

    function _digestFromReference(bytes memory imageReference) private pure returns (bytes32 digest) {
        uint256 markerOffset = type(uint256).max;
        uint256 markerCount;
        for (uint256 i = 1; i + SHA256_MARKER.length <= imageReference.length; i++) {
            if (_matches(imageReference, i, SHA256_MARKER)) {
                markerOffset = i + SHA256_MARKER.length;
                markerCount++;
            }
        }
        require(markerCount == 1 && markerOffset + 64 == imageReference.length, "worker image must be digest-pinned");

        uint256 value;
        for (uint256 i = markerOffset; i < imageReference.length; i++) {
            uint8 character = uint8(imageReference[i]);
            uint8 nibble;
            if (character >= 48 && character <= 57) nibble = character - 48;
            else if (character >= 97 && character <= 102) nibble = character - 87;
            else revert("invalid worker image digest");
            value = (value << 4) | nibble;
        }
        digest = bytes32(value);
        require(digest != bytes32(0), "invalid worker image digest");
    }

    function _lineBounds(bytes memory input, uint256 start, uint256 end)
        private
        pure
        returns (uint256 contentStart, uint256 contentEnd, uint256 indent)
    {
        contentStart = start;
        while (contentStart < end && input[contentStart] == 0x20) {
            contentStart++;
            indent++;
        }
        contentEnd = end;
        while (contentEnd > contentStart && (input[contentEnd - 1] == 0x20 || input[contentEnd - 1] == 0x0d)) {
            contentEnd--;
        }
    }

    function _startsWith(bytes memory input, uint256 start, uint256 end, bytes memory prefix)
        private
        pure
        returns (bool)
    {
        return start + prefix.length <= end && _matches(input, start, prefix);
    }

    function _equals(bytes memory input, uint256 start, uint256 end, bytes memory expected)
        private
        pure
        returns (bool)
    {
        return end - start == expected.length && _matches(input, start, expected);
    }

    function _matches(bytes memory input, uint256 offset, bytes memory expected) private pure returns (bool) {
        if (offset + expected.length > input.length) return false;
        for (uint256 i = 0; i < expected.length; i++) {
            if (input[offset + i] != expected[i]) return false;
        }
        return true;
    }

    function _contains(bytes memory input, uint256 start, uint256 end, bytes memory expected)
        private
        pure
        returns (bool)
    {
        if (expected.length == 0 || end - start < expected.length) return false;
        for (uint256 i = start; i + expected.length <= end; i++) {
            if (_matches(input, i, expected)) return true;
        }
        return false;
    }

    function _hashSlice(bytes memory input, uint256 start, uint256 end) private pure returns (bytes32 digest) {
        assembly ("memory-safe") {
            digest := keccak256(add(add(input, 0x20), start), sub(end, start))
        }
    }

    function _slice(bytes memory input, uint256 start, uint256 end) private pure returns (bytes memory output) {
        output = new bytes(end - start);
        for (uint256 i = 0; i < output.length; i++) output[i] = input[start + i];
    }
}

interface IAppComposePolicy {
    function imageDigest(bytes calldata canonicalAppCompose) external pure returns (bytes32);

    function workerPolicyHash(bytes calldata canonicalAppCompose) external pure returns (bytes32);

    function identity(bytes calldata canonicalAppCompose)
        external
        pure
        returns (bytes32 digest, bytes32 policyHash);
}

/// @notice Stateless, separately deployed parser that keeps the strict app-compose
///         policy bytecode out of DeviceRegistry's EIP-170-limited runtime.
contract AppComposePolicy is IAppComposePolicy {
    function imageDigest(bytes calldata canonicalAppCompose) external pure returns (bytes32) {
        return AppComposeImage.imageDigest(canonicalAppCompose);
    }

    function workerPolicyHash(bytes calldata canonicalAppCompose) external pure returns (bytes32) {
        return AppComposeImage.workerPolicyHash(canonicalAppCompose);
    }

    function identity(bytes calldata canonicalAppCompose)
        external
        pure
        returns (bytes32 digest, bytes32 policyHash)
    {
        return AppComposeImage.identity(canonicalAppCompose);
    }
}
