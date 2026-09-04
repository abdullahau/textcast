"""The one account: who signs in, and what the bookmarklet may do.

One person reads this library, so there is one row. It holds a username, a
password *hash*, the picture beside it, and two secrets that are neither the
password nor each other:

* ``session`` is what the cookie carries. The cookie used to carry the sign-in
  token itself, so the credential travelled on every request and changing it
  could not end a session. Rotating this signs every browser out.
* ``ingest_key`` is what the bookmarklet and the iPhone Shortcut carry, and it
  reaches exactly one route. It used to be the sign-in token, sitting in clear
  in a bookmarks bar with the power to delete the library.

The environment seeds the row and then stops mattering. `TEXTCAST_USERNAME`
and `TEXTCAST_AUTH_TOKEN` are read once, into an empty table; after that the
answer lives in the database, where the Settings page can change it. Editing
`.env` later does nothing, which is worth knowing before you try it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass

#: scrypt is in the standard library, so a login one person uses needs no
#: argon2 or bcrypt wheel. 16 MiB and about a tenth of a second here, which is
#: the point of it.
_N, _R, _P, _DKLEN = 2**14, 8, 1, 32

USERNAME_MAX = 64
PASSWORD_MIN = 8


@dataclass(frozen=True)
class Account:
    username: str
    password_hash: str
    avatar: str
    session: str
    ingest_key: str

    @property
    def has_photo(self) -> bool:
        return bool(self.avatar)


def new_secret(prefix: str) -> str:
    """A key that says what it is, so one is never mistaken for the other."""
    return f"{prefix}_{secrets.token_urlsafe(24)}"


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time, and false rather than raising on anything malformed."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(digest) // 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


def get(conn: sqlite3.Connection) -> Account | None:
    row = conn.execute(
        "SELECT username, password_hash, avatar, session, ingest_key FROM account WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return Account(
        username=row["username"],
        password_hash=row["password_hash"],
        avatar=row["avatar"] or "",
        session=row["session"],
        ingest_key=row["ingest_key"],
    )


def create(conn: sqlite3.Connection, username: str, password: str) -> Account:
    """Write the one row. Only ever called against an empty table."""
    from .db import now

    conn.execute(
        """
        INSERT INTO account (id, username, password_hash, avatar, session, ingest_key, updated_at)
        VALUES (1,?,?,'',?,?,?)
        """,
        (
            username.strip() or "textcast",
            hash_password(password),
            new_secret("tcs"),
            new_secret("tci"),
            now(),
        ),
    )
    conn.commit()
    account = get(conn)
    assert account is not None
    return account


def _update(conn: sqlite3.Connection, **columns) -> Account:
    from .db import now

    columns["updated_at"] = now()
    assignments = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(f"UPDATE account SET {assignments} WHERE id = 1", tuple(columns.values()))
    conn.commit()
    account = get(conn)
    assert account is not None
    return account


def set_username(conn: sqlite3.Connection, username: str) -> Account:
    name = " ".join(username.split())
    if not name:
        raise ValueError("A username cannot be blank.")
    if len(name) > USERNAME_MAX:
        raise ValueError(f"A username is at most {USERNAME_MAX} characters.")
    return _update(conn, username=name)


def set_password(conn: sqlite3.Connection, password: str) -> Account:
    """A new password ends every session, including the one changing it.

    The caller writes a fresh cookie from the account it gets back, so the
    browser doing the changing stays signed in and every other one does not.
    That is the point of changing it.
    """
    if len(password) < PASSWORD_MIN:
        raise ValueError(f"A password is at least {PASSWORD_MIN} characters.")
    return _update(conn, password_hash=hash_password(password), session=new_secret("tcs"))


def set_avatar(conn: sqlite3.Connection, filename: str) -> Account:
    return _update(conn, avatar=filename)


def rotate_ingest_key(conn: sqlite3.Connection) -> Account:
    return _update(conn, ingest_key=new_secret("tci"))


def rotate_session(conn: sqlite3.Connection) -> Account:
    """End every session, without changing the password.

    Signing out only deletes the cookie in the browser doing it, which is
    right: the phone should not be signed out because the laptop was. But it
    means a cookie copied off a machine goes on working, and until this the
    only way to stop it was to change the password. The caller writes a fresh
    cookie from the account it gets back if it wants to stay signed in.
    """
    return _update(conn, session=new_secret("tcs"))
