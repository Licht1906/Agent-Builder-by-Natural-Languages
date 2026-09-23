"""LDAP/Active Directory bind — UC1-02 (#118), FR-05 (Should), spec.md §4.1/§19.

`POST /auth/ldap-bind` is a second door beside `POST /auth/login`, not a
replacement for it: this module decides which password check to run by reading
`settings.ldap_*`, and `/login` never consults them at all — so turning LDAP off
(blanking `ldap_server_url`) changes nothing about `/login`'s own behaviour
(AC: "tắt LDAP thì luồng local không đổi hành vi").

LDAP replaces only the password check. The account itself must already exist
locally (created the normal way, then activated) — this is not a provisioning
flow, just an alternate way to prove you are who your local account says you are.

`default_ldap_bind` is the one function allowed to open a real LDAP connection —
the same "one seam" shape `src.services.llm.get_chat_model_factory` uses for the
chat model. A test overrides `get_ldap_authenticator`; it never reaches into
`ldap3` itself.
"""

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.passwords import local_password_matches
from src.auth.registration import normalised_email
from src.config import Settings, get_settings
from src.db.models import User, UserStatus

#: `(email, password) -> bind succeeded`. Swapped out in tests via
#: `get_ldap_authenticator`, mirroring `src.services.llm.ChatModelFactory`.
LdapAuthenticator = Callable[[str, str], bool]


class InvalidCredentialsError(Exception):
    """The email/password pair did not check out, whichever check ran."""


class PendingAccountError(Exception):
    """The credentials were fine, but the account is not activated yet."""


def ldap_configured(settings: Settings | None = None) -> bool:
    """Whether an enterprise directory is configured at all (FR-05's fallback branch)."""
    settings = settings or get_settings()
    return bool(settings.ldap_server_url and settings.ldap_user_dn_template)


def default_ldap_bind(email: str, password: str) -> bool:
    """Bind to the configured directory as `email`, using `password`. `False` on any failure.

    Never raises: a directory that is unreachable, misconfigured, or simply
    refuses the bind all mean the same thing to a caller here — this credential
    did not check out — the same way a wrong local password does.
    """
    import ldap3  # type: ignore[import-untyped]
    from ldap3.core.exceptions import LDAPExceptionError  # type: ignore[import-untyped]

    settings = get_settings()
    user_dn = settings.ldap_user_dn_template.format(email=email)
    server = ldap3.Server(settings.ldap_server_url)
    try:
        connection = ldap3.Connection(server, user=user_dn, password=password)
        return connection.bind()
    except LDAPExceptionError:
        return False


def get_ldap_authenticator() -> LdapAuthenticator:
    """FastAPI dependency: the only function allowed to attempt an LDAP bind."""
    return default_ldap_bind


def authenticate(
    db: Session,
    email: str,
    password: str,
    *,
    ldap_authenticate: LdapAuthenticator,
) -> User:
    """The account `email`/`password` names, via LDAP when configured, local otherwise.

    Mirrors `POST /auth/login`'s own check exactly, but with the password half
    swapped for a directory bind: the account must already exist and not be
    `pending`, and a wrong credential and an unknown email look the same to the
    caller either way.

    `ldap_authenticate` runs unconditionally when LDAP is configured — even for
    an email no local account holds — for the same reason `local_password_matches`
    always hashes against something: an account that skips the directory round
    trip because it doesn't exist locally is a timing signal that tells a caller
    an email is unregistered before they have proven anything.
    """
    normalized = normalised_email(email)
    user = db.execute(select(User).where(User.email == normalized)).scalar_one_or_none()

    if ldap_configured():
        password_matches = ldap_authenticate(normalized, password) and user is not None
    else:
        password_matches = local_password_matches(user, password)

    if user is None or not password_matches:
        raise InvalidCredentialsError()
    if user.status == UserStatus.PENDING:
        raise PendingAccountError()
    return user
