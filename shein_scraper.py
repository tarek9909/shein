# shein_scraper.py
import os
import re
import json
from typing import Dict, Any, Optional, List

import anyio
from playwright.sync_api import sync_playwright, Page, TimeoutError

from gmail import get_latest_shein_code

PROFILES_DIR = "profiles"
DEFAULT_BASE_URL = "https://ar.shein.com"


# =========================
# SSR extraction + parsing
# =========================
def _extract_ssr_block(html: str) -> Optional[str]:
    """
    Extract the JS object assigned to gbOrdersTrackSsrData.
    Most common:
      window.gbOrdersTrackSsrData = {...};
    """
    if not html:
        return None

    patterns = [
        r"window\.gbOrdersTrackSsrData\s*=\s*(\{.*?\})\s*;</script>",
        r"\bgbOrdersTrackSsrData\s*=\s*(\{.*?\})\s*;</script>",
        r"window\[['\"]gbOrdersTrackSsrData['\"]\]\s*=\s*(\{.*?\})\s*;</script>",
    ]

    for pat in patterns:
        m = re.search(pat, html, flags=re.DOTALL)
        if m:
            return m.group(1)

    # fallback: locate token then brace-scan from first "{"
    idx = html.find("gbOrdersTrackSsrData")
    if idx == -1:
        return None
    start = html.find("{", idx)
    if start == -1:
        return None

    depth = 0
    for i in range(start, len(html)):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]
    return None


def _json_parse_ssr(ssr_text: str) -> Optional[dict]:
    """
    Best-effort parse. Often it's valid JSON.
    If it contains small JS quirks, apply safe fixes.
    """
    if not ssr_text:
        return None
    try:
        return json.loads(ssr_text)
    except Exception:
        pass

    fixed = ssr_text
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)  # trailing commas
    fixed = re.sub(r"\bundefined\b", "null", fixed)

    # Only attempt a quote swap if it looks safe-ish
    if "'" in fixed and '"' not in fixed:
        fixed = fixed.replace("'", '"')

    try:
        return json.loads(fixed)
    except Exception:
        return None


def _pull_pkg_from_json(ssr_json: dict) -> Optional[dict]:
    """
    Find a package/logistics dict that contains:
      track_num, carrier_name, logistics_tracks_list, track_url...
    """
    if not isinstance(ssr_json, dict):
        return None

    def deep_find(obj, depth=0):
        if depth > 7:
            return None
        if isinstance(obj, dict):
            if (
                "track_num" in obj
                or "logistics_tracks_list" in obj
                or "carrier_name" in obj
                or "track_url" in obj
            ):
                # ensure it looks like a logistics package
                if isinstance(obj.get("logistics_tracks_list", []), list) or "track_num" in obj:
                    return obj
            for v in obj.values():
                r = deep_find(v, depth + 1)
                if r:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = deep_find(v, depth + 1)
                if r:
                    return r
        return None

    return deep_find(ssr_json)


def _regex_value(ssr_text: str, key: str) -> Optional[str]:
    if not ssr_text:
        return None
    m = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]+)"', ssr_text)
    return m.group(1) if m else None


def _regex_first_details(ssr_text: str) -> Optional[str]:
    if not ssr_text:
        return None
    m = re.search(r'"details"\s*:\s*"([^"]+)"', ssr_text)
    return m.group(1) if m else None


def _regex_first_timestamp(ssr_text: str) -> Optional[str]:
    if not ssr_text:
        return None
    m = re.search(r'"timestamp"\s*:\s*"([^"]+)"', ssr_text)
    return m.group(1) if m else None


def _clean_token(v: Any) -> str:
    s = str(v or "").strip()
    return s


def _detect_split_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    low = text.lower()
    patterns = [
        r"(?:in|into)\s+(\d+)\s+(?:separate\s+)?(?:packages?|parcels?)",
        r"(\d+)\s+(?:separate\s+)?(?:packages?|parcels?)",
        r"(\d+)\s*(?:حزم|حزمة)",
        r"(\d+)\s*包裹",
    ]
    for pat in patterns:
        m = re.search(pat, low, flags=re.IGNORECASE)
        if m:
            try:
                n = int(m.group(1))
                if n >= 2:
                    return n
            except Exception:
                pass
    if any(k in low for k in ("separate packages", "split package", "split shipment", "حزم منفصلة", "包裹")):
        return 2
    return None


