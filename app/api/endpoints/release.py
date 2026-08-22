from app.reports.compatibility import REPORT_API_CONTRACT_VERSION


def payload(application):
    return {
        "release": application.runtime.release_identity,
        "api_contract_version": REPORT_API_CONTRACT_VERSION,
    }
