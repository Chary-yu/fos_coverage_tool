import os

from app.release_publication import current_publication_identity
from app.reports.compatibility import REPORT_API_CONTRACT_VERSION


def _publication(application):
    upgrade = application.config.get("upgrade") or {}
    configured = upgrade.get("publish_root")
    if not configured:
        return {}
    root = str(configured)
    if not os.path.isabs(root):
        root = os.path.join(application.runtime.repo_root, root)
    return current_publication_identity(root)


def payload(application):
    result = {
        "release": application.runtime.release_identity,
        "api_contract_version": REPORT_API_CONTRACT_VERSION,
    }
    publication = _publication(application)
    if publication:
        result["publication"] = publication
    return result