def _collect_split_info(ssr_json: Optional[dict], ssr_text: Optional[str], pkg: Optional[dict]) -> Dict[str, Any]:
    tracking_nos = set()
    package_refs = set()
    explicit_count = None

    if isinstance(pkg, dict):
        t = _clean_token(pkg.get("track_num"))
        if t:
            tracking_nos.add(t)
        p = _clean_token(pkg.get("package_no"))
        if p:
            package_refs.add(p)
        for e in pkg.get("logistics_tracks_list") or []:
            if isinstance(e, dict):
                n = _detect_split_from_text(str(e.get("details") or ""))
                if n and (explicit_count is None or n > explicit_count):
                    explicit_count = n

    def walk(obj, depth=0):
        nonlocal explicit_count
        if depth > 9:
            return
        if isinstance(obj, dict):
            if "shipping_no" in obj:
                t = _clean_token(obj.get("shipping_no"))
                if t:
                    tracking_nos.add(t)
            if "track_num" in obj:
                t = _clean_token(obj.get("track_num"))
                if t:
                    tracking_nos.add(t)
            if "package_no" in obj:
                p = _clean_token(obj.get("package_no"))
                if p:
                    package_refs.add(p)
            if "reference_number" in obj:
                p = _clean_token(obj.get("reference_number"))
                if p:
                    package_refs.add(p)
            if "details" in obj:
                n = _detect_split_from_text(str(obj.get("details") or ""))
                if n and (explicit_count is None or n > explicit_count):
                    explicit_count = n
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, depth + 1)

    if isinstance(ssr_json, dict):
        walk(ssr_json)

    if ssr_text:
        n = _detect_split_from_text(ssr_text)
        if n and (explicit_count is None or n > explicit_count):
            explicit_count = n

    count_by_data = max(len(tracking_nos), len(package_refs))
    split_count = explicit_count or count_by_data
    is_split = bool(split_count and split_count >= 2)

    return {
        "is_split": is_split,
        "split_count": int(split_count if split_count else 0),
        "all_tracking_numbers": sorted(tracking_nos),
        "all_package_refs": sorted(package_refs),
    }


# =========================
# Weight sum helpers
# =========================
def _to_float(x) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def _to_int(x) -> int:
    try:
        return int(float(x))
    except Exception:
        return 0


def _find_items_list(ssr_json: dict) -> List[dict]:
    """
    Items list location can vary. Try common paths, then a limited deep search.
    We want a list of dicts containing at least 'weight'/'quantity'.
    """
    if not isinstance(ssr_json, dict):
        return []

    paths = [
        ("data", "order_goods_list"),
        ("data", "orderGoodsList"),
        ("data", "goods_list"),
        ("data", "goodsList"),
        ("data", "order_detail", "order_goods_list"),
        ("data", "orderDetail", "orderGoodsList"),
        ("props", "pageProps", "data", "order_goods_list"),
        ("props", "pageProps", "data", "orderGoodsList"),
    ]

    def get_path(d, path):
        cur = d
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                return None
            cur = cur[k]
        return cur

    for path in paths:
        val = get_path(ssr_json, path)
        if isinstance(val, list) and val and isinstance(val[0], dict):
            if "weight" in val[0] or "quantity" in val[0]:
                return val

    def deep_find(obj, depth=0):
        if depth > 7:
            return None
        if isinstance(obj, dict):
            for v in obj.values():
                r = deep_find(v, depth + 1)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            if obj and isinstance(obj[0], dict) and ("weight" in obj[0] or "quantity" in obj[0]):
                return obj
            for v in obj:
                r = deep_find(v, depth + 1)
                if r is not None:
                    return r
        return None

    found = deep_find(ssr_json)
    return found if isinstance(found, list) else []


def compute_total_weight(ssr_json: dict) -> Dict[str, Any]:
    """
    Sum total item weight = Σ(weight * quantity)
    Weight appears to be grams.
    """
    items = _find_items_list(ssr_json)
    total_g = 0.0
    counted = 0

    for it in items:
        if not isinstance(it, dict):
            continue
        w = _to_float(it.get("weight"))
        q = _to_int(it.get("quantity") or 1)
        if w <= 0 or q <= 0:
            continue
        total_g += w * q
        counted += 1

    total_g_int = int(round(total_g))
    return {
        "total_weight_g": total_g_int,
        "total_weight_kg": round(total_g_int / 1000.0, 3),
        "items_counted": counted,
    }


# =========================
# Delivery detection
# =========================
def _is_delivered_from_last_event(last: dict) -> bool:
    status = (last.get("status") or "").strip()
    mall_status = (last.get("mall_status") or "").strip()
    code = str(last.get("mall_status_code") or "").strip()
    detail_status = str(last.get("detail_status") or "").strip()
    details = (last.get("details") or "").strip()

    dlow = details.lower()
    slow = status.lower()

    return (
        detail_status == "7"
        and (
            code == "6"
            or "签收" in status
            or "签收" in mall_status
            or "delivered" in slow
            or "delivered" in dlow
            or "تم التسليم" in details
            or "تم تسليم" in details
            or "يتم تسليم طلبك" in details
        )
    )


