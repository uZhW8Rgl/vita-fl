// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Strict parser for the canonical dstack app_compose preimage used by the DFL worker.
/// @dev This intentionally accepts only the narrow worker profile: one `dfl-worker` service and
///      one immutable digest-pinned image. It is not a general JSON or YAML parser.
library AppComposeImage {
    bytes private constant DOCKER_COMPOSE_KEY = '"docker_compose_file":"';
    bytes private constant SERVICES = "services:";
    bytes private constant WORKER_SERVICE = "dfl-worker:";
    bytes private constant IMAGE_KEY = "image:";
    bytes private constant SHA256_MARKER = "@sha256:";

    function imageDigest(bytes memory canonicalAppCompose) internal pure returns (bytes32) {
        require(canonicalAppCompose.length != 0 && canonicalAppCompose.length <= 65_536, "invalid app compose size");
        bytes memory dockerCompose = _extractJsonString(canonicalAppCompose, DOCKER_COMPOSE_KEY);
        return _parseWorkerCompose(dockerCompose);
    }

    function _extractJsonString(bytes memory input, bytes memory key) private pure returns (bytes memory output) {
        uint256 valueStart = type(uint256).max;
        uint256 occurrences;
        for (uint256 i = 0; i + key.length <= input.length; i++) {
            if (_matches(input, i, key)) {
                occurrences++;
                valueStart = i + key.length;
            }
        }
        require(occurrences == 1, "docker compose field missing or ambiguous");

        output = new bytes(input.length - valueStart);
        uint256 outLength;
        bool terminated;
        for (uint256 i = valueStart; i < input.length; i++) {
            bytes1 current = input[i];
            if (current == '"') {
                terminated = true;
                break;
            }
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
        require(terminated, "unterminated docker compose field");
        assembly ("memory-safe") {
            mstore(output, outLength)
        }
    }

    function _parseWorkerCompose(bytes memory compose) private pure returns (bytes32) {
        bool inServices;
        bool inWorker;
        uint256 serviceCount;
        uint256 workerCount;
        uint256 imageCount;
        bytes memory imageReference;

        uint256 cursor;
        while (cursor <= compose.length) {
            uint256 lineEnd = cursor;
            while (lineEnd < compose.length && compose[lineEnd] != 0x0a) lineEnd++;
            (uint256 start, uint256 end, uint256 indent) = _lineBounds(compose, cursor, lineEnd);

            if (start < end && compose[start] != "#") {
                if (indent == 0) {
                    inServices = _equals(compose, start, end, SERVICES);
                    inWorker = false;
                } else if (inServices && indent == 2) {
                    // Every non-comment line at this level is a YAML service key
                    // (including flow-style values and merge keys). Count and
                    // reject it unless it is exactly the one supported worker.
                    serviceCount++;
                    bool worker = _equals(compose, start, end, WORKER_SERVICE);
                    if (worker) workerCount++;
                    inWorker = worker;
                } else if (inWorker && indent == 4 && _startsWith(compose, start, end, IMAGE_KEY)) {
                    imageCount++;
                    imageReference = _imageValue(compose, start + IMAGE_KEY.length, end);
                }
            }

            if (lineEnd == compose.length) break;
            cursor = lineEnd + 1;
        }

        require(serviceCount == 1 && workerCount == 1, "worker service missing or ambiguous");
        require(imageCount == 1, "worker image missing or ambiguous");
        return _digestFromReference(imageReference);
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

    function _slice(bytes memory input, uint256 start, uint256 end) private pure returns (bytes memory output) {
        output = new bytes(end - start);
        for (uint256 i = 0; i < output.length; i++) output[i] = input[start + i];
    }
}
