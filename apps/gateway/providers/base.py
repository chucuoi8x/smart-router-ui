from typing import Protocol, Any, AsyncIterator, TypeVar

class DriverNotFoundError(Exception):
    pass


class ProviderDiscoveryError(RuntimeError):
    """Raised when a driver cannot list models from the provider.

    The previous behavior swallowed every transport/HTTP failure and returned
    ``[]``, so the Control Plane reported a false success (``200 models=[]``)
    for an unauthenticated or unreachable provider.  This error carries an
    explicit, structured ``kind`` and the upstream ``status_code`` so the admin
    endpoint can map it onto a meaningful HTTP status instead of masking the
    failure.
    """

    def __init__(self, kind: str, detail: str, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.status_code = status_code

class ProviderDriver(Protocol):
    """Contract for upstream provider drivers.

    ``delegates_request_execution`` advertises that the driver performs the
    full HTTP exchange itself (request building, auth, response parsing).
    Drivers that do not delegate it let the router keep its direct HTTP
    client behavior. This is a driver capability, never a provider-name
    check in routing core.
    """

    delegates_request_execution: bool = False

    async def validate_connection(self, ctx: Any) -> Any: ...
    async def discover_models(self, ctx: Any) -> list[Any]: ...
    async def execute(self, ctx: Any, request: Any) -> Any: ...
    async def execute_stream(self, ctx: Any, request: Any) -> AsyncIterator[bytes]: ...
    async def fetch_quota(self, ctx: Any) -> list[Any]: ...
    def parse_usage(self, response: Any) -> dict[str, Any]: ...
    def classify_error(self, error_or_response: Any) -> dict[str, Any]: ...
    def capabilities(self) -> dict[str, Any]: ...