# =========================
# Login (uses Gmail code)
# =========================
def ensure_logged_in(page: Page, base_url: str, acc: dict, fetch_url: Optional[str] = None) -> None:
    """
    Logs in. If verification dialog appears, reads code from Gmail and submits it.
    Uses persistent profile, so typically runs once per profile.
    """
    page.goto(f"{base_url}/user/login", wait_until="domcontentloaded")
    page.wait_for_timeout(1000)

    # If already logged in, /user/login often redirects away.
    if "login" not in page.url.lower():
        return

    # Step 1: email
    email_input = page.locator("input#continue-alias-input").first
    try:
        email_input.wait_for(state="visible", timeout=30000)
    except TimeoutError:
        # Render/headless can re-render the login form while the locator is already present.
        # If it is actually visible now, continue. Otherwise try a broader fallback selector.
        try:
            if not email_input.is_visible():
                email_input = page.locator(
                    'input#continue-alias-input, input[aria-label*="البريد"], input[type="text"]'
                ).first
                email_input.wait_for(state="visible", timeout=15000)
        except Exception:
            page.screenshot(path="debug_email_input_timeout.png", full_page=True)
            raise
    email_input.click()
    email_input.press("Control+A")
    email_input.type(acc["shein_email"], delay=40)

    # Ensure full email is set (some pages strip domain on fast fill)
    try:
        current_val = email_input.input_value()
        if current_val.strip() != acc["shein_email"]:
            page.evaluate(
                "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }",
                email_input,
                acc["shein_email"],
            )
    except Exception:
        pass

    cont = page.locator("button.page__login_mainButton:has-text('متابعة')").first
    if cont.count() == 0:
        cont = page.locator("button:has-text('متابعة')").first
    cont.wait_for(state="visible", timeout=10000)
    cont.click()

    # Step 2: password
    password_input = page.locator('input[type="password"]').first
    password_input.wait_for(state="visible", timeout=15000)
    password_input.click()
    password_input.fill(acc["shein_password"])

    signin = page.locator("button.page__login_mainButton:has-text('تسجيل الدخول')").first
    if signin.count() == 0:
        signin = page.locator("button:has-text('تسجيل الدخول')").first
    signin.wait_for(state="visible", timeout=10000)
    signin.click()

    # Step 3: verification (if shown)
    try:
        code_input = page.locator("input.risk-dialog__Input").first
        code_input.wait_for(state="visible", timeout=12000)

        page.wait_for_timeout(4000)

        code = get_latest_shein_code(
            acc["gmail_email"],
            acc["gmail_app_password"],
            timeout_sec=180,
        )

        print("[DEBUG] Gmail code =", repr(code))

        if not code:
            page.screenshot(path="debug_no_code_found.png", full_page=True)
            raise RuntimeError("Verification code not found. Saved debug_no_code_found.png")

        code_input.click()
        code_input.press("Control+A")
        code_input.type(code, delay=60)

        submit_btn = page.locator("button.risk-dialog__subtn:has-text('تقديم')").first
        submit_btn.wait_for(state="visible", timeout=10000)
        submit_btn.click()

        page.wait_for_timeout(6000)

    except TimeoutError:
        pass

    # Step 4: optional post-login popup (Skip)
    try:
        skip_btn = page.locator('[aria-label="تخطي"]').first
        if skip_btn.count() == 0:
            skip_btn = page.locator("button:has-text('تخطي')").first
        skip_btn.wait_for(state="visible", timeout=5000)
        skip_btn.click()
        page.wait_for_timeout(1000)
    except TimeoutError:
        pass
    except Exception:
        pass

    # confirm login by visiting fetch URL immediately when provided
    target_url = fetch_url or f"{base_url}/user/orders/list"
    page.goto(target_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)


