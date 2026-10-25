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
    session.add(User(username=DEFAULT_USERNAME, password_hash=hash_password(DEFAULT_PASSWORD), is_admin=True))
    return True


def user_exists(session: Session, username: str) -> bool:
    return session.scalar(select(User.id).where(User.username == username)) is not None


def is_admin(session: Session, username: str) -> bool:
    return bool(session.scalar(select(User.is_admin).where(User.username == username)))


def create_user(session: Session, username: str, password: str, *, admin: bool = False) -> User:
    username = username.strip()
    _check_username(session, username)
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError("The password needs at least 8 characters.")
    user = User(username=username, password_hash=hash_password(password), is_admin=admin)
    session.add(user)
    return user


def rename_user(session: Session, user: User, username: str) -> None:
    """The person signs in with the new name from then on; their open sessions end. The audit log keeps the old
    name in its past entries."""
    username = username.strip()
    if username != user.username:
        _check_username(session, username, user)
        user.username = username


def _check_username(session: Session, username: str, account: User | None = None) -> None:
    """A name that differs from another account's only in upper and lower case is taken too: people would mix
    them up, and sign in with the wrong one."""
    if not _USERNAME.match(username):
        raise AccountError("Use 3 to 60 letters, numbers, dots or dashes for the user name.")
    others = select(User.username).where(User.id != account.id) if account else select(User.username)
    if any(name.casefold() == username.casefold() for name in session.scalars(others)):
        raise AccountError("This user name is already taken.")


def set_password(session: Session, username: str, password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError("The password needs at least 8 characters.")
    user = session.scalar(select(User).where(User.username == username))
    if user is None:
        raise AccountError("This user no longer exists.")
    user.password_hash = hash_password(password)


def set_admin(session: Session, user: User, admin: bool) -> None:
    if user.is_admin and not admin:
        _keep_an_administrator(session)
    user.is_admin = admin


def remove_user(session: Session, user: User) -> None:
    """The person can't sign in any more, and their open sessions end. The audit log keeps their name."""
    if user.is_admin:
        _keep_an_administrator(session)
    session.delete(user)


def _keep_an_administrator(session: Session) -> None:
    """Someone has to be able to add accounts and manage the database."""
    if session.scalar(select(func.count()).select_from(User).where(User.is_admin)) <= 1:
        raise AccountError("At least one administrator has to remain.")


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == username.strip()))
    if user is None or not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.now()
    return user
