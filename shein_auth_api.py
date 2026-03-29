import copy
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Page

from gmail import get_latest_shein_code_details

LOGIN_PAGE_PATH = "/user/auth/login?direction=nav"
POSITIONING_PATTERN = "/user/account/positioning"
COMMON_LOGIN_PATTERN = "/user/common_login"
RISK_RESOURCES_PATTERN = "/risk/verify/identity/validation/resources"
RISK_SEND_PATTERN = "/risk/verify/identity/validation/send"
RISK_CHECK_PATTERN = "/risk/verify/identity/validation/check"
HTTP_MODULE_PATTERN = "baseURL:gbCommonInfo.langPath"

DEFAULT_ALIAS_TYPE = 1
DEFAULT_PAGE_SOURCE = "continue"
DEFAULT_LOGIN_FROM = "login"
DEFAULT_DA_ID = "2-7-108"
DEFAULT_VERIFY_TIMEOUT_SEC = 180
EMAIL_CAPTCHA_EXTRA_PARAM = {"akka": "1000114"}

WEBPACK_AUTH_HELPER = r"""
() => {
  if (window.__sheinApiAuth) {
    return true;
  }

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  const helper = {
    req: null,
    moduleCache: Object.create(null),
    exportCache: Object.create(null),

    ensureReq() {
      if (this.req) {
        return this.req;
      }

      let captured = null;
      const chunk = (self.webpackChunkSHEIN_W = self.webpackChunkSHEIN_W || []);
      chunk.push([[Symbol("inspect-auth")], {}, function(req) {
        captured = req;
      }]);

      if (!captured) {
        throw new Error("Unable to capture SHEIN webpack runtime");
      }

      this.req = captured;
      return captured;
    },

    getModuleBySource(pattern) {
      if (this.moduleCache[pattern]) {
        return this.moduleCache[pattern];
      }

      const req = this.ensureReq();
      for (const id of Object.keys(req.m || {})) {
        const factory = req.m[id];
        const source = typeof factory === "function" ? String(factory) : "";
        if (!source.includes(pattern)) {
          continue;
        }

        try {
          const exports = req(id);
          const match = { id, exports };
          this.moduleCache[pattern] = match;
          return match;
        } catch (err) {
        }
      }

      return null;
    },

    findExport(exportsObj, pattern) {
      if (!exportsObj) {
        return null;
      }

      const candidates = [exportsObj, ...Object.values(exportsObj)];
      for (const candidate of candidates) {
        if (typeof candidate === "function" && String(candidate).includes(pattern)) {
          return candidate;
        }
      }

      return null;
    },

    getExportByPattern(pattern) {
      if (this.exportCache[pattern]) {
        return this.exportCache[pattern];
      }

      const moduleInfo = this.getModuleBySource(pattern);
      if (!moduleInfo) {
        return null;
      }

      const found = this.findExport(moduleInfo.exports, pattern);
      if (found) {
        this.exportCache[pattern] = found;
      }
      return found || null;
    },

    hasExport(pattern) {
      return !!this.getExportByPattern(pattern);
    },

    getHttpClient() {
      const moduleInfo = this.getModuleBySource("baseURL:gbCommonInfo.langPath");
      if (!moduleInfo || !moduleInfo.exports) {
        throw new Error("Unable to find SHEIN HTTP client");
      }

      const candidates = [moduleInfo.exports, ...Object.values(moduleInfo.exports)];
      for (const candidate of candidates) {
        if (typeof candidate === "function" && typeof candidate.updateXCsrfToken === "function") {
          return candidate;
        }
      }

      throw new Error("Unable to locate SHEIN HTTP client export");
    },

    async waitFor(check, timeoutMs = 12000, stepMs = 200) {
      const started = Date.now();
      while (Date.now() - started < timeoutMs) {
        try {
          if (await check()) {
            return true;
          }
        } catch (err) {
        }
        await sleep(stepMs);
      }
      return false;
    },

    async waitForBlackbox(timeoutMs = 12000) {
      return this.waitFor(() => !!(window._fmOpt && window._fmOpt.__blackbox), timeoutMs, 250);
    },

    async bootstrap() {
      const http = this.getHttpClient();

      let csrf = null;
      if (typeof http.updateXCsrfToken === "function") {
        csrf = await http.updateXCsrfToken().catch(() => null);
      }

      const ugid = await http({
        method: "GET",
        url: "/user-api/common/userinfo_ugid",
        useBffApi: true,
        timeout: 5000,
      }).catch(() => null);

      let fingerprint = null;
      if (
        window._GB_DeviceFingerPrint &&
        typeof window._GB_DeviceFingerPrint.callFuncPromise === "function"
      ) {
        fingerprint = await window._GB_DeviceFingerPrint.callFuncPromise().catch(() => null);
      }

      const hasBlackbox = await this.waitForBlackbox();
      return { csrf, ugid, fingerprint, hasBlackbox };
    },

    async callExport(args) {
      const fn = this.getExportByPattern(args.pattern);
      if (!fn) {
        throw new Error("Unable to find export for " + args.pattern);
      }
      return await fn(args.payload || {});
    },

    async callHttp(config) {
      const http = this.getHttpClient();
      return await http(config || {});
    },

    getCountryContext() {
      const selectors = [
        ".page-login__phoneArea",
        "[class*='phoneArea']",
        "[class*='PhoneArea']",
        "[class*='areaCode']",
        "[class*='AreaCode']",
        "button[aria-haspopup='listbox']",
      ];

      let rawText = "";
      for (const selector of selectors) {
        const node = document.querySelector(selector);
        if (!node) {
          continue;
        }

        const text = (node.textContent || "").trim();
        if (!text) {
          continue;
        }

        rawText = text;
        if (/[A-Z]{2}\s*\+\s*\d{1,4}/.test(text)) {
          break;
        }
      }

      const match = rawText.match(/([A-Z]{2})\s*\+\s*(\d{1,4})/);
      const globalInfo = typeof gbCommonInfo === "undefined" ? {} : gbCommonInfo;
      return {
        area_abbr: match ? match[1] : (globalInfo.countryAbbr || globalInfo.siteCountryAbbr || ""),
        area_code: match ? match[2] : "",
        raw_text: rawText,
      };
    },

    isReady() {
      try {
        return !!this.getHttpClient()
          && !!this.getExportByPattern("/user/account/positioning")
          && !!this.getExportByPattern("/user/common_login");
      } catch (err) {
        return false;
      }
    },
  };

  window.__sheinApiAuth = helper;
  return true;
}
"""


