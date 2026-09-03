"""Mutation authorization and write-freeze boundary for VNext HTTP."""

import os

from app.upgrade.lifecycle import writes_are_frozen


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
AUTH_MUTATION_PROBE_PATH = "/api/coverage/auth/mutation-probe"


def _public_bind(config):
    host = str((config or {}).get("server", {}).get("host") or
               "127.0.0.1").strip().lower()
    return host not in _LOOPBACK_HOSTS


class MutationAuthorizer(object):
    def __init__(self, repo_root, config):
        self.repo_root = os.path.realpath(repo_root)
        self.config = config or {}
        self.auth = self.config.get("auth") or {}

    def authenticate_operator(self, headers, remote_address):
        """Authenticate an operator without applying the write freeze.

        Read-only operational endpoints remain available during an upgrade so
        operators can inspect jobs, metrics, routes, and exports while writes
        are drained.
        """
        # A caller that bypasses the canonical config loader must not
        # accidentally get an unauthenticated mutation surface.
        mode = str(self.auth.get("mode") or "reverse_proxy").lower()
        origin = headers.get("Origin", "")
        allowed_origins = self.auth.get("allowed_origins") or []
        if origin and allowed_origins and origin not in allowed_origins:
            return False, 403, "origin is not allowed"
        if mode == "disabled":
            if _public_bind(self.config):
                return False, 503, "disabled authentication is not allowed on a public bind"
            return True, 200, ""
        trusted = [str(item) for item in self.auth.get("trusted_proxy_addresses") or []]
        if str(remote_address or "") not in trusted:
            return False, 401, "mutation requires a trusted reverse proxy"
        header_name = self.auth.get("user_header") or "X-Remote-User"
        user = str(headers.get(header_name) or "").strip()
        if not user:
            return False, 401, "authenticated user is required"
        return True, 200, user

    def authorize_role(self, headers, remote_address, required_roles):
        if writes_are_frozen(self.repo_root, self.config):
            return False, 503, "writes are frozen for upgrade"
        allowed, status, identity = self.authenticate_operator(headers, remote_address)
        if not allowed:
            return False, status, identity
        if str(self.auth.get("mode") or "reverse_proxy").lower() == "disabled":
            return True, 200, identity or "anonymous"
        role_header = self.auth.get("role_header") or "X-Remote-Role"
        observed = str(headers.get(role_header) or "").strip().lower()
        role_map = self.auth.get("roles") or {}
        mapped = str(role_map.get(identity) or observed or "").strip().lower()
        required = {str(item).lower() for item in (required_roles or [])}
        if mapped not in required:
            return False, 403, "role is not permitted"
        return True, 200, identity

    def authorize_mutation(self, headers, remote_address):
        if writes_are_frozen(self.repo_root, self.config):
            return False, 503, "writes are frozen for upgrade"
        return self.authenticate_operator(headers, remote_address)

    def authorize(self, headers, remote_address):
        """Backward-compatible alias for mutation authorization."""
        return self.authorize_mutation(headers, remote_address)
