"""Air-gapped real-browser release validation transport.

This module is intentionally transport-scoped and dormant unless the runtime
configuration explicitly enables ``upgrade.airgapped_operator_browser``.
It lets an authenticated operator use an ordinary Chromium-based browser
against the isolated Candidate Gateway, without installing Playwright or a
browser runtime on the production host.

The browser never chooses release identity or evidence output paths.  Those
values are recovered server-side from the immutable publication and the
pinned upgrade configuration.  Evidence is written atomically and bound to
one release-validation session.
"""

from __future__ import print_function

import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from app.api.auth import AUTH_MUTATION_PROBE_PATH
from app.api.endpoints import release as release_endpoint


PAGE_PATH = "/api/coverage/release-validation/page.html"
CONTEXT_PATH = "/api/coverage/release-validation/context"
SUBMIT_PATH = "/api/coverage/release-validation/browser-evidence"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    temporary = "{}.part-{}".format(path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass
    os.replace(temporary, path)


def _within(path, root):
    try:
        return os.path.commonpath([
            os.path.realpath(os.path.abspath(path)),
            os.path.realpath(os.path.abspath(root)),
        ]) == os.path.realpath(os.path.abspath(root))
    except (AttributeError, OSError, ValueError):
        return False


def _upgrade(application):
    return dict((application.config or {}).get("upgrade") or {})


def _profile(application):
    value = _upgrade(application).get("airgapped_operator_browser") or {}
    return dict(value) if isinstance(value, dict) else {}


def enabled(application):
    profile = _profile(application)
    return bool(profile.get("enabled")) and str(
        profile.get("mode") or ""
    ).strip() == "operator_browser"


def _publication(application):
    payload = release_endpoint.payload(application)
    release = payload.get("release") if isinstance(payload, dict) else None
    publication = payload.get("publication") if isinstance(payload, dict) else None
    if not isinstance(release, dict) or not isinstance(publication, dict):
        raise RuntimeError("immutable Candidate publication identity is unavailable")
    session_id = str(publication.get("release_validation_session_id") or "").strip()
    candidate_sha = str(release.get("commit_sha") or "").strip().lower()
    artifact_sha = str(publication.get("candidate_artifact_sha256") or "").strip().lower()
    served_sha = str(publication.get("served_root_sha256") or "").strip().lower()
    if not session_id:
        raise RuntimeError("release-validation session identity is unavailable")
    if not _SHA1_RE.fullmatch(candidate_sha):
        raise RuntimeError("Candidate commit identity is unavailable")
    if not _SHA256_RE.fullmatch(artifact_sha):
        raise RuntimeError("Candidate artifact SHA256 is unavailable")
    if not _SHA256_RE.fullmatch(served_sha):
        raise RuntimeError("Served Root SHA256 is unavailable")
    return payload, release, publication


def _allowed_evidence_roots(application):
    upgrade = _upgrade(application)
    roots = []
    for value in upgrade.get("validation_residue_roots") or []:
        if value:
            roots.append(os.path.realpath(os.path.abspath(str(value))))
    candidate_root = upgrade.get("production_candidate_root")
    if candidate_root:
        roots.append(os.path.realpath(os.path.dirname(os.path.abspath(str(candidate_root)))))
    return sorted(set(roots))


def _attempt_path(application, field, session_id):
    raw = str(_upgrade(application).get(field) or "").strip()
    if not raw:
        raise RuntimeError("upgrade.{} is required".format(field))
    raw = raw.replace("{attempt_id}", session_id)
    if not os.path.isabs(raw):
        raise RuntimeError("upgrade.{} must be absolute".format(field))
    path = os.path.realpath(os.path.abspath(raw))
    roots = _allowed_evidence_roots(application)
    if not roots or not any(_within(path, root) for root in roots):
        raise RuntimeError("upgrade.{} escapes validation evidence roots".format(field))
    return path


def _gateway_config_sha256(application):
    integration = _upgrade(application).get("production_integration") or {}
    gateway = integration.get("candidate_gateway") or {}
    path = str(gateway.get("config_path") or "").strip()
    if not path or not os.path.isabs(path) or not os.path.isfile(path):
        raise RuntimeError("Candidate Gateway config is unavailable")
    return _sha256_file(path)


def _state(application, session_id):
    state = getattr(application, "_airgapped_release_validation_state", None)
    if not isinstance(state, dict) or state.get("session_id") != session_id:
        state = {
            "session_id": session_id,
            "nonce": secrets.token_hex(32),
            "created_at": time.time(),
            "submitted": False,
        }
        setattr(application, "_airgapped_release_validation_state", state)
    return state


def _canonical_candidate_url(application):
    value = str(_upgrade(application).get("candidate_browser_url") or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError("upgrade.candidate_browser_url is not an absolute HTTP(S) URL")
    if not str(parsed.path or "").lower().endswith((".html", ".htm")):
        raise RuntimeError("upgrade.candidate_browser_url must identify an HTML report")
    if parsed.username or parsed.password:
        raise RuntimeError("upgrade.candidate_browser_url must not embed credentials")
    return value


def _public_context(application):
    payload, release, publication = _publication(application)
    session_id = str(publication.get("release_validation_session_id"))
    state = _state(application, session_id)
    profile = _profile(application)
    auth = application.config.get("auth") or {}
    browser_path = _attempt_path(application, "candidate_browser_evidence_path", session_id)
    auth_path = _attempt_path(application, "candidate_auth_probe_evidence_path", session_id)
    workload_path = browser_path + ".workload.json"
    timeout_sec = int(profile.get("operator_timeout_sec") or 1200)
    if timeout_sec < 60 or timeout_sec > 7200:
        raise RuntimeError("airgapped operator browser timeout must be between 60 and 7200 seconds")
    return {
        "schema_version": 1,
        "mode": "airgapped_operator_browser",
        "candidate_url": _canonical_candidate_url(application),
        "page_path": PAGE_PATH,
        "context_path": CONTEXT_PATH,
        "submit_path": SUBMIT_PATH,
        "candidate_revision": str(release.get("commit_sha") or ""),
        "release_validation_session_id": session_id,
        "candidate_artifact_sha256": str(publication.get("candidate_artifact_sha256") or ""),
        "served_root_sha256": str(publication.get("served_root_sha256") or ""),
        "previous_release_commit_sha": str(publication.get("previous_release_commit_sha") or ""),
        "served_root_tree_sha256": str(publication.get("served_root_tree_sha256") or ""),
        "served_root_identity_sha256": str(publication.get("served_root_identity_sha256") or ""),
        "release_identity": release,
        "publication": publication,
        "auth_mode": str(auth.get("mode") or "").strip().lower(),
        "user_header": str(auth.get("user_header") or "X-Remote-User").strip(),
        "gateway_config_sha256": _gateway_config_sha256(application),
        "nonce": state.get("nonce"),
        "submitted": bool(state.get("submitted")),
        "expires_at": float(state.get("created_at") or time.time()) + timeout_sec,
        "browser_evidence_path": browser_path,
        "browser_workload_path": workload_path,
        "auth_evidence_path": auth_path,
    }


def _permission_status(exc):
    raw = str(exc or "")
    for value in (401, 403, 503):
        if raw.startswith("{}:".format(value)):
            return value
    return 403


def _require_operator(application, headers, remote_address, mutation=False):
    try:
        if mutation:
            identity = application._require_mutation(headers, remote_address)
        else:
            identity = application._require_operator(headers, remote_address)
        return identity, None
    except PermissionError as exc:
        return "", (_permission_status(exc), {
            "error": "forbidden",
            "message": "release validation requires authenticated operator access",
        })


def _negative_mutation_probe(candidate_url):
    parsed = urlparse(candidate_url)
    origin = "{}://{}".format(parsed.scheme, parsed.netloc)
    url = origin + AUTH_MUTATION_PROBE_PATH
    request = urllib.request.Request(
        url, data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (OSError, ValueError, urllib.error.URLError):
        return 0


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and \
        value == value and value not in (float("inf"), float("-inf"))


def _validate_submission(context, body, operator):
    errors = []
    if not isinstance(body, dict):
        return ["browser evidence submission must be a JSON object"]
    if body.get("nonce") != context.get("nonce"):
        errors.append("release-validation nonce mismatch")
    for field in (
            "candidate_revision", "release_validation_session_id",
            "candidate_artifact_sha256", "served_root_sha256"):
        if body.get(field) != context.get(field):
            errors.append("{} does not match immutable Candidate context".format(field))
    if body.get("candidate_url") != context.get("candidate_url"):
        errors.append("Candidate report URL does not match configured Candidate Gateway")

    functional = body.get("browser_functional") or {}
    workload = body.get("coverage_virtual_scroll_100k") or {}
    environment = workload.get("environment_identity") or {}
    if functional.get("status") != "PASSED":
        errors.append("browser functional validation did not pass")
    if workload.get("status") != "PASSED":
        errors.append("100k browser workload did not pass")
    if workload.get("workload_id") != "vnext-real-http-virtual-scroll-100k-v1":
        errors.append("browser workload identity is invalid")
    if int(workload.get("line_count") or 0) != 100000:
        errors.append("browser workload did not exercise exactly 100000 lines")
    if environment.get("browser_name") != "chromium":
        errors.append("release browser must be Chromium/Chrome/Edge based")
    if workload.get("virtualized") is not True:
        errors.append("100k browser workload did not use virtual rendering")
    if int(workload.get("resident_js_lines_peak") or 0) > 8000:
        errors.append("100k browser resident JS lines exceeded 8000")
    if int(workload.get("max_dom_lines") or 0) >= 1500:
        errors.append("100k browser DOM lines reached 1500 or more")
    if not _finite_number(workload.get("p95_expand_ms")):
        errors.append("browser p95 expansion time is missing")
    sweep = workload.get("sweep") or []
    expected_targets = [25000, 50000, 75000, 100000, 1]
    if [int(item.get("target_line") or 0) for item in sweep if isinstance(item, dict)] != expected_targets:
        errors.append("browser sweep target sequence is invalid")
    if not sweep or not all(bool(item.get("visible")) for item in sweep if isinstance(item, dict)):
        errors.append("browser sweep did not render every target line")

    authenticated = body.get("authenticated_probe") or {}
    status_code = authenticated.get("status_code")
    probe = authenticated.get("payload") or {}
    if type(status_code) is not int or status_code < 200 or status_code >= 300:
        errors.append("authenticated mutation probe HTTP status is not successful")
    if probe.get("mutation_probe") is not True or \
            probe.get("probe_path") != AUTH_MUTATION_PROBE_PATH:
        errors.append("authenticated mutation probe backend contract was not observed")
    authenticated_user = str(probe.get("authenticated_user") or "").strip()
    if not authenticated_user:
        errors.append("authenticated mutation probe did not expose backend identity")
    if authenticated_user and operator and authenticated_user != operator:
        errors.append("browser operator identity does not match backend mutation identity")
    if probe.get("database_mutation") is not False:
        errors.append("authenticated mutation probe must remain zero-write")
    return errors


def _evidence_payloads(application, context, body, operator, negative_status):
    workload = dict(body.get("coverage_virtual_scroll_100k") or {})
    workload.update({
        "candidate_revision": context["candidate_revision"],
        "release_validation_session_id": context["release_validation_session_id"],
        "candidate_artifact_sha256": context["candidate_artifact_sha256"],
        "served_root_sha256": context["served_root_sha256"],
        "candidate_url": context["candidate_url"],
        "recorded_at": time.time(),
    })
    _atomic_json(context["browser_workload_path"], workload)
    workload_sha = _sha256_file(context["browser_workload_path"])

    browser = {
        "schema_version": 1,
        "status": "PASSED",
        "evidence_class": "real_http_chromium_browser",
        "gate": "gate-e",
        "synthetic": False,
        "release_eligible": True,
        "candidate_revision": context["candidate_revision"],
        "release_validation_session_id": context["release_validation_session_id"],
        "candidate_artifact_sha256": context["candidate_artifact_sha256"],
        "served_root_sha256": context["served_root_sha256"],
        "candidate_url": context["candidate_url"],
        "page_url": context["candidate_url"],
        "release_identity": context["release_identity"],
        "observed_publication": context["publication"],
        "browser_functional": dict(body.get("browser_functional") or {}),
        "coverage_virtual_scroll_100k": workload,
        "artifact_path": context["browser_workload_path"],
        "artifact_sha256": workload_sha,
        "report_artifact_path": context["browser_workload_path"],
        "report_artifact_sha256": workload_sha,
        "command_or_action": "air-gapped authenticated operator browser self-test",
        "operator_identity": operator,
        "credentials_recorded": False,
        "exit_code": 0,
        "recorded_at": time.time(),
    }

    authenticated = body.get("authenticated_probe") or {}
    probe = authenticated.get("payload") or {}
    parsed = urlparse(context["candidate_url"])
    origin = "{}://{}".format(parsed.scheme, parsed.netloc)
    auth = {
        "schema_version": 1,
        "status": "PASSED",
        "evidence_class": "real_candidate_authenticated_mutation",
        "release_eligible": True,
        "synthetic": False,
        "real_http": True,
        "candidate_revision": context["candidate_revision"],
        "release_validation_session_id": context["release_validation_session_id"],
        "candidate_artifact_sha256": context["candidate_artifact_sha256"],
        "served_root_sha256": context["served_root_sha256"],
        "candidate_url": context["candidate_url"],
        "page_url": context["candidate_url"],
        "release_url": origin + "/api/coverage/release",
        "mutation_url": origin + AUTH_MUTATION_PROBE_PATH,
        "auth_mode": context["auth_mode"],
        "user_header": context["user_header"],
        "identity_source": "candidate_gateway_authenticated_operator",
        "identity_propagated": bool(str(probe.get("authenticated_user") or "").strip()),
        "gateway_config_sha256": context["gateway_config_sha256"],
        "mutation_probe": {
            "status": "PASSED",
            "method": "POST",
            "endpoint": AUTH_MUTATION_PROBE_PATH,
            "probe_contract_observed": probe.get("mutation_probe") is True,
            "backend_identity_observed": bool(str(probe.get("authenticated_user") or "").strip()),
            "authenticated_user_present": bool(str(probe.get("authenticated_user") or "").strip()),
            "database_mutation": probe.get("database_mutation"),
            "authenticated_status_code": int(authenticated.get("status_code") or 0),
            "unauthenticated_status_code": int(negative_status or 0),
            "credentials_recorded": False,
        },
        "operator_identity": operator,
        "credentials_recorded": False,
        "exit_code": 0,
        "recorded_at": time.time(),
    }
    return browser, auth


def _submit(application, body, headers, remote_address):
    operator, denied = _require_operator(
        application, headers, remote_address, mutation=True
    )
    if denied:
        return denied
    try:
        context = _public_context(application)
    except RuntimeError as exc:
        return 409, {"error": "validation_context_unavailable", "message": str(exc)}
    state = _state(application, context["release_validation_session_id"])
    if state.get("submitted"):
        return 200, {
            "status": "PASSED",
            "already_submitted": True,
            "release_validation_session_id": context["release_validation_session_id"],
        }
    if time.time() > float(context.get("expires_at") or 0):
        return 409, {"error": "validation_expired", "message": "release-validation browser window expired"}
    errors = _validate_submission(context, body, operator)
    if errors:
        return 400, {"error": "invalid_browser_evidence", "violations": errors}
    negative_status = _negative_mutation_probe(context["candidate_url"])
    if negative_status not in (401, 403):
        return 409, {
            "error": "auth_negative_control_failed",
            "message": "unauthenticated Candidate mutation did not return 401/403",
            "observed_status": negative_status,
        }
    browser, auth = _evidence_payloads(
        application, context, body, operator, negative_status
    )
    _atomic_json(context["browser_evidence_path"], browser)
    _atomic_json(context["auth_evidence_path"], auth)
    state["submitted"] = True
    state["submitted_at"] = time.time()
    return 200, {
        "status": "PASSED",
        "release_validation_session_id": context["release_validation_session_id"],
        "browser_evidence_sha256": _sha256_file(context["browser_evidence_path"]),
        "auth_evidence_sha256": _sha256_file(context["auth_evidence_path"]),
        "credentials_recorded": False,
    }


def dispatch(application, method, path, query, body, headers, remote_address):
    """Return ``None`` when the path is not owned by this transport."""
    del query
    if path not in (PAGE_PATH, CONTEXT_PATH, SUBMIT_PATH):
        return None
    if not enabled(application):
        return 404, {"error": "not_found", "message": "resource not found"}
    if method == "GET" and path in (PAGE_PATH, CONTEXT_PATH):
        operator, denied = _require_operator(
            application, headers, remote_address, mutation=False
        )
        if denied:
            return denied
        try:
            context = _public_context(application)
        except RuntimeError as exc:
            return 409, {"error": "validation_context_unavailable", "message": str(exc)}
        if path == CONTEXT_PATH:
            public = dict(context)
            for field in (
                    "browser_evidence_path", "browser_workload_path",
                    "auth_evidence_path"):
                public.pop(field, None)
            public["operator_identity"] = operator
            return 200, public
        return 200, {"__html__": _VALIDATION_HTML}
    if method == "POST" and path == SUBMIT_PATH:
        return _submit(application, body, headers, remote_address)
    return 405, {"error": "method_not_allowed", "message": "method not allowed"}


_VALIDATION_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FOS R8 Candidate Browser Validation</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:24px;background:#f5f5f7;color:#1d1d1f}
main{max-width:920px;margin:auto;background:#fff;border-radius:18px;padding:24px;box-shadow:0 8px 28px rgba(0,0,0,.08)}
button{border:0;border-radius:10px;padding:10px 18px;font-weight:600;cursor:pointer}
pre{white-space:pre-wrap;background:#111;color:#ddd;padding:16px;border-radius:12px;max-height:360px;overflow:auto}
iframe{width:100%;height:520px;border:1px solid #ddd;border-radius:12px;margin-top:16px}
.ok{color:#16803a}.bad{color:#b42318}
</style>
</head>
<body><main>
<h2>FOS R8 Air-Gapped Candidate Validation</h2>
<p>本页只验证当前隔离 Candidate，不切换生产 CURRENT。</p>
<button id="run">开始真实浏览器验证</button>
<p id="status">等待开始</p>
<pre id="log"></pre>
<iframe id="candidate" title="Candidate report"></iframe>
<script>
(() => {
  const API='/api/coverage';
  const logNode=document.getElementById('log');
  const statusNode=document.getElementById('status');
  const frame=document.getElementById('candidate');
  const log=(m)=>{logNode.textContent+=`${m}\n`;logNode.scrollTop=logNode.scrollHeight;};
  const wait=(ms)=>new Promise(r=>setTimeout(r,ms));
  async function waitFor(fn,timeout=120000){const start=Date.now();for(;;){try{const v=fn();if(v)return v;}catch(_){ }if(Date.now()-start>timeout)throw new Error('等待 Candidate 页面超时');await wait(100);}}
  function browserIdentity(){const ua=navigator.userAgent||'';let product='other';if(/Edg\//.test(ua))product='edge';else if(/Chrome\//.test(ua))product='chrome';else if(/Chromium\//.test(ua))product='chromium';return{browser_name:product==='other'?product:'chromium',browser_product:product,user_agent:ua,platform:navigator.platform||'',language:navigator.language||''};}
  async function jsonFetch(url,options){const r=await fetch(url,Object.assign({cache:'no-store',credentials:'include'},options||{}));let p=null;try{p=await r.json();}catch(_){ }return{status:r.status,ok:r.ok,payload:p};}
  async function loadFrame(url){await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('Candidate HTML 加载超时')),120000);frame.onload=()=>{clearTimeout(timer);resolve();};frame.src=url+(url.includes('?')?'&':'?')+'fos_release_validation='+Date.now();});return frame.contentWindow;}
  async function workload(win){const doc=win.document;await waitFor(()=>doc.querySelector('pre.source'));await waitFor(()=>win.__COVERAGE_ENHANCE_INTERNALS__);const internals=win.__COVERAGE_ENHANCE_INTERNALS__;await waitFor(()=>internals.CodeRegionStore.getAll().some(r=>Number(r.lineCount)===100000||Number(r.endLine)-Number(r.startLine)+1===100000));const region=internals.CodeRegionStore.getAll().find(r=>Number(r.lineCount)===100000||Number(r.endLine)-Number(r.startLine)+1===100000);const errors=[];const apiPaths=[];const originalFetch=win.fetch.bind(win);win.fetch=async(...args)=>{try{const u=new URL(String(args[0]&&args[0].url||args[0]),win.location.href);if(u.pathname.startsWith(API))apiPaths.push(u.pathname);return await originalFetch(...args);}catch(e){errors.push(String(e&&e.message||e));throw e;}};const errorHandler=e=>errors.push(String(e&&e.message||'eval error'));win.addEventListener('error',errorHandler);const durations=[];const sweep=[];const started=performance.now();try{const t0=performance.now();await internals.CodeRegionController.expandRegion(region.id);durations.push(performance.now()-t0);if(!doc.querySelector('#L1'))throw new Error('首行未渲染');for(const target of [25000,50000,75000,100000,1]){const ts=performance.now();const bounds=internals.CodeRegionController.virtualWindowBounds(region,target-1);await internals.CodeRegionLoader.ensureVirtualWindow(internals.CodeRegionController.filePath,region,bounds.start,bounds.end);internals.CodeRegionController.renderVirtualWindow(region,target-1);durations.push(performance.now()-ts);sweep.push({target_line:target,visible:Boolean(doc.querySelector(`[id="L${target}"]`)),resident_js_lines:Number(region.loadedLineCount||0),dom_line_count:region.virtualContent?region.virtualContent.children.length:0});}}finally{win.fetch=originalFetch;win.removeEventListener('error',errorHandler);}const sorted=durations.slice().sort((a,b)=>a-b);const p95=sorted[Math.min(sorted.length-1,Math.ceil(sorted.length*.95)-1)];const telemetry=internals.PerformanceTelemetry.snapshot();const peak=Math.max(Number(region.loadedLineCount||0),...sweep.map(x=>x.resident_js_lines));const maxDom=Math.max(Number(telemetry.max_dom_lines||0),...sweep.map(x=>x.dom_line_count));const legacy=apiPaths.filter(p=>p===`${API}/batch`||p===`${API}/details`||p===`${API}/layout`);const passed=Boolean(region.virtualized)&&peak<=8000&&maxDom<1500&&sweep.every(x=>x.visible)&&errors.length===0&&legacy.length===0;return{status:passed?'PASSED':'FAILED',workload_id:'vnext-real-http-virtual-scroll-100k-v1',line_count:100000,logical_line_count:Number(region.lineCount||0),virtualized:Boolean(region.virtualized),time_to_target_line_ms:Number((performance.now()-started).toFixed(3)),p95_expand_ms:Number(p95.toFixed(3)),resident_js_lines:Number(region.loadedLineCount||0),resident_js_lines_peak:peak,dom_line_count:region.virtualContent?region.virtualContent.children.length:0,max_dom_lines:maxDom,sweep,telemetry,api_paths:apiPaths,workload_errors:errors,legacy_api_requests:legacy,environment_identity:browserIdentity()};}
  async function run(){document.getElementById('run').disabled=true;statusNode.textContent='验证进行中';statusNode.className='';logNode.textContent='';try{const contextResult=await jsonFetch(`${API}/release-validation/context`);if(!contextResult.ok)throw new Error(`Context HTTP ${contextResult.status}`);const ctx=contextResult.payload;log(`Session: ${ctx.release_validation_session_id}`);log(`Candidate: ${ctx.candidate_revision}`);const candidate=new URL(ctx.candidate_url,location.href);if(candidate.origin!==location.origin)throw new Error(`Candidate URL 与验证页不同源: ${candidate.origin} != ${location.origin}`);const win=await loadFrame(candidate.href);const releaseResult=await jsonFetch(`${API}/release`);if(!releaseResult.ok)throw new Error(`Release HTTP ${releaseResult.status}`);const served=(releaseResult.payload||{}).release||{};const publication=(releaseResult.payload||{}).publication||{};if(served.commit_sha!==ctx.candidate_revision)throw new Error('Release commit 不匹配');if(publication.release_validation_session_id!==ctx.release_validation_session_id)throw new Error('Publication session 不匹配');if(publication.candidate_artifact_sha256!==ctx.candidate_artifact_sha256)throw new Error('Candidate artifact SHA 不匹配');if(publication.served_root_sha256!==ctx.served_root_sha256)throw new Error('Served Root SHA 不匹配');const wl=await workload(win);log(`100k workload: ${wl.status}, p95=${wl.p95_expand_ms}ms, peak=${wl.resident_js_lines_peak}, DOM=${wl.max_dom_lines}`);const metricsResult=await jsonFetch(`${API}/metrics`);const metrics=metricsResult.payload||{};const cd=metrics.code_detail||{};const proc=metrics.process||{};const crossLayer=metricsResult.ok&&Number(cd.overlay_db_queries||0)>0&&Number(cd.sidecar_decode_count||0)>0&&Number(proc.peak_rss_bytes||0)>0;const functional={status:wl.status==='PASSED'&&crossLayer?'PASSED':'FAILED',release_identity_verified:true,publication_identity_verified:true,cross_layer_metrics_verified:crossLayer,metrics_status:metricsResult.status};if(functional.status!=='PASSED')throw new Error('Candidate functional/cross-layer gate 未通过');const authResult=await jsonFetch(`${API}/auth/mutation-probe`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(!authResult.ok)throw new Error(`Authenticated mutation probe HTTP ${authResult.status}`);const submission={nonce:ctx.nonce,candidate_revision:ctx.candidate_revision,release_validation_session_id:ctx.release_validation_session_id,candidate_artifact_sha256:ctx.candidate_artifact_sha256,served_root_sha256:ctx.served_root_sha256,candidate_url:ctx.candidate_url,browser_functional:functional,coverage_virtual_scroll_100k:wl,authenticated_probe:{status_code:authResult.status,payload:authResult.payload||{}}};const submit=await jsonFetch(`${API}/release-validation/browser-evidence`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(submission)});if(!submit.ok)throw new Error(`Evidence submit HTTP ${submit.status}: ${JSON.stringify(submit.payload)}`);statusNode.textContent='PASSED — 可以返回升级终端';statusNode.className='ok';log('Evidence submitted: PASSED');log(JSON.stringify(submit.payload,null,2));}catch(e){statusNode.textContent='FAILED — 不要继续切换生产';statusNode.className='bad';log(`ERROR: ${String(e&&e.stack||e)}`);}finally{document.getElementById('run').disabled=false;}}
  document.getElementById('run').addEventListener('click',run);
})();
</script>
</main></body></html>'''
