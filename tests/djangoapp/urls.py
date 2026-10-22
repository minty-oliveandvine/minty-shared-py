"""A ninja API whose routes hit every path of the error envelope and the middleware."""

import logging

from django.urls import path
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError
from ninja.security import HttpBearer

from minty_shared.django.errors import ApiRefusal, register_error_handlers

api = NinjaAPI(urls_namespace="shared-test")


class DomainConflictError(Exception):
    pass


class StatusError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


register_error_handlers(
    api,
    logger=logging.getLogger("shared-test"),
    refusals={DomainConflictError: 409, StatusError: lambda e: e.status},  # type: ignore[attr-defined]
)


class Bearer(HttpBearer):
    def authenticate(self, request, token):
        return token if token == "good" else None


class Bill(Schema):
    amount: float
    payee_name: str


@api.get("/items")
def list_items(request):
    return {"ok": "list"}


@api.post("/items")
def create_item(request, bill: Bill):
    return {"ok": "create"}


@api.get("/refused")
def refused(request):
    raise ApiRefusal(
        "That day is closed. Open it to change it.", 409, fields={"day": "Closed."}, to="dashboard"
    )


@api.get("/conflict")
def conflict(request):
    raise DomainConflictError("That reference already exists. Use another one.")


@api.get("/status")
def status(request):
    raise StatusError("Your card was declined. Use another card.", 402)


@api.get("/http")
def http(request):
    raise HttpError(404, "I couldn't find that bill.")


@api.get("/missing")
def missing(request):
    from django.http import Http404

    raise Http404("I couldn't find that bill.")


@api.get("/forbidden")
def forbidden(request):
    from ninja.errors import AuthorizationError

    raise AuthorizationError()


@api.get("/boom")
def boom(request):
    raise RuntimeError("database fell over: secret-detail")


@api.get("/private", auth=Bearer())
def private(request):
    return {"ok": True}


@api.get("/share/{secret}")
def share(request, secret: str):
    return {"ok": True}


def loggable_path(p: str) -> str:
    return "/api/share/[redacted]" if p.startswith("/api/share/") else p


urlpatterns = [path("api/", api.urls)]
