"""Abstract tunnel interface — all tunnel implementations must subclass this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional


class TunnelBase(ABC):
    """
    A tunnel exposes a local port to an external URL.
    The spectrogram port is never passed to any tunnel — enforced by the caller.
    """

    def __init__(self, local_port: int) -> None:
        self.local_port = local_port
        self.public_url: Optional[str] = None
        self.on_url_assigned: Optional[Callable[[str], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

    @abstractmethod
    def start(self) -> None:
        """Start the tunnel. Must call on_url_assigned when the URL is known."""

    @abstractmethod
    def stop(self) -> None:
        """Tear down the tunnel cleanly."""

    @property
    def is_running(self) -> bool:
        return self.public_url is not None
