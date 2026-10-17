import pytest
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)

from agent_harness.mcp.oauth import (
    MCPKeyringTokenStorage,
    OAuthCredentialError,
    logout_mcp_oauth,
)


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, value):
        self.values[(service, username)] = value

    def delete_password(self, service, username):
        key = (service, username)
        if key not in self.values:
            raise KeyError(key)
        del self.values[key]


def _metadata(**overrides):
    values = {
        "issuer": "https://as.example.com",
        "authorization_endpoint": "https://as.example.com/authorize",
        "token_endpoint": "https://as.example.com/token",
    }
    values.update(overrides)
    return OAuthMetadata(**values)


@pytest.mark.asyncio
async def test_credentials_persist_across_storage_instances():
    keyring = FakeKeyring()
    first = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    await first.set_tokens(OAuthToken(access_token="access", refresh_token="refresh", scope="files:read"))
    await first.set_client_info(OAuthClientInformationFull(client_id="client"))
    await first.set_oauth_metadata(_metadata(revocation_endpoint="https://as.example.com/revoke"))
    await first.set_resource_metadata(ProtectedResourceMetadata(
        resource="https://mcp.example.com/mcp",
        authorization_servers=["https://as.example.com"],
    ))

    restarted = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)

    assert (await restarted.get_tokens()).refresh_token == "refresh"
    assert (await restarted.get_client_info()).client_id == "client"
    assert str((await restarted.get_oauth_metadata()).revocation_endpoint) == "https://as.example.com/revoke"
    assert str((await restarted.get_resource_metadata()).resource) == "https://mcp.example.com/mcp"


@pytest.mark.asyncio
async def test_oauth_client_configuration_isolated_from_previous_registration():
    keyring = FakeKeyring()
    old = MCPKeyringTokenStorage(
        "remote",
        "https://mcp.example.com/mcp",
        client_info=OAuthClientInformationFull(client_id="old-client"),
        oauth_identity="old-client",
        keyring_api=keyring,
    )
    current = MCPKeyringTokenStorage(
        "remote",
        "https://mcp.example.com/mcp",
        client_info=OAuthClientInformationFull(client_id="current-client"),
        oauth_identity="current-client",
        keyring_api=keyring,
    )
    await old.set_tokens(OAuthToken(access_token="old-access"))
    await old.set_client_info(OAuthClientInformationFull(client_id="old-client"))

    assert await current.get_tokens() is None
    assert (await current.get_client_info()).client_id == "current-client"


@pytest.mark.asyncio
async def test_oversized_credentials_fail_before_keyring_write():
    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)

    with pytest.raises(OAuthCredentialError, match="exceed the secure credential store limit"):
        await storage.set_tokens(OAuthToken(access_token="x" * 3000))

    assert keyring.values == {}


@pytest.mark.asyncio
async def test_clear_is_idempotent_and_removes_persisted_oauth_data():
    import json

    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    await storage.set_tokens(OAuthToken(access_token="access"))

    await storage.clear()
    await storage.clear()

    assert await storage.get_tokens() is None
    assert [json.loads(value) for value in keyring.values.values()] == [{"generation": 1}]


@pytest.mark.asyncio
async def test_old_storage_instance_cannot_restore_credentials_after_logout():
    keyring = FakeKeyring()
    runtime_storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    logout_storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    restarted = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    await runtime_storage.set_tokens(OAuthToken(access_token="before-logout"))

    await logout_storage.clear()
    with pytest.raises(OAuthCredentialError, match="changed during authorization"):
        await runtime_storage.set_tokens(OAuthToken(access_token="late-refresh"))

    assert await restarted.get_tokens() is None

    login_storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    await login_storage.set_tokens(OAuthToken(access_token="after-login"))
    assert (await MCPKeyringTokenStorage(
        "remote", "https://mcp.example.com/mcp", keyring_api=keyring
    ).get_tokens()).access_token == "after-login"
    assert await runtime_storage.get_tokens() is None


@pytest.mark.asyncio
async def test_independent_storage_instances_preserve_concurrent_record_updates():
    import asyncio
    import time
    from concurrent.futures import ThreadPoolExecutor

    class RacingKeyring(FakeKeyring):
        def get_password(self, service, username):
            value = super().get_password(service, username)
            if value is None:
                time.sleep(0.05)
            return value

    keyring = RacingKeyring()
    token_storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    client_storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    with ThreadPoolExecutor(max_workers=2) as executor:
        token_write = executor.submit(asyncio.run, token_storage.set_tokens(OAuthToken(access_token="access")))
        client_write = executor.submit(
            asyncio.run,
            client_storage.set_client_info(OAuthClientInformationFull(client_id="client")),
        )
        token_write.result(timeout=5)
        client_write.result(timeout=5)

    restarted = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    assert (await restarted.get_tokens()).access_token == "access"
    assert (await restarted.get_client_info()).client_id == "client"


@pytest.mark.asyncio
async def test_clear_reports_keyring_write_failure_without_hiding_credentials():
    from keyring.errors import PasswordSetError

    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    await storage.set_tokens(OAuthToken(access_token="access"))

    def fail_set(*_):
        raise PasswordSetError("locked")

    keyring.set_password = fail_set

    with pytest.raises(OAuthCredentialError, match="could not clear MCP OAuth data"):
        await storage.clear()

    assert await storage.get_tokens() is not None


