# app.py
import os
import traceback
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from db import SessionLocal, engine, Base
from models import User, Order
from crypto import encrypt_str, decrypt_str
from shein_scraper import fetch_tracking_for_order, fetch_weight_for_order

load_dotenv()

APP_SECRET = os.getenv("APP_SECRET")
if not APP_SECRET:
    raise RuntimeError("APP_SECRET missing in .env")

# Run Playwright headless by default (server-safe). Set PLAYWRIGHT_HEADLESS=0 to debug locally with visible browser.
PLAYWRIGHT_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "1").strip().lower() in ("1", "true", "yes")

app = FastAPI(title="SHEIN Tracker API")

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

DB_AVAILABLE = True
try:
    Base.metadata.create_all(bind=engine)
except Exception as e:
    DB_AVAILABLE = False
    print(f"[WARN] DB unavailable at startup: {type(e).__name__}: {e}")


# =========================
# Request models
# =========================
class RegisterReq(BaseModel):
    email: str
    owner_user_id: int | None = None
    gmail_email: str
    gmail_app_password: str
    shein_email: str
    shein_password: str


class AddOrderReq(BaseModel):
    email: str
    owner_user_id: int | None = None
    order_no: str


class EmailReq(BaseModel):
    email: str


class TrackOneReq(BaseModel):
    email: str
    order_no: str


class WeightOneReq(BaseModel):
    email: str
    order_no: str


class WeightBatchReq(BaseModel):
    email: str
    # optional: only compute weights for not-delivered orders
    only_pending: bool = False


class UserListItem(BaseModel):
    email: str


class UserDetailReq(BaseModel):
    email: str
    owner_user_id: int | None = None


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


# =========================
# Helpers
# =========================
def _load_user_and_creds(db, email: str):
    u = db.query(User).filter(User.email == email).first()
    if not u:
        raise HTTPException(404, "User not found. Register first.")

    if not u.shein_email or not u.shein_password_enc:
        raise HTTPException(400, "Missing SHEIN credentials. Register again.")

    if not u.gmail_email or not u.gmail_app_password_enc:
        raise HTTPException(400, "Missing Gmail credentials. Register again.")

    def _normalize_email(val: str) -> str:
        v = (val or "").strip().lower()
        if "@" in v:
            local, domain = v.split("@", 1)
            if local and domain and "." not in domain:
                v = f"{local}@{domain}.com"
        return v

    try:
        shein_email = _normalize_email(u.shein_email)
        shein_password = decrypt_str(APP_SECRET, u.shein_password_enc)
        gmail_email = _normalize_email(u.gmail_email)
        gmail_app_password = decrypt_str(APP_SECRET, u.gmail_app_password_enc)
    except Exception as e:
        raise HTTPException(400, f"Failed to decrypt credentials: {type(e).__name__}")

    profile_key = f"user_{u.id}"  # one persistent profile per user
    return u, profile_key, shein_email, shein_password, gmail_email, gmail_app_password


def _require_order_belongs_to_user(db, user_id: int, order_no: str) -> Order:
    o = db.query(Order).filter(Order.user_id == user_id, Order.order_no == order_no).first()
    if not o:
        raise HTTPException(404, "Order not found for this user. Add it first.")
    return o


# =========================
# Auth + orders
# =========================
@app.post("/api/register")
def register_user(req: RegisterReq):
    db = SessionLocal()
    try:
        def _normalize_email(val: str) -> str:
            v = (val or "").strip().lower()
            if "@" in v:
                local, domain = v.split("@", 1)
                if local and domain and "." not in domain:
                    v = f"{local}@{domain}.com"
            return v

        req.email = (req.email or "").strip().lower()
        req.gmail_email = _normalize_email(req.gmail_email)
        req.shein_email = _normalize_email(req.shein_email)
        req.gmail_app_password = (req.gmail_app_password or "").replace(" ", "")

        q = db.query(User).filter(User.email == req.email)
        if req.owner_user_id is not None:
            q = q.filter(User.owner_user_id == req.owner_user_id)
        u = q.first()
        if u:
            u.owner_user_id = req.owner_user_id
            u.gmail_email = req.gmail_email
            u.gmail_app_password_enc = encrypt_str(APP_SECRET, req.gmail_app_password)
            u.shein_email = req.shein_email
            u.shein_password_enc = encrypt_str(APP_SECRET, req.shein_password)
            db.commit()
            return {"ok": True, "message": "Updated credentials."}

        u = User(
            owner_user_id=req.owner_user_id,
            email=req.email,
            gmail_email=req.gmail_email,
            gmail_app_password_enc=encrypt_str(APP_SECRET, req.gmail_app_password),
            shein_email=req.shein_email,
            shein_password_enc=encrypt_str(APP_SECRET, req.shein_password),
        )
        db.add(u)
        db.commit()
        return {"ok": True, "message": "Registered."}
    finally:
        db.close()


