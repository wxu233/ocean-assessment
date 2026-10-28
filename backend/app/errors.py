"""Uniform error body: {"error": {"code": ..., "message": ...}}."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def not_found() -> ApiError:
    """The same 404 for a missing id and another tenant's id."""
    return ApiError(404, "not_found", "Not found.")


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    def _api_error(_: Request, e: ApiError):
        return error_response(e.status, e.code, e.message)

    @app.exception_handler(RequestValidationError)
    def _validation(_: Request, e: RequestValidationError):
        return error_response(422, "invalid_request", str(e.errors()))

    @app.exception_handler(StarletteHTTPException)
    def _http(_: Request, e: StarletteHTTPException):
        code = "not_found" if e.status_code == 404 else "http_error"
        return error_response(e.status_code, code, str(e.detail))