@pytest.mark.asyncio
async def test_logout_revokes_refresh_and_access_tokens_then_clears_local_state():
    import base64

    import httpx2

    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    await storage.set_tokens(OAuthToken(access_token="access", refresh_token="refresh"))
    await storage.set_client_info(OAuthClientInformationFull(
        client_id="client id",
        client_secret="client:secret",
        token_endpoint_auth_method="client_secret_basic",
    ))
    await storage.set_oauth_metadata(_metadata(
        revocation_endpoint="http://127.0.0.1/revoke",
        revocation_endpoint_auth_methods_supported=["client_secret_basic"],
    ))
    requests = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, *, data, headers):
            requests.append((url, data, headers))
            return httpx2.Response(200)

    result = await logout_mcp_oauth(
        storage,
        client_factory=lambda **_: Client(),
    )

    assert result == {"local_cleared": True, "remote_revoke": "succeeded"}
    assert [request[1]["token_type_hint"] for request in requests] == ["refresh_token", "access_token"]
    expected_basic = base64.b64encode(b"client+id:client%3Asecret").decode()
    assert all(request[2]["Authorization"] == f"Basic {expected_basic}" for request in requests)
    assert await storage.get_tokens() is None


@pytest.mark.asyncio
async def test_logout_reports_remote_failure_but_still_clears_local_state():
    import httpx2

    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    await storage.set_tokens(OAuthToken(access_token="access", refresh_token="refresh"))
    await storage.set_client_info(OAuthClientInformationFull(client_id="client", token_endpoint_auth_method="none"))
    await storage.set_oauth_metadata(_metadata(revocation_endpoint="https://as.example.com/revoke"))
    revoked = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, *, data, headers):
            revoked.append(data["token_type_hint"])
            return httpx2.Response(503 if data["token_type_hint"] == "refresh_token" else 200)

    result = await logout_mcp_oauth(storage, client_factory=lambda **_: Client())

    assert result == {
        "local_cleared": True,
        "remote_revoke": "failed",
        "detail": "revocation endpoint returned HTTP 503",
    }
    assert await storage.get_tokens() is None
    assert revoked == ["refresh_token", "access_token"]


@pytest.mark.asyncio
async def test_logout_without_revocation_metadata_only_clears_local_state():
    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    await storage.set_tokens(OAuthToken(access_token="access"))
    await storage.set_client_info(OAuthClientInformationFull(client_id="client"))
    await storage.set_oauth_metadata(_metadata())

    result = await logout_mcp_oauth(storage)

    assert result == {"local_cleared": True, "remote_revoke": "not_supported"}
    assert await storage.get_tokens() is None


@pytest.mark.asyncio
async def test_logout_clears_local_state_when_stored_oauth_record_is_malformed():
    import json

    class MalformedKeyring(FakeKeyring):
        deleted = False

        def get_password(self, service, username):
            if not self.deleted:
                return json.dumps({"tokens": {"access_token": 7}})
            return None

        def set_password(self, service, username, value):
            self.deleted = True
            super().set_password(service, username, value)

    keyring = MalformedKeyring()
    storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)

    result = await logout_mcp_oauth(storage)

    assert result == {
        "local_cleared": True,
        "remote_revoke": "failed",
        "detail": "stored OAuth credentials could not be read",
    }
    assert keyring.deleted


def test_oauth_client_provider_uses_the_locked_sdk_and_registered_redirect():
    from mcp.client.auth.oauth2 import OAuthClientProvider

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import (
        PersistingOAuthClientProvider,
        make_oauth_client_provider,
    )

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {"callbackPort": 8123, "scopes": "files:read"},
    }]})[0]
    storage = MCPKeyringTokenStorage("remote", config.url, keyring_api=FakeKeyring())

    provider = make_oauth_client_provider(config, storage)

    assert isinstance(provider, OAuthClientProvider)
    assert "_initialize" not in PersistingOAuthClientProvider.__dict__
    assert str(provider.context.client_metadata.redirect_uris[0]) == "http://localhost:8123/callback"
    assert provider.context.client_metadata.scope == "files:read"


@pytest.mark.asyncio
async def test_oauth_provider_validates_the_token_snapshot_loaded_by_the_sdk():
    import httpx2

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import (
        MCPReauthorizationRequired,
        make_oauth_client_provider,
    )

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]

    class ChangingTokenStorage(MCPKeyringTokenStorage):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.snapshots = []

        async def get_tokens(self):
            if self.snapshots:
                return self.snapshots.pop(0)
            return await super().get_tokens()

    storage = ChangingTokenStorage(config.name, config.url, keyring_api=FakeKeyring())
    await storage.set_tokens(OAuthToken(access_token="snapshot-expired", expires_in=-1))
    await storage.set_client_info(OAuthClientInformationFull(client_id="client-id"))
    storage.snapshots = [
        OAuthToken(access_token="snapshot-valid", expires_in=3600),
        OAuthToken(access_token="snapshot-expired", expires_in=-1),
    ]
    provider = make_oauth_client_provider(config, storage)
    sent_requests = []

    def transport(request):
        sent_requests.append(request)
        return httpx2.Response(200, json={"ok": True})

    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(transport),
    ) as client:
        with pytest.raises(MCPReauthorizationRequired):
            await client.get(config.url)

    assert sent_requests == []


