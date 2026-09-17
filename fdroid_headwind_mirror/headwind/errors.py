from __future__ import annotations


class HeadwindError(Exception):
    pass


class HeadwindTransportError(HeadwindError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class HeadwindApiError(HeadwindError):
    def __init__(self, status: str, message: str | None, path: str) -> None:
        super().__init__(f"{path}: status={status} message={message or '<none>'}")
        self.status = status
        self.message = message
        self.path = path


class HeadwindPermissionError(HeadwindApiError):
    pass


class HeadwindCredentialsError(HeadwindError):
    def __init__(self, login: str) -> None:
        super().__init__(f"identifiants refuses par Headwind pour l'utilisateur {login}")
        self.login = login


PERMISSION_DENIED_MESSAGE = "error.permission.denied"