def _preferred_host(base_url: str) -> str:
    return (urlsplit(base_url).netloc or "").strip().lower()


def _force_url_host(url: str, base_url: str) -> str:
    if not url:
        return url

    parts = urlsplit(url)
    target_host = _preferred_host(base_url)
    current_host = (parts.netloc or "").strip().lower()
    if not target_host or not current_host or current_host == target_host:
        return url

    return urlunsplit((parts.scheme, target_host, parts.path, parts.query, parts.fragment))


def _goto_preferred(page: Page, url: str, base_url: str, wait_until: str = "domcontentloaded") -> None:
    page.goto(_force_url_host(url, base_url), wait_until=wait_until)


def _response_code(response: Optional[Dict[str, Any]]) -> str:
    return str((response or {}).get("code") or "")


def _response_message(response: Optional[Dict[str, Any]]) -> str:
    return str((response or {}).get("msg") or "")


def _raise_on_bad_response(step: str, response: Optional[Dict[str, Any]], ok_codes: tuple[str, ...] = ("0",)) -> None:
    code = _response_code(response)
    if code in ok_codes:
        return
    raise RuntimeError(f"{step} failed with code={code or 'unknown'} msg={_response_message(response)!r}")


def _debug_log(step: str, **fields: Any) -> None:
    parts = [f"{key}={fields[key]!r}" for key in sorted(fields)]
    print(f"[AUTH DEBUG] {step}: " + ", ".join(parts))