@pytest.mark.asyncio
async def test_cancelled_sdk_initialization_rechecks_loaded_token_expiry_on_retry():
    import asyncio

    import httpx2

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]

    class BlockingClientInfoStorage(MCPKeyringTokenStorage):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.client_info_read = asyncio.Event()
            self.release_client_info = asyncio.Event()
            self.block_client_info = True

        async def get_tokens(self):
            return OAuthToken(access_token="expired-access", expires_in=-1)

        async def get_client_info(self):
            if self.block_client_info:
                self.client_info_read.set()
                await self.release_client_info.wait()

    storage = BlockingClientInfoStorage(config.name, config.url, keyring_api=FakeKeyring())
    provider = make_oauth_client_provider(config, storage)
    cancelled_flow = provider.async_auth_flow(httpx2.Request("GET", config.url))
    initialization = asyncio.create_task(anext(cancelled_flow))
    await storage.client_info_read.wait()
    initialization.cancel()
    with pytest.raises(asyncio.CancelledError):
        await initialization
    await cancelled_flow.aclose()

    storage.block_client_info = False
    retry_flow = provider.async_auth_flow(httpx2.Request("GET", config.url))
    try:
        outgoing = await anext(retry_flow)
    finally:
        await retry_flow.aclose()

    assert outgoing.headers.get("authorization") is None


@pytest.mark.asyncio
async def test_expired_token_during_sdk_initialization_is_refreshed_before_mcp_request(monkeypatch):
    import asyncio

    import httpx2
    import mcp.client.auth.oauth2 as oauth2_sdk
    from mcp.shared import auth_utils

    import agent_harness.mcp.oauth as oauth_storage
    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]

    class Clock:
        now = 1000.0

        def time(self):
            return self.now

    clock = Clock()
    monkeypatch.setattr(oauth2_sdk, "time", clock)
    monkeypatch.setattr(auth_utils, "time", clock)
    monkeypatch.setattr(oauth_storage, "time", clock)

    class BlockingClientInfoStorage(MCPKeyringTokenStorage):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.client_info_read = asyncio.Event()
            self.release_client_info = asyncio.Event()

        async def get_client_info(self):
            self.client_info_read.set()
            await self.release_client_info.wait()
            return await super().get_client_info()

    storage = BlockingClientInfoStorage(config.name, config.url, keyring_api=FakeKeyring())
    await storage.set_tokens(OAuthToken(
        access_token="short-lived",
        refresh_token="refresh-token",
        expires_in=2,
    ))
    await storage.set_client_info(OAuthClientInformationFull(
        client_id="client-id",
        token_endpoint_auth_method="none",
    ))
    await storage.set_oauth_metadata(_metadata())
    provider = make_oauth_client_provider(config, storage)
    flow = provider.async_auth_flow(httpx2.Request("GET", config.url))

    async def initialize_flow():
        try:
            return await anext(flow)
        finally:
            await flow.aclose()

    initialization = asyncio.create_task(initialize_flow())
    await storage.client_info_read.wait()
    clock.now = 1003.0
    storage.release_client_info.set()

    outgoing = await initialization
    assert outgoing.url == httpx2.URL("https://as.example.com/token")
    assert outgoing.headers.get("authorization") != "Bearer short-lived"


