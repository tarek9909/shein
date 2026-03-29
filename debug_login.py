import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from shein_scraper import (
    DEFAULT_BASE_URL,
    PROFILES_DIR,
    _attach_page_debug,
    _build_context,
    _goto_preferred,
    _install_host_guard,
    ensure_logged_in,
    fetch_one_order,
)


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _mask(value: str, keep: int = 3) -> str:
    value = str(value or "")
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + ("*" * max(len(value) - keep, 0))


def _collect_page_summary(page) -> Dict[str, Any]:
    return page.evaluate(
        """() => {
            const toItems = (nodes, mapper) => Array.from(nodes || []).slice(0, 40).map(mapper);
            const visible = el => {
              if (!el) return false;
              const style = window.getComputedStyle(el);
              const rect = el.getBoundingClientRect();
              return style && style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
            };

            return {
              title: document.title,
              url: location.href,
              visible_buttons: toItems(document.querySelectorAll("button, a, [role='button']"), el => ({
                tag: el.tagName,
                text: (el.innerText || el.textContent || "").trim().slice(0, 120),
                aria_label: el.getAttribute("aria-label"),
                class_name: el.className || "",
                id: el.id || "",
                visible: visible(el),
              })).filter(item => item.visible),
              visible_inputs: toItems(document.querySelectorAll("input, textarea"), el => ({
                tag: el.tagName,
                type: el.getAttribute("type") || "",
                name: el.getAttribute("name") || "",
                id: el.id || "",
                placeholder: el.getAttribute("placeholder") || "",
                autocomplete: el.getAttribute("autocomplete") || "",
                aria_label: el.getAttribute("aria-label") || "",
                value_preview: ((el.value || "").slice(0, 3) + (((el.value || "").length > 3) ? "..." : "")),
                visible: visible(el),
              })).filter(item => item.visible),
              dialogs: toItems(document.querySelectorAll("[role='dialog'], .sui-dialog, .risk-dialog"), el => ({
                text: (el.innerText || el.textContent || "").trim().slice(0, 500),
                class_name: el.className || "",
                visible: visible(el),
              })).filter(item => item.visible),
            };
        }"""
    )


