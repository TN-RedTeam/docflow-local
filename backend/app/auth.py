"""Authentification par cookie de session."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from .models import AuthSession, User

router = APIRouter(prefix="/api", tags=["auth"])

SESSION_COOKIE = "docflow_session"
PUBLIC_PATHS = frozenset({"/api/health", "/api/connexion", "/docs", "/openapi.json"})


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return f"{salt}${h.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    salt, h = stored.split("$", 1)
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return secrets.compare_digest(candidate.hex(), h)


def ensure_admin(session_factory, username: str, password: str) -> None:
    with session_factory() as session:
        existing = session.scalar(select(User).where(User.username == username))
        if existing is None:
            session.add(User(username=username, password_hash=_hash_password(password), is_admin=True))
            session.commit()


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/api/health"):
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return JSONResponse(status_code=401, content={"detail": "Non authentifié"})

        with request.app.state.session_factory() as db:
            auth = db.scalar(select(AuthSession).where(AuthSession.token == token))
            expires = auth.expires_at if auth else None
            if expires and expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if auth is None or expires < datetime.now(timezone.utc):
                response = JSONResponse(status_code=401, content={"detail": "Session expirée"})
                response.delete_cookie(SESSION_COOKIE)
                return response
            request.state.user = auth.user

        return await call_next(request)


class LoginPayload(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    is_admin: bool


class CreateUserPayload(BaseModel):
    username: str
    password: str
    is_admin: bool = False


def _get_db(request: Request):
    with request.app.state.session_factory() as session:
        yield session


def _require_admin(request: Request) -> User:
    user: User | None = getattr(request.state, "user", None)
    if user is None or not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Accès réservé aux administrateurs")
    return user


@router.post("/connexion")
def login(payload: LoginPayload, request: Request, response: Response, db: Session = Depends(_get_db)):
    user = db.scalar(select(User).where(User.username == payload.username))
    if user is None or not _verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Identifiants incorrects")

    token = secrets.token_hex(32)
    lifetime = request.app.state.settings.session_lifetime_hours
    expires = datetime.now(timezone.utc) + timedelta(hours=lifetime)
    db.add(AuthSession(token=token, user_id=user.id, expires_at=expires))
    db.commit()

    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=lifetime * 3600,
        path="/",
    )
    return {"user": UserOut.model_validate(user, from_attributes=True)}


@router.post("/deconnexion")
def logout(request: Request, response: Response, db: Session = Depends(_get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        auth = db.scalar(select(AuthSession).where(AuthSession.token == token))
        if auth:
            db.delete(auth)
            db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"detail": "Déconnecté"}


@router.get("/moi")
def me(request: Request) -> dict:
    user: User | None = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Non authentifié")
    return {"user": UserOut.model_validate(user, from_attributes=True)}


@router.post("/utilisateurs", status_code=201)
def create_user(
    payload: CreateUserPayload,
    _admin: User = Depends(_require_admin),
    db: Session = Depends(_get_db),
):
    if db.scalar(select(User).where(User.username == payload.username)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Cet utilisateur existe déjà")
    user = User(username=payload.username, password_hash=_hash_password(payload.password), is_admin=payload.is_admin)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"user": UserOut.model_validate(user, from_attributes=True)}


@router.get("/utilisateurs")
def list_users(_admin: User = Depends(_require_admin), db: Session = Depends(_get_db)):
    users = db.scalars(select(User).order_by(User.username)).all()
    return [UserOut.model_validate(u, from_attributes=True) for u in users]
