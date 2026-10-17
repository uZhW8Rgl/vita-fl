import httpx
from preflight_phala import preflight


def test_preflight_repeats_exact_urls_without_restart_and_redacts_api_details():
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"kms_urls": ["https://kms.example"], "ports": []})
        return httpx.Response(403, json={"detail": "workspace restricted", "debug": "must not retain"})

    with httpx.Client(base_url="https://api.example", transport=httpx.MockTransport(handle)) as client:
        result = preflight(client, "cvm_test", check_network_permission=True)
    import json

    assert json.loads(calls[1].content) == {"kms_urls": ["https://kms.example"], "restart": False}
    assert result["custom_kms_configuration_available"] is False
    assert result["restarted"] is False
    assert "must not retain" not in str(result)


def test_default_preflight_is_read_only():
    calls = []

    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json={"kms_urls": []})

    with httpx.Client(base_url="https://api.example", transport=httpx.MockTransport(handle)) as client:
        preflight(client, "cvm_test")
    assert calls == ["GET"]