def _write_debug_bundle(page, bundle_dir: Path, label: str, extra: Dict[str, Any] | None = None) -> None:
    bundle_dir.mkdir(parents=True, exist_ok=True)

    png_path = bundle_dir / f"{label}.png"
    html_path = bundle_dir / f"{label}.html"
    json_path = bundle_dir / f"{label}.json"

    page.screenshot(path=str(png_path), full_page=True)
    html_path.write_text(page.content(), encoding="utf-8")

    payload = {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "page": _collect_page_summary(page),
    }
    if extra:
        payload["extra"] = extra

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[DEBUG LOGIN] wrote screenshot: {png_path}")
    print(f"[DEBUG LOGIN] wrote html: {html_path}")
    print(f"[DEBUG LOGIN] wrote summary: {json_path}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open a visible Playwright browser and debug SHEIN login.")
    parser.add_argument("--shein-email", default=os.getenv("SHEIN_EMAIL", ""))
    parser.add_argument("--shein-password", default=os.getenv("SHEIN_PASSWORD", ""))
    parser.add_argument("--gmail-email", default=os.getenv("GMAIL_EMAIL", ""))
    parser.add_argument("--gmail-app-password", default=os.getenv("GMAIL_APP_PASSWORD", ""))
    parser.add_argument("--profile-key", default=os.getenv("DEBUG_PROFILE_KEY", "debug-login"))
    parser.add_argument("--base-url", default=(os.getenv("SHEIN_BASE_URL") or DEFAULT_BASE_URL))
    parser.add_argument("--order-no", default=os.getenv("DEBUG_ORDER_NO", ""))
    parser.add_argument("--headless", action="store_true", help="Run headless instead of opening the browser window.")
    parser.add_argument("--keep-open", action="store_true", help="Keep the browser open after the run until Enter is pressed.")
    parser.add_argument("--pause-before-login", action="store_true", help="Pause on the login page before submitting credentials.")
    parser.add_argument("--pause-on-error", action="store_true", help="Keep the page open after an error before closing.")
    parser.add_argument("--artifacts-dir", default="debug/login_runs")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = _parse_args()

    missing = [
        name
        for name, value in (
            ("shein_email", args.shein_email),
            ("shein_password", args.shein_password),
            ("gmail_email", args.gmail_email),
            ("gmail_app_password", args.gmail_app_password),
        )
        if not str(value or "").strip()
    ]
    if missing:
        print(f"[DEBUG LOGIN] missing required values: {', '.join(missing)}")
        return 2

    profile_path = os.path.join(PROFILES_DIR, args.profile_key)
    os.makedirs(PROFILES_DIR, exist_ok=True)
    os.makedirs(profile_path, exist_ok=True)

    acc = {
        "shein_email": args.shein_email.strip(),
        "shein_password": args.shein_password,
        "gmail_email": args.gmail_email.strip(),
        "gmail_app_password": args.gmail_app_password.replace(" ", ""),
    }

    target_url = (
        f"{args.base_url.rstrip('/')}/orders/track?billno={args.order_no.strip()}"
        if args.order_no.strip()
        else f"{args.base_url.rstrip('/')}/user/orders/list"
    )
    artifacts_dir = Path(args.artifacts_dir) / _timestamp()

    print("[DEBUG LOGIN] starting")
    print(f"[DEBUG LOGIN] base_url={args.base_url}")
    print(f"[DEBUG LOGIN] target_url={target_url}")
    print(f"[DEBUG LOGIN] profile_key={args.profile_key}")
    print(f"[DEBUG LOGIN] headless={args.headless}")
    print(f"[DEBUG LOGIN] shein_email={_mask(acc['shein_email'])}")
    print(f"[DEBUG LOGIN] gmail_email={_mask(acc['gmail_email'])}")

    with sync_playwright() as p:
        ctx = _build_context(p, profile_path, args.headless)
        _install_host_guard(ctx, args.base_url)
        page = ctx.new_page()
        _attach_page_debug(page)

        try:
            _goto_preferred(
                page,
                f"{args.base_url.rstrip('/')}/user/auth/login?direction=nav",
                args.base_url,
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(1500)
            _write_debug_bundle(page, artifacts_dir, "before_login", {"phase": "before_login"})

            if args.pause_before_login:
                input("[DEBUG LOGIN] Login page loaded. Press Enter to continue...")

            ensure_logged_in(page, args.base_url.rstrip("/"), acc, fetch_url=target_url)

            extra: Dict[str, Any] = {
                "phase": "after_login",
                "final_url": page.url,
            }
            if args.order_no.strip():
                try:
                    extra["tracking"] = fetch_one_order(page, args.base_url.rstrip("/"), args.order_no.strip())
                except Exception as tracking_exc:
                    extra["tracking_error"] = f"{type(tracking_exc).__name__}: {tracking_exc}"

            _write_debug_bundle(page, artifacts_dir, "after_login", extra)
            print(f"[DEBUG LOGIN] login flow completed. final_url={page.url}")

            if args.keep_open:
                input("[DEBUG LOGIN] Browser is still open. Press Enter to close...")
            return 0
        except Exception as exc:
            print(f"[DEBUG LOGIN] error: {type(exc).__name__}: {exc}")
            try:
                _write_debug_bundle(
                    page,
                    artifacts_dir,
                    "error_state",
                    {
                        "phase": "error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "final_url": page.url,
                    },
                )
            except Exception as dump_exc:
                print(f"[DEBUG LOGIN] failed to write error artifacts: {type(dump_exc).__name__}: {dump_exc}")

            if args.pause_on_error or args.keep_open:
                input("[DEBUG LOGIN] Error captured. Press Enter to close the browser...")
            raise
        finally:
            ctx.close()


if __name__ == "__main__":
    raise SystemExit(main())
