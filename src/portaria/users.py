"""Web UI accounts. Passwords are stored as salted scrypt hashes, never as text."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portaria.db import User

MIN_PASSWORD_LENGTH = 8
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"  # only for the first sign-in: shorter than the minimum, so it can't be set again
_USERNAME = re.compile(r"^[\w.@-]{3,60}$")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


class AccountError(ValueError):
    """The message is English UI text, translated where it is shown."""


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), **_SCRYPT)
    return hmac.compare_digest(candidate, base64.b64decode(digest))


def has_users(session: Session) -> bool:
    return bool(session.scalar(select(func.count()).select_from(User)))


def ensure_default_user(session: Session) -> bool:
    """Creates admin/admin when there are no accounts yet. Returns whether it did."""
    if has_users(session):
        return False
    session.add(User(username=DEFAULT_USERNAME, password_hash=hash_password(DEFAULT_PASSWORD)))
    return True


def user_exists(session: Session, username: str) -> bool:
    return session.scalar(select(User.id).where(User.username == username)) is not None


def create_user(session: Session, username: str, password: str) -> User:
    username = username.strip()
    if not _USERNAME.match(username):
        raise AccountError("Use 3 to 60 letters, numbers, dots or dashes for the user name.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError("The password needs at least 8 characters.")
    if session.scalar(select(User).where(User.username == username)):
        raise AccountError("This user name is already taken.")
    user = User(username=username, password_hash=hash_password(password))
    session.add(user)
    return user


def set_password(session: Session, username: str, password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError("The password needs at least 8 characters.")
    user = session.scalar(select(User).where(User.username == username))
    if user is None:
        raise AccountError("This user no longer exists.")
    user.password_hash = hash_password(password)


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == username.strip()))
    if user is None or not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.now()
    return user