@app.post("/api/orders")
def add_order(req: AddOrderReq):
    db = SessionLocal()
    try:
        q = db.query(User).filter(User.email == req.email)
        if req.owner_user_id is not None:
            q = q.filter(User.owner_user_id == req.owner_user_id)
        u = q.first()
        if not u:
            raise HTTPException(404, "User not found. Register first.")

        existing = db.query(Order).filter(Order.user_id == u.id, Order.order_no == req.order_no).first()
        if existing:
            return {"ok": True, "message": "Order already exists."}

        o = Order(user_id=u.id, order_no=req.order_no)
        db.add(o)
        db.commit()
        return {"ok": True, "message": "Order added."}
    finally:
        db.close()


@app.get("/api/orders")
def list_orders(email: str, owner_user_id: int | None = None):
    db = SessionLocal()
    try:
        q = db.query(User).filter(User.email == email)
        if owner_user_id is not None:
            q = q.filter(User.owner_user_id == owner_user_id)
        u = q.first()
        if not u:
            raise HTTPException(404, "User not found.")

        orders = db.query(Order).filter(Order.user_id == u.id).all()
        return {
            "ok": True,
            "orders": [
                {
                    "order_no": o.order_no,
                    "carrier": o.carrier,
                    "tracking_no": o.tracking_no,
                    "status_text": o.status_text,
                    "delivered": bool(o.delivered),
                    "last_details": o.last_details,
                    "last_timestamp": o.last_timestamp,
                }
                for o in orders
            ],
        }
    finally:
        db.close()


@app.get("/api/users")
def list_users(email: str | None = None, owner_user_id: int | None = None):
    db = SessionLocal()
    try:
        q = db.query(User)
        if email:
            q = q.filter(User.email == email)
        if owner_user_id is not None:
            q = q.filter(User.owner_user_id == owner_user_id)
        users = q.all()
        return {
            "ok": True,
            "users": [{"email": u.email} for u in users],
        }
    finally:
        db.close()


@app.delete("/api/users")
def delete_user(email: str, owner_user_id: int | None = None):
    db = SessionLocal()
    try:
        q = db.query(User).filter(User.email == email)
        if owner_user_id is not None:
            q = q.filter(User.owner_user_id == owner_user_id)
        u = q.first()
        if not u:
            raise HTTPException(404, "User not found.")
        db.delete(u)
        db.commit()
        return {"ok": True, "message": "User deleted."}
    finally:
        db.close()


@app.get("/api/users/detail")
def user_detail(email: str, owner_user_id: int | None = None):
    db = SessionLocal()
    try:
        q = db.query(User).filter(User.email == email)
        if owner_user_id is not None:
            q = q.filter(User.owner_user_id == owner_user_id)
        u = q.first()
        if not u:
            raise HTTPException(404, "User not found.")

        return {
            "ok": True,
            "user": {
                "email": u.email,
                "owner_user_id": u.owner_user_id,
                "gmail_email": u.gmail_email,
                "gmail_app_password": decrypt_str(APP_SECRET, u.gmail_app_password_enc),
                "shein_email": u.shein_email,
                "shein_password": decrypt_str(APP_SECRET, u.shein_password_enc),
            },
        }
    finally:
        db.close()


# ============================================================
# TRACKING SCRAPE API (SEPARATE)
# ============================================================

