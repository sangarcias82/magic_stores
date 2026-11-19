from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import authenticate, login_session, logout_session, safe_next_path
from app.db import get_db
from app.i18n import translator
from app.models import UserRole
from app.services import storefront

router = APIRouter(tags=["auth"])


def _login_page(request: Request, *, next_path: str, email: str = "", error: Optional[str] = None, status_code: int = 200):
    return storefront.platform_templates().TemplateResponse(
        request,
        "login.html",
        {"page_title": translator(request)("login.title"), "next_path": next_path, "email": email, "error": error},
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: Optional[str] = None):
    return _login_page(request, next_path=safe_next_path(next, default=""))


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = authenticate(db, email, password) if email and password else None
    if user is None:
        return _login_page(
            request,
            next_path=safe_next_path(next, default=""),
            email=email,
            error=translator(request)("login.invalid"),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    login_session(request, user)
    default = "/admin" if user.role == UserRole.admin else "/"
    return RedirectResponse(safe_next_path(next, default=default), status_code=status.HTTP_303_SEE_OTHER)


# POST is what the "Sign out" button uses; GET is accepted so /logout also works from the address bar.
# Registered separately (not one api_route) so each method gets its own OpenAPI operation id.
@router.post("/logout")
@router.get("/logout")
def logout(request: Request):
    logout_session(request)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
