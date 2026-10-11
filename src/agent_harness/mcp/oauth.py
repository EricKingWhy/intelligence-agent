"""MCP OAuth integration around the OAuth client already provided by the SDK."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlsplit

import httpx2
import keyring
from keyring.errors import KeyringError
from mcp.client.auth.exceptions import OAuthFlowError
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from pydantic import ValidationError

from agent_harness.mcp.config import MCPServerConfig
from agent_harness.skills.package_lock import registry_lock

KEYRING_SERVICE = "agent-harness:mcp-oauth"
MAX_CREDENTIAL_BLOB_BYTES = 2560
_RECORD_FIELDS = {
    "tokens", "tokens_saved_at", "client_info", "oauth_metadata", "resource_metadata", "generation",
}
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _credential_lock_path(username: str) -> Path:
    lock_id = hashlib.sha256(username.encode()).hexdigest()
    return Path.home() / ".cache" / "agent-harness" / "mcp-oauth" / lock_id


def _process_lock_for(username: str) -> threading.RLock:
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(username, threading.RLock())


@contextmanager
def _serialized_record_lock(process_lock: threading.RLock, lock_path: Path):
    """Serialize record updates across both storage instances and processes."""
    with process_lock, registry_lock(lock_path):
        yield


class OAuthCredentialError(RuntimeError):
    """Credential persistence failed or stored OAuth data could not be read safely."""


class MCPReauthorizationRequired(OAuthFlowError):
    """The SDK reached an interactive authorization step without login handlers."""


async def _require_interactive_login(*_args: Any) -> None:
    raise MCPReauthorizationRequired("OAuth reauthorization requires the interactive login command")


class MCPKeyringTokenStorage:
    """Persist one server's OAuth record in the configured system keyring backend."""

    def __init__(
        self,
        server_name: str,
        server_url: str,
        *,
        client_info: OAuthClientInformationFull | None = None,
        oauth_identity: str = "",
        keyring_api: Any = keyring,
    ) -> None:
        identity = hashlib.sha256(f"{server_name}\0{server_url}\0{oauth_identity}".encode()).hexdigest()[:32]
        self._username = f"mcp:{server_name}:{identity}"
        self._client_info = client_info
        self._keyring = keyring_api
        self._lock = asyncio.Lock()
        # A logout advances this marker so an older provider cannot restore tokens.
        self._generation: int | None = None

    def _record_lock(self):
        return _serialized_record_lock(
            _process_lock_for(self._username),
            _credential_lock_path(self._username),
        )

    async def get_tokens(self) -> OAuthToken | None:
        async with self._lock:
            with self._record_lock():
                record = self._read()
                if not self._is_current_generation(record):
                    return None
            value = record.get("tokens")
            if value is None:
                return None
            tokens = OAuthToken.model_validate(value)
            saved_at = record.get("tokens_saved_at")
            if tokens.expires_in is not None and isinstance(saved_at, (int, float)):
                elapsed = max(0, time.time() - saved_at)
                remaining = int(tokens.expires_in - elapsed)
                tokens = tokens.model_copy(update={"expires_in": max(-1, remaining)})
            return tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        if not await self._update("tokens", tokens.model_dump(mode="json", exclude_none=True)):
            raise OAuthCredentialError("OAuth credentials changed during authorization; run login again")

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        async with self._lock:
            if self._client_info is not None:
                return self._client_info
            with self._record_lock():
                record = self._read()
                if not self._is_current_generation(record):
                    return None
                value = record.get("client_info")
            if value is not None:
                return OAuthClientInformationFull.model_validate(value)
            return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self._update("client_info", client_info.model_dump(mode="json", exclude_none=True))

    async def get_oauth_metadata(self) -> OAuthMetadata | None:
        async with self._lock:
            with self._record_lock():
                record = self._read()
                if not self._is_current_generation(record):
                    return None
                value = record.get("oauth_metadata")
            return OAuthMetadata.model_validate(value) if value is not None else None

    async def set_oauth_metadata(self, metadata: OAuthMetadata) -> None:
        await self._update("oauth_metadata", metadata.model_dump(mode="json", exclude_none=True))

    async def clear_oauth_metadata(self, *, expected: OAuthMetadata | None = None) -> bool:
        expected_value = (
            expected.model_dump(mode="json", exclude_none=True) if expected is not None else None
        )
        return await self._update("oauth_metadata", None, expected_value=expected_value)

    async def get_resource_metadata(self) -> ProtectedResourceMetadata | None:
        async with self._lock:
            with self._record_lock():
                record = self._read()
                if not self._is_current_generation(record):
                    return None
                value = record.get("resource_metadata")
            return ProtectedResourceMetadata.model_validate(value) if value is not None else None

    async def set_resource_metadata(self, metadata: ProtectedResourceMetadata) -> None:
        await self._update("resource_metadata", metadata.model_dump(mode="json", exclude_none=True))

    async def clear(self) -> None:
        async with self._lock:
            with self._record_lock():
                record = self._read_for_clear()
                generation = max(self._record_generation(record), self._generation or 0)
                already_cleared = set(record) <= {"generation"} and generation > 0
                next_generation = generation if already_cleared else generation + 1
                try:
                    # Keep only a non-secret tombstone to fence in-flight refreshes.
                    self._write({"generation": next_generation})
                except OAuthCredentialError as error:
                    raise OAuthCredentialError(
                        "system credential store could not clear MCP OAuth data"
                    ) from error
                self._generation = next_generation

    async def _update(
        self,
        field: str,
        value: dict[str, Any] | None,
        *,
        expected_value: dict[str, Any] | None = None,
    ) -> bool:
        async with self._lock:
            with self._record_lock():
                record = self._read()
                if not self._is_current_generation(record):
                    return False
                if expected_value is not None and record.get(field) != expected_value:
                    return False
                if value is None:
                    record.pop(field, None)
                else:
                    record[field] = value
                if field == "tokens":
                    record["tokens_saved_at"] = time.time()
                self._write(record)
                return True

    def _is_current_generation(self, record: dict[str, Any]) -> bool:
        generation = self._record_generation(record)
        if self._generation is None:
            self._generation = generation
        return generation == self._generation

    @staticmethod
    def _record_generation(record: dict[str, Any]) -> int:
        generation = record.get("generation", 0)
        if type(generation) is not int or generation < 0:
            raise OAuthCredentialError("stored MCP OAuth data is invalid")
        return generation

    def _read_for_clear(self) -> dict[str, Any]:
        try:
            raw = self._keyring.get_password(KEYRING_SERVICE, self._username)
        except KeyringError as error:
            raise OAuthCredentialError("system credential store is unavailable") from error
        if raw is None:
            return {}
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            return {"generation": self._generation or 0}
        if not isinstance(record, dict):
            return {"generation": self._generation or 0}
        generation = record.get("generation", 0)
        if type(generation) is not int or generation < 0:
            return {"generation": self._generation or 0}
        if not set(record).issubset(_RECORD_FIELDS):
            return {"generation": generation}
        return record

    def _read(self) -> dict[str, Any]:
        try:
            raw = self._keyring.get_password(KEYRING_SERVICE, self._username)
        except KeyringError as error:
            raise OAuthCredentialError("system credential store is unavailable") from error
        if raw is None:
            return {}
        try:
            record = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise OAuthCredentialError("stored MCP OAuth data is invalid") from error
        if not isinstance(record, dict) or not set(record).issubset(_RECORD_FIELDS):
            raise OAuthCredentialError("stored MCP OAuth data is invalid")
        self._record_generation(record)
        return record

    def _write(self, record: dict[str, Any]) -> None:
        raw = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        if len(raw.encode("utf-16-le")) > MAX_CREDENTIAL_BLOB_BYTES:
            raise OAuthCredentialError(
                "OAuth credentials exceed the secure credential store limit; no data was written"
            )
        try:
            self._keyring.set_password(KEYRING_SERVICE, self._username, raw)
        except KeyringError as error:
            raise OAuthCredentialError("system credential store could not save MCP OAuth data") from error