@pytest.mark.asyncio
async def test_queued_oauth_flow_does_not_extend_an_expired_token(monkeypatch):
    import asyncio

    import httpx2
    import mcp.client.auth.oauth2 as oauth2_sdk
    from mcp.shared import auth_utils

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]

    class Clock:
        now = 1000.0

        def time(self):
            return self.now

    clock = Clock()
    monkeypatch.setattr(oauth2_sdk, "time", clock)
    monkeypatch.setattr(auth_utils, "time", clock)

    class BlockingMetadataStorage(MCPKeyringTokenStorage):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.metadata_read_started = asyncio.Event()
            self.release_metadata_read = asyncio.Event()

        async def get_tokens(self):
            return OAuthToken(access_token="short-lived", expires_in=10)

        async def get_client_info(self):
            return OAuthClientInformationFull(client_id="client-id")

        async def get_oauth_metadata(self):
            self.metadata_read_started.set()
            await self.release_metadata_read.wait()

        async def get_resource_metadata(self):
            pass

    storage = BlockingMetadataStorage(config.name, config.url, keyring_api=FakeKeyring())
    provider = make_oauth_client_provider(config, storage)
    first_flow = provider.async_auth_flow(httpx2.Request("GET", config.url))
    second_flow = provider.async_auth_flow(httpx2.Request("GET", config.url))
    requests = asyncio.Queue()
    responses = {name: asyncio.Queue() for name in ("first", "second")}

    async def drive_flow(name, flow):
        try:
            outgoing = await anext(flow)
            await requests.put((name, outgoing))
            response = await responses[name].get()
            try:
                await flow.asend(response)
            except StopAsyncIteration:
                pass
        finally:
            await flow.aclose()

    first_task = asyncio.create_task(drive_flow("first", first_flow))
    await storage.metadata_read_started.wait()

    second_started = asyncio.Event()

    async def start_second_flow():
        second_started.set()
        await drive_flow("second", second_flow)

    second_task = asyncio.create_task(start_second_flow())
    await second_started.wait()
    await asyncio.sleep(0)
    storage.release_metadata_read.set()

    try:
        leader_name, leader_request = await requests.get()
        assert leader_request.headers.get("authorization") == "Bearer short-lived"
        initial_expiry = provider.context.token_expiry_time
        clock.now = 1011.0
        await responses[leader_name].put(httpx2.Response(403))
        waiting_name, waiting_request = await requests.get()
        assert waiting_name != leader_name
        assert waiting_request.headers.get("authorization") is None
        assert provider.context.token_expiry_time == initial_expiry
    finally:
        await responses["first"].put(httpx2.Response(403))
        await responses["second"].put(httpx2.Response(403))
        await asyncio.gather(first_task, second_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("client_info_issuer", [None, "https://old-as.example.com"])
async def test_restart_discards_persisted_metadata_when_resource_changes_issuer(client_info_issuer):
    import httpx2

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    keyring = FakeKeyring()
    before_restart = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    await before_restart.set_tokens(OAuthToken(access_token="old-access", expires_in=3600))
    await before_restart.set_client_info(OAuthClientInformationFull(
        client_id="legacy-client",
        token_endpoint_auth_method="none",
        issuer=client_info_issuer,
    ))
    await before_restart.set_oauth_metadata(_metadata(
        issuer="https://old-as.example.com",
        authorization_endpoint="https://old-as.example.com/authorize",
        token_endpoint="https://old-as.example.com/token",
    ))
    await before_restart.set_resource_metadata(ProtectedResourceMetadata(
        resource=config.url,
        authorization_servers=["https://old-as.example.com"],
    ))

    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    provider = make_oauth_client_provider(config, storage)
    flow = provider.async_auth_flow(httpx2.Request("GET", config.url))
    try:
        mcp_request = await anext(flow)
        assert mcp_request.headers["authorization"] == "Bearer old-access"
        prm_request = await flow.asend(httpx2.Response(
            401,
            headers={
                "WWW-Authenticate": (
                    'Bearer resource_metadata="https://mcp.example.com/'
                    '.well-known/oauth-protected-resource"'
                )
            },
            request=mcp_request,
        ))
        discovery_request = await flow.asend(httpx2.Response(
            200,
            json={
                "resource": config.url,
                "authorization_servers": ["https://new-as.example.com"],
            },
            request=prm_request,
        ))
    finally:
        await flow.aclose()

    assert discovery_request.url.host == "new-as.example.com"
    assert provider.context.auth_server_url == "https://new-as.example.com"
    assert provider.context.oauth_metadata is None
    assert await storage.get_oauth_metadata() is None


@pytest.mark.asyncio
async def test_changed_issuer_cleanup_preserves_metadata_written_concurrently():
    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    keyring = FakeKeyring()
    old_metadata = _metadata(
        issuer="https://old-as.example.com",
        authorization_endpoint="https://old-as.example.com/authorize",
        token_endpoint="https://old-as.example.com/token",
    )
    new_metadata = _metadata(
        issuer="https://new-as.example.com",
        authorization_endpoint="https://new-as.example.com/authorize",
        token_endpoint="https://new-as.example.com/token",
    )

    concurrent_writer = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)

    class RacingStorage(MCPKeyringTokenStorage):
        async def clear_oauth_metadata(self, *args, **kwargs):
            await concurrent_writer.set_oauth_metadata(new_metadata)
            return await super().clear_oauth_metadata(*args, **kwargs)

    storage = RacingStorage(config.name, config.url, keyring_api=keyring)
    provider = make_oauth_client_provider(config, storage)
    provider.context.auth_server_url = "https://new-as.example.com"
    provider.context.oauth_metadata = old_metadata

    await provider._discard_oauth_metadata_for_changed_issuer()

    assert provider.context.oauth_metadata is None
    assert await storage.get_oauth_metadata() == new_metadata


@pytest.mark.asyncio
async def test_oauth_provider_does_not_overwrite_metadata_discovered_while_loading():
    import asyncio

    import httpx2

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]

    class BlockingMetadataStorage(MCPKeyringTokenStorage):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.oauth_read_started = asyncio.Event()
            self.oauth_read_release = asyncio.Event()
            self.resource_read_started = asyncio.Event()
            self.resource_read_release = asyncio.Event()

        async def get_oauth_metadata(self):
            self.oauth_read_started.set()
            await self.oauth_read_release.wait()
            return _metadata(revocation_endpoint="https://old.example.com/revoke")

        async def get_resource_metadata(self):
            self.resource_read_started.set()
            await self.resource_read_release.wait()
            return ProtectedResourceMetadata(
                resource="https://old.example.com/mcp",
                authorization_servers=["https://old.example.com"],
            )

    storage = BlockingMetadataStorage(config.name, config.url, keyring_api=FakeKeyring())
    provider = make_oauth_client_provider(config, storage)
    flow = provider.async_auth_flow(httpx2.Request("GET", config.url))

    async def open_and_close_flow():
        try:
            await anext(flow)
        finally:
            await flow.aclose()

    first_request = asyncio.create_task(open_and_close_flow())
    await storage.oauth_read_started.wait()
    provider.context.oauth_metadata = _metadata(
        revocation_endpoint="https://new.example.com/revoke"
    )
    storage.oauth_read_release.set()
    await storage.resource_read_started.wait()
    provider.context.protected_resource_metadata = ProtectedResourceMetadata(
        resource="https://new.example.com/mcp",
        authorization_servers=["https://new.example.com"],
    )
    storage.resource_read_release.set()
    await first_request

    assert str(provider.context.oauth_metadata.revocation_endpoint) == "https://new.example.com/revoke"
    assert str(provider.context.protected_resource_metadata.resource) == "https://new.example.com/mcp"


