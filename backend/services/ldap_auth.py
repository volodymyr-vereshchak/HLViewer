"""
Domain (Active Directory) authentication via direct LDAP bind.

No service account and no directory search: the user's own credentials are
verified by attempting a SIMPLE bind as ``username@LDAP_DOMAIN``. Group
membership is never read — application rights always come from the app_user
table (see auth_ep.login for the provisioning rules).

The flags are re-read from the environment on every call so the mode can be
flipped in .env.v2 / docker env without code changes, and so tests can toggle
them with monkeypatch.setenv.
"""

import logging
import os

logger = logging.getLogger(__name__)


def ldap_enabled() -> bool:
    return os.getenv("LDAP_ENABLED", "false").lower() == "true"


def ldap_authenticate(username: str, password: str) -> bool:
    """True only when the AD bind with these exact credentials succeeds.

    An empty password is rejected explicitly: AD treats a bind with an empty
    password as an anonymous ("unauthenticated") bind and reports success,
    which would let anyone in with just a valid username.
    """
    if not username or not password:
        return False

    server_url = os.getenv("LDAP_SERVER")
    domain = os.getenv("LDAP_DOMAIN")
    if not server_url or not domain:
        logger.warning("LDAP_ENABLED but LDAP_SERVER/LDAP_DOMAIN are not configured")
        return False

    use_ssl = os.getenv("LDAP_USE_SSL", "false").lower() == "true"

    try:
        from ldap3 import Connection, Server, SIMPLE
        from ldap3.core.exceptions import LDAPException

        server = Server(server_url, use_ssl=use_ssl, connect_timeout=5)
        try:
            conn = Connection(
                server,
                user=f"{username}@{domain}",
                password=password,
                authentication=SIMPLE,
                auto_bind=True,
                receive_timeout=10,
            )
            conn.unbind()
            return True
        except LDAPException:
            # Wrong credentials, locked/disabled AD account, server refused, …
            return False
    except Exception as e:  # server unreachable, TLS failure, import error, …
        logger.warning(f"LDAP authentication unavailable: {e}")
        return False
