from collections.abc import Callable
from typing import Any, Protocol


class CcApiResponse(Protocol):
    """The CC SDK response shape decoded and detached by its driver."""

    @property
    def is_success(self) -> bool: ...

    @property
    def result(self) -> Any: ...

    @property
    def error_code(self) -> int: ...

    @property
    def error_message(self) -> str: ...


class CcApiClient(Protocol):
    """An already connected, application-owned CC SDK client.

    Methods enqueue work without waiting for network responses. SDK callbacks
    may arrive on foreign threads and must be bridged by the driver. One driver
    owns the client's finished/disconnect handlers and multiplexes them across
    skills. Importing this contract never loads the native SDK.
    """

    def request(
        self, method: str, params: dict[str, Any], callback: Callable[[CcApiResponse], None],
    ) -> None: ...

    def notify(self, method: str) -> None: ...

    def add_service(self, name: str, callback: Callable[[Any, Any], None]) -> None: ...

    def remove_service(self, name: str) -> None: ...

    def set_disconnect_callback(self, callback: Callable[[], None]) -> None: ...