# =========================
# Tracking (SSR) — TRACK ONLY (NO WEIGHT)
# =========================
def fetch_one_order(page: Page, base_url: str, order_no: str) -> Dict[str, Any]:
    track_url = f"{base_url}/orders/track?billno={order_no}"
    page.goto(track_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)

    print("[DEBUG] Track page final URL:", page.url)

    html = page.content()
    ssr_text = _extract_ssr_block(html)
    if not ssr_text:
        print(f"[DEBUG] SSR var not found for {order_no} → returning nulls")
        return {
            "carrier": None,
            "tracking_no": None,
            "status_text": None,
            "last_details": None,
            "last_timestamp": None,
            "delivered": False,
            "track_url": track_url,
            "_used": "ssr_missing",
        }

    ssr_json = _json_parse_ssr(ssr_text)
    if ssr_json:
        pkg = _pull_pkg_from_json(ssr_json)
        if pkg:
            carrier = pkg.get("carrier_name")
            tracking_no = pkg.get("track_num")
            carrier_track_url = pkg.get("track_url") or track_url
            tracks = pkg.get("logistics_tracks_list") or []
            split_info = _collect_split_info(ssr_json, ssr_text, pkg)

            def _ts(e: dict) -> int:
                try:
                    return int(str(e.get("timestamp") or "0").strip())
                except Exception:
                    return 0

            last = max(tracks, key=_ts) if tracks else {}
            status_text = last.get("details")
            last_timestamp = last.get("timestamp")
            delivered = _is_delivered_from_last_event(last) if last else False

            return {
                "carrier": carrier,
                "tracking_no": tracking_no,
                "status_text": status_text,
                "last_details": status_text,
                "last_timestamp": last_timestamp,
                "delivered": delivered,
                "track_url": carrier_track_url,
                "is_split": split_info["is_split"],
                "split_count": split_info["split_count"],
                "all_tracking_numbers": split_info["all_tracking_numbers"],
                "all_package_refs": split_info["all_package_refs"],
                "_used": "ssr_json",
            }

        split_info = _collect_split_info(ssr_json, ssr_text, None)
        return {
            "carrier": None,
            "tracking_no": None,
            "status_text": None,
            "last_details": None,
            "last_timestamp": None,
            "delivered": False,
            "track_url": track_url,
            "is_split": split_info["is_split"],
            "split_count": split_info["split_count"],
            "all_tracking_numbers": split_info["all_tracking_numbers"],
            "all_package_refs": split_info["all_package_refs"],
            "_used": "ssr_json_no_pkg",
        }

    # regex fallback
    carrier = _regex_value(ssr_text, "carrier_name")
    tracking_no = _regex_value(ssr_text, "track_num")
    status_text = _regex_first_details(ssr_text)
    last_timestamp = _regex_first_timestamp(ssr_text)

    delivered = False
    if status_text and (
        "签收" in status_text
        or "DELIVERED" in status_text.upper()
        or "تم التسليم" in status_text
        or "تم تسليم" in status_text
        or "يتم تسليم طلبك" in status_text
    ):
        delivered = True

    if not (carrier or tracking_no or status_text):
        split_info = _collect_split_info(None, ssr_text, None)
        return {
            "carrier": None,
            "tracking_no": None,
            "status_text": None,
            "last_details": None,
            "last_timestamp": None,
            "delivered": False,
            "track_url": track_url,
            "is_split": split_info["is_split"],
            "split_count": split_info["split_count"],
            "all_tracking_numbers": split_info["all_tracking_numbers"],
            "all_package_refs": split_info["all_package_refs"],
            "_used": "ssr_regex_failed",
        }

    split_info = _collect_split_info(None, ssr_text, None)
    return {
        "carrier": carrier,
        "tracking_no": tracking_no,
        "status_text": status_text,
        "last_details": status_text,
        "last_timestamp": last_timestamp,
        "delivered": delivered,
        "track_url": track_url,
        "is_split": split_info["is_split"],
        "split_count": split_info["split_count"],
        "all_tracking_numbers": split_info["all_tracking_numbers"],
        "all_package_refs": split_info["all_package_refs"],
        "_used": "ssr_regex",
    }


# =========================
# Weight-only fetch (SSR) — WEIGHT ONLY
# =========================
def fetch_one_order_weight(page: Page, base_url: str, order_no: str) -> Dict[str, Any]:
    track_url = f"{base_url}/orders/track?billno={order_no}"
    page.goto(track_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)

    html = page.content()
    ssr_text = _extract_ssr_block(html)
    if not ssr_text:
        return {
            "order_no": order_no,
            "total_weight_g": None,
            "total_weight_kg": None,
            "items_counted": None,
            "is_split": False,
            "split_count": 0,
            "all_tracking_numbers": [],
            "all_package_refs": [],
            "_used": "ssr_missing",
        }

    ssr_json = _json_parse_ssr(ssr_text)
    if not ssr_json:
        split_info = _collect_split_info(None, ssr_text, None)
        return {
            "order_no": order_no,
            "total_weight_g": None,
            "total_weight_kg": None,
            "items_counted": None,
            "is_split": split_info["is_split"],
            "split_count": split_info["split_count"],
            "all_tracking_numbers": split_info["all_tracking_numbers"],
            "all_package_refs": split_info["all_package_refs"],
            "_used": "ssr_json_parse_failed",
        }

    weights = compute_total_weight(ssr_json)
    split_info = _collect_split_info(ssr_json, ssr_text, _pull_pkg_from_json(ssr_json))
    return {
        "order_no": order_no,
        "total_weight_g": weights["total_weight_g"],
        "total_weight_kg": weights["total_weight_kg"],
        "items_counted": weights["items_counted"],
        "is_split": split_info["is_split"],
        "split_count": split_info["split_count"],
        "all_tracking_numbers": split_info["all_tracking_numbers"],
        "all_package_refs": split_info["all_package_refs"],
        "_used": "ssr_weight",
    }


