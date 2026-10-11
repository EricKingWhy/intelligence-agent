"""MCP OAuth integration around the OAuth client already provided by the SDK."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import time
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

KEYRING_SERVICE = "agent-harness:mcp-oauth"
MAX_CREDENTIAL_BLOB_BYTES = 2560
_RECORD_FIELDS = {"tokens", "tokens_saved_at", "client_info", "oauth_metadata", "resource_metadata"}


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

    async def get_tokens(self) -> OAuthToken | None:
        async with self._lock:
            record = self._read()
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
        await self._update("tokens", tokens.model_dump(mode="json", exclude_none=True))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        async with self._lock:
            if self._client_info is not None:
                return self._client_info
            value = self._read().get("client_info")
            if value is not None:
                return OAuthClientInformationFull.model_validate(value)
            return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self._update("client_info", client_info.model_dump(mode="json", exclude_none=True))

    async def get_oauth_metadata(self) -> OAuthMetadata | None:
        async with self._lock:
            value = self._read().get("oauth_metadata")
            return OAuthMetadata.model_validate(value) if value is not None else None

    async def set_oauth_metadata(self, metadata: OAuthMetadata) -> None:
        await self._update("oauth_metadata", metadata.model_dump(mode="json", exclude_none=True))

    async def get_resource_metadata(self) -> ProtectedResourceMetadata | None:
        async with self._lock:
            value = self._read().get("resource_metadata")
            return ProtectedResourceMetadata.model_validate(value) if value is not None else None

    async def set_resource_metadata(self, metadata: ProtectedResourceMetadata) -> None:
        await self._update("resource_metadata", metadata.model_dump(mode="json", exclude_none=True))

    async def clear(self) -> None:
        async with self._lock:
            try:
                self._keyring.delete_password(KEYRING_SERVICE, self._username)
            except (KeyringError, KeyError) as error:
                try:
                    remaining = self._keyring.get_password(KEYRING_SERVICE, self._username)
                except (KeyringError, KeyError) as read_error:
                    raise OAuthCredentialError(
                        "system credential store could not verify MCP OAuth data was cleared"
                    ) from read_error
                if remaining is not None:
                    raise OAuthCredentialError("system credential store could not clear MCP OAuth data") from error

    async def _update(self, field: str, value: dict[str, Any]) -> None:
        async with self._lock:
            record = self._read()
            record[field] = value
            if field == "tokens":
                record["tokens_saved_at"] = time.time()
            self._write(record)

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

    async def _initialize(self) -> None:
        await super()._initialize()
        if self.context.current_tokens is not None:
            self.context.update_token_expiry(self.context.current_tokens)
        get_oauth_metadata = getattr(self.context.storage, "get_oauth_metadata", None)
        get_resource_metadata = getattr(self.context.storage, "get_resource_metadata", None)
        if get_oauth_metadata is not None:
            self.context.oauth_metadata = await get_oauth_metadata()
        if get_resource_metadata is not None:
            self.context.protected_resource_metadata = await get_resource_metadata()

    async def async_auth_flow(self, request: httpx2.Request):
        flow = super().async_auth_flow(request)
        try:
            outgoing = await anext(flow)
            while True:
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