@pytest.mark.asyncio
async def test_oauth_provider_does_not_restore_metadata_after_sdk_invalidation():
    import httpx2

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]

    class CountingMetadataStorage(MCPKeyringTokenStorage):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.oauth_reads = 0
            self.resource_reads = 0

        async def get_oauth_metadata(self):
            self.oauth_reads += 1
            return _metadata(revocation_endpoint="https://old.example.com/revoke")

        async def get_resource_metadata(self):
            self.resource_reads += 1
            return ProtectedResourceMetadata(
                resource="https://old.example.com/mcp",
                authorization_servers=["https://old.example.com"],
            )

    storage = CountingMetadataStorage(config.name, config.url, keyring_api=FakeKeyring())
    provider = make_oauth_client_provider(config, storage)

    async def open_and_close_flow():
        flow = provider.async_auth_flow(httpx2.Request("GET", config.url))
        try:
            await anext(flow)
        finally:
            await flow.aclose()

    await open_and_close_flow()
    provider.context.oauth_metadata = None
    provider.context.protected_resource_metadata = None
    await open_and_close_flow()

    assert provider.context.oauth_metadata is None
    assert provider.context.protected_resource_metadata is None
    assert storage.oauth_reads == 1
    assert storage.resource_reads == 1


@pytest.mark.asyncio
async def test_existing_oauth_provider_stops_sending_cached_tokens_after_logout():
    import httpx2
    from mcp.shared.auth import OAuthToken

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import (
        MCPReauthorizationRequired,
        logout_mcp_oauth,
        make_oauth_client_provider,
    )

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    keyring = FakeKeyring()
    runtime_storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    await runtime_storage.set_tokens(OAuthToken(
        access_token="live-access",
        refresh_token="live-refresh",
        expires_in=3600,
    ))
    provider = make_oauth_client_provider(config, runtime_storage)
    authorizations = []

    def transport(request):
        authorizations.append(request.headers.get("authorization"))
        return httpx2.Response(200, json={"ok": True})

    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(transport),
    ) as client:
        assert (await client.get(config.url)).status_code == 200
        logout_storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
        result = await logout_mcp_oauth(logout_storage)
        assert result["local_cleared"]
        with pytest.raises(MCPReauthorizationRequired):
            await client.get(config.url)

    assert authorizations == ["Bearer live-access"]
    assert await runtime_storage.get_tokens() is None


@pytest.mark.asyncio
async def test_runtime_refuses_oauth_connection_without_saved_credentials(monkeypatch):
    from contextlib import asynccontextmanager

    from agent_harness.mcp.client import MCPServerConnection, MCPServerDownError
    from agent_harness.mcp.config import parse_mcp_servers

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]

    class EmptyStorage:
        async def get_tokens(self):
            return None

    transport_opened = False

    @asynccontextmanager
    async def forbidden_transport(*args, **kwargs):
        nonlocal transport_opened
        transport_opened = True
        raise AssertionError("unauthenticated MCP transport must not start")
        yield

    monkeypatch.setattr("agent_harness.mcp.client.make_oauth_token_storage", lambda _: EmptyStorage())
    monkeypatch.setattr("agent_harness.mcp.client.streamable_http_client", forbidden_transport)

    connection = MCPServerConnection(config)
    with pytest.raises(MCPServerDownError, match="OAuth login is required"):
        await connection.connect()

    assert not transport_opened
    # #1200：缺 token 是"需要授权"的失败形态——管理窗口要显示 needs-auth，
    # 而不是笼统的"未连接"（provider 此时还没建出来，只看它会漏报）。
    assert connection.needs_auth is True


@pytest.mark.asyncio
async def test_sdk_oauth_challenge_completes_login_and_persists_tokens():
    import json
    from urllib.parse import parse_qs, urlsplit

    import httpx2
    from mcp.shared.auth import AuthorizationCodeResult

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {"scopes": "files:read"},
    }]})[0]
    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    auth_state = {}
    requests = []

    async def redirect_handler(url):
        auth_state.update(parse_qs(urlsplit(url).query))

    async def callback_handler():
        return AuthorizationCodeResult(
            code="authorization-code",
            state=auth_state["state"][0],
            iss="https://as.example.com",
        )

    def oauth_http(request):
        requests.append(request)
        if request.url.path == "/mcp" and "authorization" not in request.headers:
            return httpx2.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
                },
            )
        if request.url.path == "/mcp":
            assert request.headers["authorization"] == "Bearer access-token"
            return httpx2.Response(200, json={"ok": True})
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx2.Response(200, json={
                "resource": "https://mcp.example.com/mcp",
                "authorization_servers": ["https://as.example.com"],
                "scopes_supported": ["files:read"],
            })
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={
                "issuer": "https://as.example.com",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "registration_endpoint": "https://as.example.com/register",
                "revocation_endpoint": "https://as.example.com/revoke",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            })
        if request.url.path == "/register":
            body = json.loads(request.content)
            return httpx2.Response(201, json={
                "client_id": "client-id",
                "token_endpoint_auth_method": "none",
                "redirect_uris": body["redirect_uris"],
            })
        if request.url.path == "/token":
            return httpx2.Response(200, json={
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "files:read",
            })
        raise AssertionError(f"unexpected OAuth request: {request.method} {request.url}")

    provider = make_oauth_client_provider(
        config,
        storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(oauth_http),
    ) as client:
        response = await client.get(config.url)

    assert response.status_code == 200
    assert (await storage.get_tokens()).refresh_token == "refresh-token"
    assert (await storage.get_client_info()).client_id == "client-id"
    assert str((await storage.get_oauth_metadata()).revocation_endpoint) == "https://as.example.com/revoke"
    assert str((await storage.get_resource_metadata()).resource) == "https://mcp.example.com/mcp"
    assert auth_state["code_challenge_method"] == ["S256"]
    assert len(requests) >= 6