@app.post("/api/track/one")
async def scrape_track_one(req: TrackOneReq):
    """
    Scrape tracking for ONE order and return it (doesn't modify DB).
    """
    db = SessionLocal()
    try:
        u, profile_key, shein_email, shein_password, gmail_email, gmail_app_password = (
            _load_user_and_creds(db, req.email)
        )

        _require_order_belongs_to_user(db, u.id, req.order_no)

        result = await fetch_tracking_for_order(
            storage_state=None,
            shein_email=shein_email,
            shein_password=shein_password,
            gmail_email=gmail_email,
            gmail_app_password=gmail_app_password,
            order_no=req.order_no,
            profile_key=profile_key,
            headless=PLAYWRIGHT_HEADLESS,
        )

        # TRACK-ONLY response
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
    except HTTPException:
        raise
    except Exception as e:
        print(
            f"[ERROR] /api/track/one email={req.email} order_no={req.order_no}: "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    finally:
        db.close()


@app.post("/api/track/refresh")
async def refresh_not_delivered(req: EmailReq):
    """
    Refresh tracking for ALL not delivered orders, and SAVE results into DB.
    """
    print("Starting tracking refresh for:", req.email)

    db = SessionLocal()
    try:
        u, profile_key, shein_email, shein_password, gmail_email, gmail_app_password = (
            _load_user_and_creds(db, req.email)
        )

        pending = db.query(Order).filter(Order.user_id == u.id, Order.delivered == False).all()

        updated = []
        for o in pending:
            try:
                print("Refreshing order:", o.order_no)

                result = await fetch_tracking_for_order(
                    storage_state=None,
                    shein_email=shein_email,
                    shein_password=shein_password,
                    gmail_email=gmail_email,
                    gmail_app_password=gmail_app_password,
                    order_no=o.order_no,
                    profile_key=profile_key,
                    headless=PLAYWRIGHT_HEADLESS,
                )

                # save TRACKING ONLY
                o.carrier = result.get("carrier")
                o.tracking_no = result.get("tracking_no")
                o.status_text = result.get("status_text")
                o.last_details = result.get("last_details")
                o.last_timestamp = result.get("last_timestamp")
                o.delivered = bool(result.get("delivered"))

                db.flush()
                db.commit()
                db.refresh(o)

                updated.append(
                    {
                        "order_no": o.order_no,
                        "carrier": o.carrier,
                        "tracking_no": o.tracking_no,
                        "status_text": o.status_text,
                        "delivered": bool(o.delivered),
                        "track_url": result.get("track_url"),
                        "is_split": bool(result.get("is_split")),
                        "split_count": int(result.get("split_count") or 0),
                        "all_tracking_numbers": result.get("all_tracking_numbers") or [],
                        "all_package_refs": result.get("all_package_refs") or [],
                        "_used": result.get("_used"),
                    }
                )

            except Exception as e:
                db.rollback()
                updated.append(
                    {
                        "order_no": o.order_no,
                        "error": f"{type(e).__name__}: {str(e)}",
                        "_used": "exception",
                    }
                )

        return {"ok": True, "updated": updated, "count": len(updated)}
    finally:
        db.close()


# ============================================================
# WEIGHT SCRAPE API (SEPARATE)
# ============================================================

@app.post("/api/weight/one")
async def scrape_weight_one(req: WeightOneReq):
    """
    Scrape WEIGHT for ONE order and return it (doesn't modify DB).
    """
    db = SessionLocal()
    try:
        u, profile_key, shein_email, shein_password, gmail_email, gmail_app_password = (
            _load_user_and_creds(db, req.email)
        )

        _require_order_belongs_to_user(db, u.id, req.order_no)

        result = await fetch_weight_for_order(
            storage_state=None,
            shein_email=shein_email,
            shein_password=shein_password,
            gmail_email=gmail_email,
            gmail_app_password=gmail_app_password,
            order_no=req.order_no,
            profile_key=profile_key,
            headless=PLAYWRIGHT_HEADLESS,
        )

        # WEIGHT-ONLY response
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
    except HTTPException:
        raise
    except Exception as e:
        print(
            f"[ERROR] /api/weight/one email={req.email} order_no={req.order_no}: "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    finally:
        db.close()


@app.post("/api/weight/batch")
async def scrape_weight_batch(req: WeightBatchReq):
    """
    Scrape weights for MANY orders (returns list).
    By default: all orders for user.
    If only_pending=True: only orders where delivered=False.
    """
    db = SessionLocal()
    try:
        u, profile_key, shein_email, shein_password, gmail_email, gmail_app_password = (
            _load_user_and_creds(db, req.email)
        )

        q = db.query(Order).filter(Order.user_id == u.id)
        if req.only_pending:
            q = q.filter(Order.delivered == False)

        orders = q.all()
        results = []

        for o in orders:
            try:
                r = await fetch_weight_for_order(
                    storage_state=None,
                    shein_email=shein_email,
                    shein_password=shein_password,
                    gmail_email=gmail_email,
                    gmail_app_password=gmail_app_password,
                    order_no=o.order_no,
                    profile_key=profile_key,
                    headless=PLAYWRIGHT_HEADLESS,
                )
                results.append(
                    {
                        "order_no": o.order_no,
                        "total_weight_g": r.get("total_weight_g"),
                        "total_weight_kg": r.get("total_weight_kg"),
                        "items_counted": r.get("items_counted"),
                        "is_split": bool(r.get("is_split")),
                        "split_count": int(r.get("split_count") or 0),
                        "all_tracking_numbers": r.get("all_tracking_numbers") or [],
                        "all_package_refs": r.get("all_package_refs") or [],
                        "_used": r.get("_used"),
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "order_no": o.order_no,
                        "error": f"{type(e).__name__}: {str(e)}",
                        "_used": "exception",
                    }
                )

        return {"ok": True, "results": results, "count": len(results)}
    finally:
        db.close()


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

        return {"ok": True, "count": len(results), "results": results, "storage_state_json": storage_state}
    except HTTPException:
        raise
    except Exception as e:
        print(
            f"[ERROR] /api/direct/weight_many count={len(req.order_nos or [])}: "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        raise HTTPException(500, f"{type(e).__name__}: {e}")
