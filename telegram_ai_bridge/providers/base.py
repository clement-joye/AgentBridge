from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import LocalSession


class SessionProvider(ABC):
    name: str

    @abstractmethod
    def discover_sessions(self, limit: int) -> list[LocalSession]:
        raise NotImplementedError

    @abstractmethod
    def resume_command(self, session: LocalSession) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def new_command(self, cwd: str) -> list[str]:
        raise NotImplementedError