def _fetch_weight_sync(
    profile_key: str,
    shein_email: str,
    shein_password: str,
    gmail_email: str,
    gmail_app_password: str,
    order_no: str,
    base_url: str,
    headless: bool,
) -> Dict[str, Any]:
    os.makedirs(PROFILES_DIR, exist_ok=True)
    profile_path = os.path.join(PROFILES_DIR, profile_key)
    os.makedirs(profile_path, exist_ok=True)

    acc = {
        "shein_email": shein_email,
        "shein_password": shein_password,
        "gmail_email": gmail_email,
        "gmail_app_password": gmail_app_password,
    }
    target_track_url = f"{base_url}/orders/track?billno={order_no}"

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile_path,
            headless=headless,
            locale="ar",
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        try:
            ensure_logged_in(page, base_url, acc, fetch_url=target_track_url)
            return fetch_one_order_weight(page, base_url, order_no)
        finally:
            ctx.close()


async def fetch_weight_for_order(
    storage_state,
    shein_email,
    shein_password,
    gmail_email,
    gmail_app_password,
    order_no,
    profile_key="default",
    base_url=DEFAULT_BASE_URL,
    headless=False,
) -> Dict[str, Any]:
    return await anyio.to_thread.run_sync(
        _fetch_weight_sync,
        profile_key,
        shein_email,
        shein_password,
        gmail_email,
        gmail_app_password,
        order_no,
        base_url,
        headless,
    )


# =========================
# Runner (persistent profile) — TRACK ONLY
# =========================
def _fetch_tracking_sync(
    profile_key: str,
    shein_email: str,
    shein_password: str,
    gmail_email: str,
    gmail_app_password: str,
    order_no: str,
    base_url: str,
    headless: bool,
) -> Dict[str, Any]:
    os.makedirs(PROFILES_DIR, exist_ok=True)
    profile_path = os.path.join(PROFILES_DIR, profile_key)
    os.makedirs(profile_path, exist_ok=True)

    acc = {
        "shein_email": shein_email,
        "shein_password": shein_password,
        "gmail_email": gmail_email,
        "gmail_app_password": gmail_app_password,
    }
    target_track_url = f"{base_url}/orders/track?billno={order_no}"

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile_path,
            headless=headless,
            locale="ar",
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        try:
            ensure_logged_in(page, base_url, acc, fetch_url=target_track_url)
            info = fetch_one_order(page, base_url, order_no)

            # exact keys app.py expects (TRACK ONLY)
            return {
                "carrier": info.get("carrier"),
                "tracking_no": info.get("tracking_no"),
                "status_text": info.get("status_text"),
                "last_details": info.get("last_details"),
                "last_timestamp": info.get("last_timestamp"),
                "delivered": bool(info.get("delivered")),
                "track_url": info.get("track_url"),
                "is_split": bool(info.get("is_split")),
                "split_count": int(info.get("split_count") or 0),
                "all_tracking_numbers": info.get("all_tracking_numbers") or [],
                "all_package_refs": info.get("all_package_refs") or [],
                "_used": info.get("_used") or "sync_playwright_persistent_profile",
            }
        finally:
            ctx.close()


# =========================
# Async wrapper (FastAPI) — TRACK ONLY
# =========================
async def fetch_tracking_for_order(
    storage_state,
    shein_email,
    shein_password,
    gmail_email,
    gmail_app_password,
    order_no,
    profile_key="default",
    base_url=DEFAULT_BASE_URL,
    headless=False,
) -> Dict[str, Any]:
    return await anyio.to_thread.run_sync(
        _fetch_tracking_sync,
        profile_key,
        shein_email,
        shein_password,
        gmail_email,
        gmail_app_password,
        order_no,
        base_url,
        headless,
    )
