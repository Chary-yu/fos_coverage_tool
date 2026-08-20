"""Code Detail service entrypoint used by VNext composition."""

from code_detail_service import CodeDetailService as _LegacyCodeDetailService


class CodeDetailService(_LegacyCodeDetailService):
    """Application-owned name retaining the tested Lazy Collapse contract."""

    runtime_owner = "app.code_detail.service"