@pytest.mark.asyncio
async def test_restart_refreshes_with_persisted_authorization_server_metadata():
    import httpx2

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    keyring = FakeKeyring()
    before_restart = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    await before_restart.set_tokens(OAuthToken(
        access_token="expired-access",
        refresh_token="refresh-token",
        expires_in=-1,
    ))
    await before_restart.set_client_info(OAuthClientInformationFull(
        client_id="client-id",
        token_endpoint_auth_method="none",
        issuer="https://as.example.com",
    ))
    await before_restart.set_oauth_metadata(_metadata(
        revocation_endpoint="https://as.example.com/revoke",
        token_endpoint_auth_methods_supported=["none"],
    ))
    await before_restart.set_resource_metadata(ProtectedResourceMetadata(
        resource="https://mcp.example.com/mcp",
        authorization_servers=["https://as.example.com"],
    ))
    after_restart = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    requests = []

    def refresh_http(request):
        requests.append(request)
        if request.url.path == "/token":
            assert request.content
            return httpx2.Response(200, json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            })
        if request.url.path == "/mcp":
            assert request.headers["authorization"] == "Bearer new-access"
            return httpx2.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected refresh request: {request.method} {request.url}")

    provider = make_oauth_client_provider(config, after_restart)
    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(refresh_http),
    ) as client:
        response = await client.get(config.url)

    assert response.status_code == 200
    assert [request.url.path for request in requests] == ["/token", "/mcp"]
    assert (await after_restart.get_tokens()).access_token == "new-access"


@pytest.mark.asyncio
async def test_rejected_persisted_token_failure_points_to_login(monkeypatch):
    import httpx2

    from agent_harness.mcp.client import MCPServerConnection, MCPServerDownError
    from agent_harness.mcp.config import parse_mcp_servers

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    await storage.set_tokens(OAuthToken(
        access_token="expired-access",
        refresh_token="revoked-refresh",
        expires_in=-1,
    ))
    await storage.set_client_info(OAuthClientInformationFull(
        client_id="client-id",
        token_endpoint_auth_method="none",
        issuer="https://as.example.com",
    ))
    await storage.set_oauth_metadata(_metadata(
        token_endpoint_auth_methods_supported=["none"],
    ))
    await storage.set_resource_metadata(ProtectedResourceMetadata(
        resource=config.url,
        authorization_servers=["https://as.example.com"],
    ))

    def rejected_http(request):
        if request.url.path == "/mcp":
            return httpx2.Response(401, headers={
                "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
            })
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx2.Response(200, json={
                "resource": config.url,
                "authorization_servers": ["https://as.example.com"],
            })
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={
                "issuer": "https://as.example.com",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            })
        if request.url.path == "/token":
            return httpx2.Response(400, json={"error": "invalid_grant"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(
        "agent_harness.mcp.client.make_oauth_token_storage",
        lambda _config: storage,
    )
    monkeypatch.setattr(
        "agent_harness.mcp.client.build_http_client",
        lambda _config, *, auth=None: httpx2.AsyncClient(
            auth=auth,
            transport=httpx2.MockTransport(rejected_http),
        ),
    )

    with pytest.raises(MCPServerDownError, match="plugins mcp login remote"):
        await MCPServerConnection(config).connect()


@pytest.mark.asyncio
async def test_unclassified_oauth_flow_error_does_not_point_to_login():
    from contextlib import asynccontextmanager

    from mcp.client.auth.exceptions import OAuthFlowError

    from agent_harness.mcp.client import MCPServerConnection, MCPServerDownError
    from agent_harness.mcp.config import parse_mcp_servers

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]

    class Session:
        async def initialize(self):
            return None

        async def call_tool(self, *_args, **_kwargs):
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [ExceptionGroup("nested transport error", [
                    OAuthFlowError("authorization is required"),
                ])],
            )

    @asynccontextmanager
    async def session_factory():
        yield Session()

    connection = MCPServerConnection(
        config,
        session_factory=session_factory,
        oauth_redirect_handler=lambda _url: None,
    )
    await connection.connect()
    try:
        with pytest.raises(MCPServerDownError) as error:
            await connection.call_tool("write", {})
        assert "plugins mcp login remote" not in str(error.value)
    finally:
        await connection.aclose()


@pytest.mark.asyncio
async def test_protected_resource_mismatch_does_not_mark_reauthorization_required():
    import httpx2
    from mcp.client.auth.exceptions import OAuthFlowError

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=FakeKeyring())
    provider = make_oauth_client_provider(config, storage)

    def mismatch_http(request):
        if request.url.path == "/mcp":
            return httpx2.Response(401, headers={
                "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
            })
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx2.Response(200, json={
                "resource": "https://other.example.com/mcp",
                "authorization_servers": ["https://as.example.com"],
            })
        raise AssertionError(f"unexpected OAuth request: {request.method} {request.url}")

    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(mismatch_http),
    ) as client:
        with pytest.raises(OAuthFlowError, match="Protected resource"):
            await client.get(config.url)

    assert not provider.reauthorization_required