def _validation_summary(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    data = payload or {}
    info = (data.get("info") or {}) if isinstance(data, dict) else {}
    validate_param = {}

    if isinstance(data, dict):
        validate_param = data.get("validate_param") or {}
    if not validate_param and isinstance(info, dict):
        validate_param = info.get("validate_param") or {}

    return {
        "code": _response_code(data if isinstance(data, dict) else None),
        "msg": _response_message(data if isinstance(data, dict) else None),
        "validate_type": data.get("validate_type") if isinstance(data, dict) else None,
        "validate_scene": data.get("validate_scene") if isinstance(data, dict) else None,
        "validate_channel": data.get("validate_channel") if isinstance(data, dict) else None,
        "request_id": validate_param.get("request_id"),
        "msg_id": validate_param.get("msg_id"),
        "risk_id": validate_param.get("risk_id"),
        "ttl": validate_param.get("ttl"),
        "result": validate_param.get("result"),
        "email": validate_param.get("email"),
        "sdk_version": validate_param.get("sdk_version"),
    }


def _install_runtime_helper(page: Page) -> None:
    page.wait_for_function("() => !!window.webpackChunkSHEIN_W", timeout=30000)
    page.evaluate(WEBPACK_AUTH_HELPER)
    page.wait_for_function("() => window.__sheinApiAuth && window.__sheinApiAuth.isReady()", timeout=30000)


def _runtime_has_export(page: Page, pattern: str) -> bool:
    return bool(page.evaluate("(pattern) => window.__sheinApiAuth.hasExport(pattern)", pattern))


def _runtime_call_export(page: Page, pattern: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return page.evaluate(
        "(args) => window.__sheinApiAuth.callExport(args)",
        {"pattern": pattern, "payload": payload or {}},
    )


def _runtime_call_http(page: Page, config: Dict[str, Any]) -> Dict[str, Any]:
    return page.evaluate("(config) => window.__sheinApiAuth.callHttp(config)", config)


def _call_pattern_or_http(
    page: Page,
    pattern: str,
    payload: Dict[str, Any],
    *,
    use_bff_api: bool = False,
) -> Dict[str, Any]:
    try:
        if _runtime_has_export(page, pattern):
            return _runtime_call_export(page, pattern, payload)

        config: Dict[str, Any] = {
            "method": "POST",
            "url": pattern,
            "data": payload,
        }
        if use_bff_api:
            config["useBffApi"] = True
        return _runtime_call_http(page, config)
    except Exception as exc:
        _debug_log(
            "runtime call exception",
            pattern=pattern,
            page_url=page.url,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise


def _bootstrap_runtime(page: Page) -> Dict[str, Any]:
    return page.evaluate("() => window.__sheinApiAuth.bootstrap()")


def _country_context(page: Page) -> Dict[str, Any]:
    return page.evaluate("() => window.__sheinApiAuth.getCountryContext()")


def _positioning_payload(acc: Dict[str, str], country: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "alias": acc["shein_email"],
        "area_code": str(country.get("area_code") or ""),
        "area_abbr": str(country.get("area_abbr") or ""),
        "alias_type": DEFAULT_ALIAS_TYPE,
        "page_source": DEFAULT_PAGE_SOURCE,
        "login_from": DEFAULT_LOGIN_FROM,
    }


def _login_payload(
    acc: Dict[str, str],
    *,
    biz_uuid: str,
    validate_token: Optional[str] = None,
    risk_id: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "email": acc["shein_email"],
        "password": acc["shein_password"],
        "checkboxSubscrib": False,
        "biz_uuid": biz_uuid,
        "daId": DEFAULT_DA_ID,
        "login_from": DEFAULT_LOGIN_FROM,
        "clause_flag": 0,
        "clause_country_id": "",
    }
    if validate_token:
        payload["validate_token"] = validate_token
    if risk_id:
        payload["risk_id"] = risk_id
    return payload


def _risk_payload_from_login(login_response: Dict[str, Any]) -> Dict[str, Any]:
    info = (login_response or {}).get("info") or {}
    payload = copy.deepcopy(info.get("extend_info") or {})
    if not payload:
        raise RuntimeError("SHEIN login returned a risk challenge without extend_info")

    validate_param = payload.setdefault("validate_param", {})
    validate_param.setdefault("sdk_version", "0.35.0")
    payload["validate_channel"] = None
    return payload


def _ensure_gmail_credentials(acc: Dict[str, str]) -> None:
    if acc.get("gmail_email") and acc.get("gmail_app_password"):
        return
    raise RuntimeError("Gmail credentials are required when SHEIN requests email verification")


def _submit_email_verification(page: Page, acc: Dict[str, str], login_response: Dict[str, Any]) -> Dict[str, Any]:
    resources_payload = _risk_payload_from_login(login_response)
    _debug_log(
        "risk challenge received",
        page_url=page.url,
        risk_id=((login_response.get("info") or {}).get("risk_id") if login_response else None),
        validate_type=((login_response.get("info") or {}).get("extend_info") or {}).get("validate_type"),
        validate_scene=((login_response.get("info") or {}).get("extend_info") or {}).get("validate_scene"),
    )

    resources_started_at = time.time()
    resources_response = _call_pattern_or_http(page, RISK_RESOURCES_PATTERN, resources_payload)
    _debug_log(
        "risk resources response",
        elapsed_ms=int((time.time() - resources_started_at) * 1000),
        **_validation_summary(resources_response),
    )
    _raise_on_bad_response("risk resources", resources_response)

    send_payload = copy.deepcopy((resources_response.get("info") or {}) or resources_payload)
    send_payload.setdefault("validate_param", {}).setdefault("sdk_version", "0.35.0")
    verification_started_at = time.time()
    _debug_log(
        "risk send request",
        elapsed_since_verification_start_ms=0,
        **_validation_summary(send_payload),
    )
    send_response = _call_pattern_or_http(page, RISK_SEND_PATTERN, send_payload)
    _debug_log(
        "risk send response",
        elapsed_ms=int((time.time() - verification_started_at) * 1000),
        **_validation_summary(send_response),
    )
    _raise_on_bad_response("risk send", send_response)

    _ensure_gmail_credentials(acc)
    _debug_log(
        "gmail lookup starting",
        received_after_ts=verification_started_at,
        timeout_sec=DEFAULT_VERIFY_TIMEOUT_SEC,
        gmail_email_hint=(acc.get("gmail_email")[:3] + "***") if acc.get("gmail_email") else None,
    )
    try:
        code_details = get_latest_shein_code_details(
            acc["gmail_email"],
            acc["gmail_app_password"],
            timeout_sec=DEFAULT_VERIFY_TIMEOUT_SEC,
            received_after_ts=verification_started_at,
        )
    except Exception as exc:
        _debug_log(
            "gmail lookup exception",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise
    verification_code = (code_details or {}).get("code") if code_details else None
    if not verification_code:
        _debug_log("gmail lookup finished without code", page_url=page.url)
        raise RuntimeError("Verification code not found in Gmail")
    _debug_log(
        "gmail verification code selected",
        code_length=len(verification_code),
        code_suffix=verification_code[-2:],
        message_ts=(code_details or {}).get("message_ts"),
        verification_started_at=verification_started_at,
        subject=(code_details or {}).get("subject"),
        from_header=(code_details or {}).get("from"),
    )

    send_info = (send_response or {}).get("info") or {}
    check_payload = {
        "validate_channel": send_info.get("validate_channel") or send_payload.get("validate_channel"),
        "validate_type": send_info.get("validate_type") or send_payload.get("validate_type"),
        "validate_scene": send_info.get("validate_scene") or send_payload.get("validate_scene"),
        "validate_token": send_info.get("validate_token") or send_payload.get("validate_token"),
        "validate_param": copy.deepcopy(send_info.get("validate_param") or send_payload.get("validate_param") or {}),
    }
    check_payload["validate_param"]["verification_code"] = verification_code
    check_payload["validate_param"].setdefault("sdk_version", "0.35.0")
    check_payload["validate_param"]["extra_param"] = dict(EMAIL_CAPTCHA_EXTRA_PARAM)

    check_started_at = time.time()
    _debug_log(
        "risk check request",
        code_length=len(verification_code),
        code_suffix=verification_code[-2:],
        elapsed_since_send_ms=int((check_started_at - verification_started_at) * 1000),
        **_validation_summary(check_payload),
    )
    check_response = _call_pattern_or_http(page, RISK_CHECK_PATTERN, check_payload)
    _debug_log(
        "risk check response",
        elapsed_ms=int((time.time() - check_started_at) * 1000),
        **_validation_summary(check_response),
    )
    _raise_on_bad_response("risk check", check_response)
    return check_response


def ensure_logged_in_via_api(
    page: Page,
    base_url: str,
    acc: Dict[str, str],
    fetch_url: Optional[str] = None,
) -> None:
    base_url = base_url.rstrip("/")
    login_url = f"{base_url}{LOGIN_PAGE_PATH}"
    target_url = fetch_url or f"{base_url}/user/orders/list"

    _goto_preferred(page, login_url, base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)

    if "login" not in page.url.lower():
        _goto_preferred(page, target_url, base_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        return

    _install_runtime_helper(page)
    _bootstrap_runtime(page)

    country = _country_context(page)
    positioning_response = _runtime_call_export(page, POSITIONING_PATTERN, _positioning_payload(acc, country))
    _raise_on_bad_response("positioning", positioning_response)

    biz_uuid = str(((positioning_response.get("info") or {}).get("risk_id")) or "")
    if not biz_uuid:
        raise RuntimeError("SHEIN positioning did not return a risk_id for login")

    login_response = _runtime_call_export(page, COMMON_LOGIN_PATTERN, _login_payload(acc, biz_uuid=biz_uuid))
    code = _response_code(login_response)
    if code == "402922":
        _submit_email_verification(page, acc, login_response)

        risk_info = (login_response.get("info") or {}) if login_response else {}
        validate_token = str(((risk_info.get("extend_info") or {}).get("validate_token")) or "")
        risk_id = str(risk_info.get("risk_id") or "")
        if not validate_token or not risk_id:
            raise RuntimeError("SHEIN risk challenge did not return validate_token and risk_id")

        login_response = _runtime_call_export(
            page,
            COMMON_LOGIN_PATTERN,
            _login_payload(
                acc,
                biz_uuid=biz_uuid,
                validate_token=validate_token,
                risk_id=risk_id,
            ),
        )

    _raise_on_bad_response("common_login", login_response)

    _goto_preferred(page, f"{base_url}/user/index", base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    _goto_preferred(page, target_url, base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)

    if "login" in page.url.lower():
        raise RuntimeError("SHEIN login API flow completed but the session still redirected to login")
