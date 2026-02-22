# app.py (direct-only minimal version, no DB dependency)

import os
import traceback
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from shein_scraper import fetch_tracking_for_order, fetch_weight_for_order

load_dotenv()

APP_SECRET = os.getenv("APP_SECRET")
if not APP_SECRET:
    raise RuntimeError("APP_SECRET missing in .env")

# Headless by default (server-safe). Set PLAYWRIGHT_HEADLESS=0 locally to debug with visible browser.
PLAYWRIGHT_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "1").strip().lower() in ("1", "true", "yes")

app = FastAPI(title="SHEIN Tracker API (Direct Only)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/ping")
def ping():
    return {"ok": True, "msg": "pong"}


# =========================
# Request models (direct endpoints only)
# =========================
class DirectScrapeReq(BaseModel):
    order_no: str
    shein_email: str
    shein_password: str
    gmail_email: str
    gmail_app_password: str
    profile_key: str = "default"
    storage_state_json: str | None = None


class DirectScrapeBatchReq(BaseModel):
    order_nos: list[str]
    shein_email: str
    shein_password: str
    gmail_email: str
    gmail_app_password: str
    profile_key: str = "default"
    storage_state_json: str | None = None


# ============================================================
# DIRECT (STATELESS) SCRAPE API (NO DB LOOKUPS)
# ============================================================

@app.post("/api/direct/track_one")
async def direct_track_one(req: DirectScrapeReq):
    try:
        result = await fetch_tracking_for_order(
            storage_state=req.storage_state_json,
            shein_email=req.shein_email.strip(),
            shein_password=req.shein_password,
            gmail_email=req.gmail_email.strip(),
            gmail_app_password=req.gmail_app_password.replace(" ", ""),
            order_no=req.order_no.strip(),
            profile_key=(req.profile_key or "default").strip(),
            headless=PLAYWRIGHT_HEADLESS,
        )
        return {
            "ok": True,
            "order_no": req.order_no,
            "carrier": result.get("carrier"),
            "tracking_no": result.get("tracking_no"),
            "status_text": result.get("status_text"),
            "last_details": result.get("last_details"),
            "last_timestamp": result.get("last_timestamp"),
            "delivered": bool(result.get("delivered")),
            "track_url": result.get("track_url"),
            "is_split": bool(result.get("is_split")),
            "split_count": int(result.get("split_count") or 0),
            "all_tracking_numbers": result.get("all_tracking_numbers") or [],
            "all_package_refs": result.get("all_package_refs") or [],
            "_used": result.get("_used"),
        }
    except Exception as e:
        print(
            f"[ERROR] /api/direct/track_one order_no={req.order_no}: "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.post("/api/direct/weight_one")
async def direct_weight_one(req: DirectScrapeReq):
    try:
        result = await fetch_weight_for_order(
            storage_state=req.storage_state_json,
            shein_email=req.shein_email.strip(),
            shein_password=req.shein_password,
            gmail_email=req.gmail_email.strip(),
            gmail_app_password=req.gmail_app_password.replace(" ", ""),
            order_no=req.order_no.strip(),
            profile_key=(req.profile_key or "default").strip(),
            headless=PLAYWRIGHT_HEADLESS,
        )
        return {
            "ok": True,
            "order_no": req.order_no,
            "total_weight_g": result.get("total_weight_g"),
            "total_weight_kg": result.get("total_weight_kg"),
            "items_counted": result.get("items_counted"),
            "is_split": bool(result.get("is_split")),
            "split_count": int(result.get("split_count") or 0),
            "all_tracking_numbers": result.get("all_tracking_numbers") or [],
            "all_package_refs": result.get("all_package_refs") or [],
            "_used": result.get("_used"),
        }
    except Exception as e:
        print(
            f"[ERROR] /api/direct/weight_one order_no={req.order_no}: "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.post("/api/direct/weight_many")
async def direct_weight_many(req: DirectScrapeBatchReq):
    try:
        order_nos = []
        seen = set()
        for raw in req.order_nos or []:
            ono = str(raw or "").strip()
            if not ono or ono in seen:
                continue
            seen.add(ono)
            order_nos.append(ono)

        if not order_nos:
            raise HTTPException(400, "order_nos is required")

        storage_state = req.storage_state_json
        results = []

        for order_no in order_nos:
            try:
                result = await fetch_weight_for_order(
                    storage_state=storage_state,
                    shein_email=req.shein_email.strip(),
                    shein_password=req.shein_password,
                    gmail_email=req.gmail_email.strip(),
                    gmail_app_password=req.gmail_app_password.replace(" ", ""),
                    order_no=order_no,
                    profile_key=(req.profile_key or "default").strip(),
                    headless=PLAYWRIGHT_HEADLESS,
                )
                storage_state = result.get("_storage_state") or storage_state

                results.append(
                    {
                        "ok": True,
                        "order_no": order_no,
                        "total_weight_g": result.get("total_weight_g"),
                        "total_weight_kg": result.get("total_weight_kg"),
                        "items_counted": result.get("items_counted"),
                        "is_split": bool(result.get("is_split")),
                        "split_count": int(result.get("split_count") or 0),
                        "all_tracking_numbers": result.get("all_tracking_numbers") or [],
                        "all_package_refs": result.get("all_package_refs") or [],
                        "_used": result.get("_used"),
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "ok": False,
                        "order_no": order_no,
                        "error": f"{type(e).__name__}: {e}",
                    }
                )

        return {
            "ok": True,
            "count": len(results),
            "results": results,
            "storage_state_json": storage_state,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(
            f"[ERROR] /api/direct/weight_many count={len(req.order_nos or [])}: "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        raise HTTPException(500, f"{type(e).__name__}: {e}")