@pytest.mark.asyncio
async def test_active_http_connection_rejected_token_points_to_login(monkeypatch):
    import json

    import httpx2

    from agent_harness.mcp.client import MCPServerConnection, MCPServerDownError
    from agent_harness.mcp.config import parse_mcp_servers

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    await storage.set_tokens(OAuthToken(
        access_token="rejected-access",
        refresh_token="refresh-token",
        expires_in=3600,
    ))
    await storage.set_client_info(OAuthClientInformationFull(
        client_id="client-id",
        token_endpoint_auth_method="none",
        issuer="https://as.example.com",
    ))
    await storage.set_oauth_metadata(_metadata(
        token_endpoint_auth_methods_supported=["none"],
    ))
    await storage.set_resource_metadata(ProtectedResourceMetadata(
        resource=config.url,
        authorization_servers=["https://as.example.com"],
    ))
    methods = []

    def rejected_http(request):
        if request.url.path == "/mcp":
            assert request.headers["authorization"] == "Bearer rejected-access"
            message = json.loads(request.content)
            method = message["method"]
            methods.append(method)
            if method == "initialize":
                return httpx2.Response(200, json={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "oauth-mcp", "version": "1.0"},
                    },
                })
            if method == "notifications/initialized":
                return httpx2.Response(202)
            if method == "tools/call":
                return httpx2.Response(401, headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
                })
            raise AssertionError(f"unexpected MCP method: {method}")
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx2.Response(200, json={
                "resource": config.url,
                "authorization_servers": ["https://as.example.com"],
            })
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={
                "issuer": "https://as.example.com",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            })
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(
        "agent_harness.mcp.client.make_oauth_token_storage",
        lambda _config: storage,
    )
    monkeypatch.setattr(
        "agent_harness.mcp.client.build_http_client",
        lambda _config, *, auth=None: httpx2.AsyncClient(
            auth=auth,
            transport=httpx2.MockTransport(rejected_http),
        ),
    )

    connection = MCPServerConnection(config)
    await connection.connect()
    try:
        with pytest.raises(MCPServerDownError, match="plugins mcp login remote"):
            await connection.call_tool("write", {})
    finally:
        await connection.aclose()

    assert methods == ["initialize", "notifications/initialized", "tools/call"]


@pytest.mark.asyncio
async def test_oauth_server_cannot_succeed_on_an_unauthenticated_request():
    import httpx2
    from mcp.client.auth.exceptions import OAuthFlowError

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=FakeKeyring())
    provider = make_oauth_client_provider(config, storage)

    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={"ok": True})),
    ) as client:
        with pytest.raises(OAuthFlowError, match="accepted an unauthenticated request"):
            await client.get(config.url)


@pytest.mark.asyncio
async def test_authenticated_http_connection_reuses_existing_mcp_tool_path(monkeypatch, tmp_path):
    import json
    from urllib.parse import parse_qs, urlsplit

    import httpx2
    from langchain_core.messages import AIMessage
    from mcp.shared.auth import AuthorizationCodeResult

    from agent_harness.agent import AgentRuntime
    from agent_harness.mcp.adapter import MCPTool
    from agent_harness.mcp.client import MCPServerConnection
    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.session import MODEL_COMPLETED, TOOL_CALL, TOOL_RESULT
    from agent_harness.storage import OperationState, SqliteOperationLedger
    from agent_harness.tooling import PermissionPolicy, ToolExecutor, ToolRegistry
    from tests.conftest import make_session
    from tests.scripted_model import ScriptedModel

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {"scopes": "files:write"},
    }]})[0]
    keyring = FakeKeyring()
    auth_state = {}
    calls = []
    tool_calls = []

    async def redirect_handler(url):
        auth_state.update(parse_qs(urlsplit(url).query))

    async def callback_handler():
        return AuthorizationCodeResult(
            code="authorization-code",
            state=auth_state["state"][0],
            iss="https://as.example.com",
        )

    def response(payload, status=200, headers=None):
        return httpx2.Response(status, json=payload, headers=headers)

    def server_http(request):
        path = request.url.path
        if path == "/mcp":
            if not request.headers.get("authorization"):
                return response(None, 401, {
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
                })
            assert request.headers["authorization"] == "Bearer access-token"
            message = json.loads(request.content)
            method = message["method"]
            calls.append(method)
            if method == "notifications/initialized":
                return httpx2.Response(202)
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "oauth-mcp", "version": "1.0"},
                }
            elif method == "tools/list":
                result = {"tools": [{
                    "name": "write",
                    "description": "write through the authenticated server",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "annotations": {"readOnlyHint": False},
                }]}
            elif method == "tools/call":
                tool_calls.append(message["params"]["arguments"]["text"])
                result = {"content": [{"type": "text", "text": "written"}], "isError": False}
            else:
                raise AssertionError(f"unexpected MCP request: {method}")
            return response({"jsonrpc": "2.0", "id": message["id"], "result": result})
        if path == "/.well-known/oauth-protected-resource":
            return response({
                "resource": "https://mcp.example.com/mcp",
                "authorization_servers": ["https://as.example.com"],
                "scopes_supported": ["files:write"],
            })
        if path == "/.well-known/oauth-authorization-server":
            return response({
                "issuer": "https://as.example.com",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "registration_endpoint": "https://as.example.com/register",
                "revocation_endpoint": "https://as.example.com/revoke",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            })
        if path == "/register":
            body = json.loads(request.content)
            return response({
                "client_id": "client-id",
                "token_endpoint_auth_method": "none",
                "redirect_uris": body["redirect_uris"],
            }, status=201)
        if path == "/token":
            return response({
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "files:write",
            })
        raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")

    monkeypatch.setattr(
        "agent_harness.mcp.client.make_oauth_token_storage",
        lambda server: MCPKeyringTokenStorage(server.name, server.url, keyring_api=keyring),
    )
    monkeypatch.setattr(
        "agent_harness.mcp.client.build_http_client",
        lambda server, *, auth=None: httpx2.AsyncClient(
            auth=auth,
            transport=httpx2.MockTransport(server_http),
        ),
    )

    first_connection = MCPServerConnection(
        config,
        oauth_redirect_handler=redirect_handler,
        oauth_callback_handler=callback_handler,
    )
    await first_connection.connect()
    remote_tools = await first_connection.list_tools()
    assert [tool.name for tool in remote_tools] == ["write"]
    tool_registry = ToolRegistry()
    tool_registry.register(MCPTool(first_connection, config, remote_tools[0]))
    ledger = SqliteOperationLedger(tmp_path / "oauth-tool-state.db")
    await ledger.initialize()
    executor = ToolExecutor(
        tool_registry,
        policy=PermissionPolicy.DANGER_FULL_ACCESS,
        operation_ledger=ledger,
    )
    runtime = AgentRuntime(
        ScriptedModel([
            AIMessage(content="", tool_calls=[{
                "id": "oauth-tool-call",
                "name": "mcp__remote__write",
                "args": {"text": "from runtime"},
                "type": "tool_call",
            }]),
            AIMessage(content="completed"),
        ]),
        tool_registry,
        executor,
    )
    session = make_session(tmp_path / "oauth-sessions")
    first_result = await runtime.run(session, "use the OAuth MCP tool")
    await first_connection.aclose()

    restarted_connection = MCPServerConnection(config)
    await restarted_connection.connect()
    second_result = await restarted_connection.call_tool("write", {"text": "after restart"})
    await restarted_connection.aclose()

    assert first_result.completed
    assert second_result.content[0].text == "written"
    assert tool_calls == ["from runtime", "after restart"]
    assert "tools/call" in calls
    operation = await ledger.get(session.session_id, "oauth-tool-call")
    assert operation is not None and operation.state == OperationState.SUCCEEDED
    event_types = [event.type for event in session.events]
    assert MODEL_COMPLETED in event_types
    assert event_types.index(TOOL_CALL) < event_types.index(TOOL_RESULT)


