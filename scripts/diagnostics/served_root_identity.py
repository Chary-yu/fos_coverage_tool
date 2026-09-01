"""Verify that HTTP static content is served from the validated release root."""

from __future__ import print_function

import hashlib
import os
import re
import urllib.parse
import urllib.request


_REFERENCE_RE = re.compile(
    r"(?:src|href)\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))",
    re.IGNORECASE,
)


def _real(path):
    return os.path.realpath(os.path.abspath(str(path)))


def _inside(root, path):
    try:
        return os.path.commonpath((_real(root), _real(path))) == _real(root)
    except ValueError:
        return False


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_bytes(path):
    with open(path, "rb") as stream:
        return stream.read()


def _url_prefix(value):
    prefix = str(value or "/coverage/").strip()
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if not prefix.endswith("/"):
        prefix += "/"
    return prefix


def _relative_url_path(url, prefix):
    path = urllib.parse.urlsplit(url).path or "/"
    if not path.startswith(prefix):
        raise ValueError(
            "HTTP Served Root URL is outside configured prefix {}: {}".format(
                prefix, path
            )
        )
    relative = path[len(prefix):].lstrip("/")
    return relative or "index.html"


def _local_payload_path(release_root, relative):
    relative = str(relative or "").replace("\\", "/")
    if not relative or relative.startswith("/") or ".." in relative.split("/"):
        raise ValueError("HTTP Served Root reference escapes the release root")
    reports_path = _real(os.path.join(release_root, "reports", *relative.split("/")))
    if _inside(os.path.join(release_root, "reports"), reports_path) and \
            os.path.isfile(reports_path):
        return reports_path
    root_path = _real(os.path.join(release_root, *relative.split("/")))
    if _inside(release_root, root_path) and os.path.isfile(root_path):
        return root_path
    raise ValueError(
        "HTTP Served Root file is not present in validated release: {}".format(
            relative
        )
    )


def _fetch(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        status = int(getattr(response, "status", 200))
        body = response.read()
    if status != 200:
        raise RuntimeError("HTTP Served Root probe returned HTTP {}: {}".format(status, url))
    return body


def verify_http_served_root(
        probe_url, release_root, configured_served_root_path="",
        url_prefix="/coverage/", relative_path=""):
    """Compare a real HTTP report and same-origin assets with CURRENT bytes.

    ``configured_served_root_path`` must be the literal path used by the
    static server (normally ``publish_root/CURRENT/reports``).  The helper
    additionally fetches the report at ``probe_url`` and every same-origin
    ``src``/``href`` reference in that report, comparing response bytes with
    the corresponding files below the validated release root.
    """
    probe_url = str(probe_url or "").strip()
    if not probe_url:
        raise ValueError("served_root_probe_url is required")
    parsed = urllib.parse.urlsplit(probe_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("served_root_probe_url must be an absolute HTTP(S) URL")
    release_root = _real(release_root)
    expected_static_root = _real(os.path.join(release_root, "reports"))
    if configured_served_root_path and _real(configured_served_root_path) != \
            expected_static_root:
        raise ValueError(
            "configured static Served Root does not match validated CURRENT release"
        )
    prefix = _url_prefix(url_prefix)
    target_relative = str(relative_path or "").strip().replace("\\", "/")
    if not target_relative:
        target_relative = _relative_url_path(probe_url, prefix)
    local_path = _local_payload_path(release_root, target_relative)
    observed = _fetch(probe_url)
    expected = _read_bytes(local_path)
    if observed != expected:
        raise ValueError(
            "HTTP Served Root bytes do not match CURRENT release: {}".format(
                target_relative
            )
        )

    references = []
    if local_path.lower().endswith((".html", ".htm")):
        text = observed.decode("utf-8", "replace")
        for match in _REFERENCE_RE.finditer(text):
            reference = next(value for value in match.groups() if value is not None)
            reference = str(reference).strip()
            if not reference or reference.startswith(("#", "data:", "javascript:")):
                continue
            reference_url = urllib.parse.urljoin(probe_url, reference)
            reference_parts = urllib.parse.urlsplit(reference_url)
            if (reference_parts.scheme, reference_parts.netloc) != \
                    (parsed.scheme, parsed.netloc):
                continue
            reference_relative = _relative_url_path(reference_url, prefix)
            reference_path = _local_payload_path(release_root, reference_relative)
            reference_observed = _fetch(reference_url)
            reference_expected = _read_bytes(reference_path)
            if reference_observed != reference_expected:
                raise ValueError(
                    "HTTP Served Root asset bytes do not match CURRENT release: {}".format(
                        reference_relative
                    )
                )
            references.append({
                "url": reference_url,
                "relative_path": reference_relative,
                "sha256": _sha256_bytes(reference_observed),
            })
    return {
        "status": "PASSED",
        "probe_url": probe_url,
        "url_prefix": prefix,
        "relative_path": target_relative,
        "sha256": _sha256_bytes(observed),
        "local_path": local_path,
        "referenced_assets": references,
    }
