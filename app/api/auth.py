"""Mutation authorization and write-freeze boundary for VNext HTTP."""

import os

from app.upgrade.lifecycle import writes_are_frozen


class MutationAuthorizer(object):
    def __init__(self, repo_root, config):
        self.repo_root = os.path.realpath(repo_root)
        self.config = config or {}
        self.auth = self.config.get("auth") or {}

    def authorize(self, headers, remote_address):
        if writes_are_frozen(self.repo_root, self.config):
            return False, 503, "writes are frozen for upgrade"
        mode = str(self.auth.get("mode") or "disabled").lower()
        origin = headers.get("Origin", "")
        allowed_origins = self.auth.get("allowed_origins") or []
        if origin and allowed_origins and origin not in allowed_origins:
            return False, 403, "origin is not allowed"
        if mode == "disabled":
            return True, 200, ""
        trusted = [str(item) for item in self.auth.get("trusted_proxy_addresses") or []]
        if str(remote_address or "") not in trusted:
            return False, 401, "mutation requires a trusted reverse proxy"
        header_name = self.auth.get("user_header") or "X-Remote-User"
        user = str(headers.get(header_name) or "").strip()
        if not user:
            return False, 401, "authenticated user is required"
        return True, 200, user