@pytest.mark.asyncio
async def test_user_denial_does_not_persist_a_token_or_retry_the_mcp_request():
    import httpx2
    from mcp.client.auth.exceptions import OAuthFlowError

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=FakeKeyring())
    requests = []

    async def redirect_handler(_url):
        return None

    async def callback_handler():
        raise OAuthFlowError("user denied")

    def denied_http(request):
        requests.append(request)
        if request.url.path == "/mcp":
            return httpx2.Response(401, headers={
                "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
            })
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx2.Response(200, json={
                "resource": config.url,
                "authorization_servers": ["https://as.example.com"],
            })
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={
                "issuer": "https://as.example.com",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "registration_endpoint": "https://as.example.com/register",
                "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            })
        if request.url.path == "/register":
            import json

            registration = json.loads(request.content)
            return httpx2.Response(201, json={
                "client_id": "client-id",
                "token_endpoint_auth_method": "none",
                "redirect_uris": registration["redirect_uris"],
            })
        raise AssertionError(f"unexpected request after denial: {request.method} {request.url}")

    provider = make_oauth_client_provider(
        config,
        storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(denied_http),
    ) as client:
        with pytest.raises(OAuthFlowError, match="user denied"):
            await client.get(config.url)

    assert sum(request.url.path == "/mcp" for request in requests) == 1
    assert await storage.get_tokens() is None


@pytest.mark.asyncio
async def test_insufficient_scope_uses_sdk_step_up_and_preserves_existing_scope():
    from urllib.parse import parse_qs, urlsplit

    import httpx2
    from mcp.shared.auth import AuthorizationCodeResult

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {"scopes": "files:read"},
    }]})[0]
    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    await storage.set_tokens(OAuthToken(
        access_token="read-token",
        refresh_token="read-refresh",
        expires_in=3600,
        scope="files:read",
    ))
    await storage.set_client_info(OAuthClientInformationFull(
        client_id="client-id",
        token_endpoint_auth_method="none",
        issuer="https://as.example.com",
        redirect_uris=["http://localhost:8765/callback"],
    ))
    await storage.set_oauth_metadata(_metadata(
        revocation_endpoint="https://as.example.com/revoke",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
    ))
    await storage.set_resource_metadata(ProtectedResourceMetadata(
        resource=config.url,
        authorization_servers=["https://as.example.com"],
    ))
    auth_state = {}

    async def redirect_handler(url):
        auth_state.update(parse_qs(urlsplit(url).query))

    async def callback_handler():
        return AuthorizationCodeResult(
            code="step-up-code",
            state=auth_state["state"][0],
            iss="https://as.example.com",
        )

    def step_up_http(request):
        if request.url.path == "/mcp":
            token = request.headers.get("authorization")
            if token == "Bearer read-token":
                return httpx2.Response(403, headers={
                    "WWW-Authenticate": 'Bearer error="insufficient_scope", scope="files:write"'
                })
            assert token == "Bearer write-token"
            return httpx2.Response(200, json={"ok": True})
        if request.url.path == "/token":
            return httpx2.Response(200, json={
                "access_token": "write-token",
                "refresh_token": "write-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "files:read files:write",
            })
        raise AssertionError(f"unexpected scope step-up request: {request.method} {request.url}")

    provider = make_oauth_client_provider(
        config,
        storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(step_up_http),
    ) as client:
        response = await client.get(config.url)

    assert response.status_code == 200
    assert set(auth_state["scope"][0].split()) == {"files:read", "files:write"}
    assert set((await storage.get_tokens()).scope.split()) == {"files:read", "files:write"}