class PersistingOAuthClientProvider(OAuthClientProvider):
    """Persist OAuth server metadata yielded by the SDK for conditional revocation."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reauthorization_required = False
        self._metadata_restore_attempted = False

    async def async_auth_flow(self, request: httpx2.Request):
        async with self.context.lock:
            if not self._metadata_restore_attempted:
                if self.context.oauth_metadata is None:
                    get_oauth_metadata = getattr(self.context.storage, "get_oauth_metadata", None)
                    if get_oauth_metadata is not None:
                        oauth_metadata = await get_oauth_metadata()
                        if self.context.oauth_metadata is None:
                            self.context.oauth_metadata = oauth_metadata
                if self.context.protected_resource_metadata is None:
                    get_resource_metadata = getattr(self.context.storage, "get_resource_metadata", None)
                    if get_resource_metadata is not None:
                        resource_metadata = await get_resource_metadata()
                        if self.context.protected_resource_metadata is None:
                            self.context.protected_resource_metadata = resource_metadata
                self._metadata_restore_attempted = True

        flow = super().async_auth_flow(request)
        try:
            outgoing = await anext(flow)
            while True:
                await self._ensure_saved_token_is_current(outgoing)
                if self.context.token_expiry_time is None and self.context.current_tokens is not None:
                    self.context.update_token_expiry(self.context.current_tokens)
                    if not self.context.is_token_valid():
                        expired_token = self.context.current_tokens.access_token
                        if request.headers.get("authorization") == f"Bearer {expired_token}":
                            request.headers.pop("authorization", None)
                        await flow.aclose()
                        flow = super().async_auth_flow(request)
                        outgoing = await anext(flow)
                        continue
                response = yield outgoing
                if (
                    response.status_code < 300
                    and self._is_mcp_request(outgoing)
                    and not outgoing.headers.get("authorization")
                ):
                    raise OAuthFlowError("OAuth-protected MCP server accepted an unauthenticated request")
                try:
                    outgoing = await flow.asend(response)
                except StopAsyncIteration:
                    return
                await self._discard_oauth_metadata_for_changed_issuer()
                metadata = self.context.oauth_metadata
                persist = getattr(self.context.storage, "set_oauth_metadata", None)
                if metadata is not None and persist is not None:
                    await persist(metadata)
                resource = self.context.protected_resource_metadata
                persist_resource = getattr(self.context.storage, "set_resource_metadata", None)
                if resource is not None and persist_resource is not None:
                    await persist_resource(resource)
        except MCPReauthorizationRequired:
            self.reauthorization_required = True
            raise
        finally:
            await flow.aclose()

    async def _discard_oauth_metadata_for_changed_issuer(self) -> None:
        metadata = self.context.oauth_metadata
        auth_server_url = self.context.auth_server_url
        if auth_server_url is None:
            return

        if metadata is None:
            get_metadata = getattr(self.context.storage, "get_oauth_metadata", None)
            if get_metadata is None:
                return
            metadata = await get_metadata()
        metadata_issuer = str(metadata.issuer) if metadata is not None and metadata.issuer else None
        if metadata is None or metadata_issuer == auth_server_url:
            return

        self.context.oauth_metadata = None
        clear_metadata = getattr(self.context.storage, "clear_oauth_metadata", None)
        if clear_metadata is not None:
            await clear_metadata(expected=metadata)

    async def _ensure_saved_token_is_current(self, request: httpx2.Request) -> None:
        if not self._is_mcp_request(request) or not request.headers.get("authorization"):
            return
        get_tokens = getattr(self.context.storage, "get_tokens", None)
        if get_tokens is None:
            return
        stored = await get_tokens()
        current = self.context.current_tokens
        if stored is None or current is None or stored.access_token != current.access_token:
            raise MCPReauthorizationRequired(
                "MCP OAuth credentials were cleared or changed; run login before reconnecting"
            )
        if self.context.token_expiry_time is None:
            self.context.current_tokens = stored

    def _is_mcp_request(self, request: httpx2.Request) -> bool:
        return request.url == httpx2.URL(self.context.server_url)


def oauth_redirect_uri(config: MCPServerConfig) -> str:
    if config.oauth is None:
        raise OAuthCredentialError("server has no OAuth settings")
    return f"http://localhost:{config.oauth.callback_port}/callback"


def make_oauth_token_storage(config: MCPServerConfig) -> MCPKeyringTokenStorage:
    if config.oauth is None or config.url is None:
        raise OAuthCredentialError("server has no HTTP OAuth settings")
    static_client_info = None
    if config.oauth.client_id is not None:
        secret = config.oauth.client_secret
        static_client_info = OAuthClientInformationFull(
            client_id=config.oauth.client_id,
            client_secret=secret.get_secret_value() if secret else None,
            token_endpoint_auth_method="client_secret_basic" if secret else "none",
            redirect_uris=[oauth_redirect_uri(config)],
        )
    return MCPKeyringTokenStorage(
        config.name,
        config.url,
        client_info=static_client_info,
        oauth_identity=(
            f"{config.oauth.client_id or ''}\0{config.oauth.client_metadata_url or ''}"
            f"\0{oauth_redirect_uri(config)}"
        ),
    )


def make_oauth_client_provider(
    config: MCPServerConfig,
    storage: MCPKeyringTokenStorage,
    *,
    redirect_handler: Any = None,
    callback_handler: Any = None,
) -> PersistingOAuthClientProvider:
    if config.oauth is None or config.url is None:
        raise OAuthCredentialError("server has no HTTP OAuth settings")
    if redirect_handler is None or callback_handler is None:
        redirect_handler = _require_interactive_login
        callback_handler = _require_interactive_login
    metadata = OAuthClientMetadata(
        client_name="Intelligence Agent",
        redirect_uris=[oauth_redirect_uri(config)],
        scope=config.oauth.scopes,
    )
    return PersistingOAuthClientProvider(
        server_url=config.url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        client_metadata_url=config.oauth.client_metadata_url,
    )


async def logout_mcp_oauth(
    storage: MCPKeyringTokenStorage,
    *,
    client_factory: Any = httpx2.AsyncClient,
) -> dict[str, Any]:
    """Try RFC 7009 when advertised, then always remove local OAuth state."""
    remote_status = "not_logged_in"
    remote_detail: str | None = None
    try:
        tokens = await storage.get_tokens()
        client_info = await storage.get_client_info()
        metadata = await storage.get_oauth_metadata()
        if tokens is not None:
            if metadata is None or metadata.revocation_endpoint is None:
                remote_status = "not_supported"
            else:
                try:
                    await _revoke_tokens(
                        str(metadata.revocation_endpoint), tokens, client_info, metadata,
                        client_factory=client_factory,
                    )
                    remote_status = "succeeded"
                except (OAuthCredentialError, httpx2.HTTPError) as error:
                    remote_status = "failed"
                    remote_detail = _safe_revoke_error(error)
    except (OAuthCredentialError, ValidationError):
        remote_status = "failed"
        remote_detail = "stored OAuth credentials could not be read"
    finally:
        await storage.clear()
    return {
        "local_cleared": True,
        "remote_revoke": remote_status,
        **({"detail": remote_detail} if remote_detail else {}),
    }


async def _revoke_tokens(
    endpoint: str,
    tokens: OAuthToken,
    client_info: OAuthClientInformationFull | None,
    metadata: OAuthMetadata,
    *,
    client_factory: Any,
) -> None:
    parsed = urlsplit(endpoint)
    if not _is_secure_revocation_url(parsed):
        raise OAuthCredentialError("revocation endpoint must use HTTPS or loopback HTTP")
    if client_info is None or not client_info.client_id:
        raise OAuthCredentialError("OAuth client information is unavailable")

    method = client_info.token_endpoint_auth_method or (
        "client_secret_basic" if client_info.client_secret else "none"
    )
    supported = metadata.revocation_endpoint_auth_methods_supported
    if supported is not None and method not in supported:
        raise OAuthCredentialError("revocation endpoint does not support this client authentication method")

    async with client_factory(timeout=10.0, follow_redirects=False) as client:
        values = [(tokens.refresh_token, "refresh_token"), (tokens.access_token, "access_token")]
        failures: list[str] = []
        for token, hint in values:
            if not token:
                continue
            data = {"token": token, "token_type_hint": hint}
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if method == "client_secret_basic":
                if not client_info.client_secret:
                    raise OAuthCredentialError("OAuth client secret is unavailable")
                raw = f"{quote_plus(client_info.client_id, safe='')}:{quote_plus(client_info.client_secret, safe='')}"
                headers["Authorization"] = f"Basic {base64.b64encode(raw.encode()).decode()}"
            elif method == "client_secret_post":
                if not client_info.client_secret:
                    raise OAuthCredentialError("OAuth client secret is unavailable")
                data.update(client_id=client_info.client_id, client_secret=client_info.client_secret)
            elif method == "none":
                data["client_id"] = client_info.client_id
            else:
                raise OAuthCredentialError("OAuth client authentication method is unsupported for revocation")
            try:
                response = await client.post(endpoint, data=data, headers=headers)
            except httpx2.HTTPError as error:
                failures.append(_safe_revoke_error(error))
                continue
            if response.status_code != 200:
                failures.append(f"revocation endpoint returned HTTP {response.status_code}")
        if failures:
            raise OAuthCredentialError("; ".join(failures))


def _is_secure_revocation_url(parsed) -> bool:
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http" or not parsed.hostname:
        return False
    if parsed.hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _safe_revoke_error(error: Exception) -> str:
    if isinstance(error, OAuthCredentialError):
        return str(error)
    if isinstance(error, httpx2.HTTPStatusError):
        return f"revocation endpoint returned HTTP {error.response.status_code}"
    return f"revocation request failed ({type(error).__name__})"
