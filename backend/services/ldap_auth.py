"""
Domain (Active Directory) authentication via direct LDAP bind.

No service account and no privileged directory access: the user's own
credentials are verified by attempting a SIMPLE bind as
``username@LDAP_DOMAIN``, and the same bound connection is then used to read
the user's own displayName. Group membership is never read — application
rights always come from the app_user table (see auth_ep.login for the
provisioning rules).

The flags are re-read from the environment on every call so the mode can be
flipped in .env.v2 / docker env without code changes, and so tests can toggle
them with monkeypatch.setenv.
"""

import logging
import os

logger = logging.getLogger(__name__)


def ldap_enabled() -> bool:
    return os.getenv("LDAP_ENABLED", "false").lower() == "true"


def ldap_authenticate(username: str, password: str) -> tuple[bool, str | None]:
    """(bind_ok, display_name_from_AD_or_None).

    An empty password is rejected explicitly: AD treats a bind with an empty
    password as an anonymous ("unauthenticated") bind and reports success,
    which would let anyone in with just a valid username.

    get_info=DSA fetches only the rootDSE (a single small entry) instead of
    ldap3's default SCHEMA, which downloads the entire AD schema and makes
    every login take seconds.
    """
    if not username or not password:
        return False, None

    server_url = os.getenv("LDAP_SERVER")
    domain = os.getenv("LDAP_DOMAIN")
    if not server_url or not domain:
        logger.warning("LDAP_ENABLED but LDAP_SERVER/LDAP_DOMAIN are not configured")
        return False, None

    use_ssl = os.getenv("LDAP_USE_SSL", "false").lower() == "true"

    try:
        from ldap3 import Connection, Server, DSA, SIMPLE
        from ldap3.core.exceptions import LDAPException
        from ldap3.utils.conv import escape_filter_chars

        server = Server(server_url, use_ssl=use_ssl, get_info=DSA, connect_timeout=5)
        try:
            conn = Connection(
                server,
                user=f"{username}@{domain}",
                password=password,
                authentication=SIMPLE,
                auto_bind=True,
                receive_timeout=10,
            )
        except LDAPException:
            # Wrong credentials, locked/disabled AD account, server refused, …
            return False, None

        # Bind succeeded — try to read the user's own displayName. Any failure
        # here must not fail the login: the name is a nicety, not a gate.
        display_name = None
        try:
            base_dn = None
            if server.info and server.info.other.get("defaultNamingContext"):
                base_dn = server.info.other["defaultNamingContext"][0]
            if base_dn:
                upn = escape_filter_chars(f"{username}@{domain}")
                if conn.search(
                    search_base=base_dn,
                    search_filter=f"(userPrincipalName={upn})",
                    attributes=["displayName"],
                    size_limit=1,
                ) and conn.entries:
                    value = conn.entries[0].displayName.value
                    if value:
                        display_name = str(value)
        except LDAPException as e:
            logger.info(f"LDAP displayName lookup failed for {username}: {e}")

        conn.unbind()
        return True, display_name
    except Exception as e:  # server unreachable, TLS failure, import error, …
        logger.warning(f"LDAP authentication unavailable: {e}")
        return False, None
