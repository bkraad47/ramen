from fastapi import HTTPException


class ApiError(HTTPException):
    def __init__(self, status: int, detail: str):
        super().__init__(status, sentence(detail))


def sentence(msg: str) -> str:
    """Every message a person can see starts with a capital letter (U10, CONTRACTS §12.1)."""
    return msg[:1].upper() + msg[1:] if msg else msg


def not_found(what: str) -> ApiError:
    return ApiError(404, f"{what} not found")


def conflict(msg: str) -> ApiError:
    return ApiError(409, msg)


def invalid(msg: str) -> ApiError:
    return ApiError(422, msg)


def forbidden(msg: str) -> ApiError:
    return ApiError(403, msg)
