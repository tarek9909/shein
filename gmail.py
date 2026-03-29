# gmail.py
import re
import time
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime

SHEIN_FROM_HINTS = ("shein", "sheinnotice.com", "noreply@sheinnotice.com")

KEYWORDS = [
    "code", "verify", "verification", "enter the following",
    "رمز", "التحقق", "رمز التحقق", "للأمان"
]

def _decode(s: str) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    out = ""
    for p, enc in parts:
        if isinstance(p, bytes):
            out += p.decode(enc or "utf-8", errors="ignore")
        else:
            out += p
    return out

def _extract_text(msg) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True) or b""
                body += payload.decode(errors="ignore") + "\n"
    else:
        payload = msg.get_payload(decode=True) or b""
        body = payload.decode(errors="ignore")
    return body


def _message_timestamp(msg) -> float | None:
    raw_date = msg.get("Date")
    if not raw_date:
        return None
    try:
        dt = parsedate_to_datetime(raw_date)
        if dt is None:
            return None
        return dt.timestamp()
    except Exception:
        return None


def _mask_emails(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"([A-Za-z0-9._%+-]{1,3})[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", r"\1***\2", text)

def _is_junk_code(code: str) -> bool:
    # reject 00000, 111111 etc.
    return len(set(code)) == 1

def _pick_best_code(body: str) -> str | None:
    matches = list(re.finditer(r"\b(\d{5,6})\b", body))
    if not matches:
        return None

    lower = body.lower()
    best = None
    best_score = -1

    for m in matches:
        code = m.group(1)
        if _is_junk_code(code):
            continue

        start = max(0, m.start() - 100)
        end = min(len(lower), m.end() + 100)
        window = lower[start:end]

        score = 0
        for kw in KEYWORDS:
            if kw.lower() in window:
                score += 10

        # slight preference for 6-digit if tied
        score += (1 if len(code) == 6 else 0)

        if score > best_score:
            best_score = score
            best = code

    if best:
        return best

    # fallback: newest-like in text
    for m in reversed(matches):
        code = m.group(1)
        if not _is_junk_code(code):
            return code

    return None

def get_latest_shein_code_details(
    gmail_email: str,
    gmail_app_password: str,
    timeout_sec: int = 180,
    received_after_ts: float | None = None,
) -> dict | None:
    """
    Polls Gmail inbox for latest SHEIN verification email and returns code metadata.
    Requires Gmail App Password.
    """
    start = time.time()
    min_ts = (received_after_ts - 30.0) if received_after_ts else None
    poll_delay_sec = 5

    def _safe_logout(mail) -> None:
        try:
            mail.logout()
        except Exception:
            pass

    while time.time() - start < timeout_sec:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
        try:
            mail.login(gmail_email, gmail_app_password)
        except imaplib.IMAP4.error as e:
            _safe_logout(mail)
            raise RuntimeError(
                "Gmail IMAP login failed. Check Gmail address/app password and make sure IMAP is enabled."
            ) from e
        try:
            mail.select("INBOX")

            # Prefer unseen; fallback to all
            status, messages = mail.search(None, "UNSEEN")
            if status != "OK" or not messages[0]:
                status, messages = mail.search(None, "ALL")

            if status == "OK" and messages and messages[0]:
                ids = messages[0].split()

                # newest first
                for msg_id in reversed(ids[-60:]):
                    try:
                        status, data = mail.fetch(msg_id, "(RFC822)")
                    except imaplib.IMAP4.abort as e:
                        print(f"[GMAIL DEBUG] fetch abort for msg_id={msg_id!r}: {e}")
                        raise
                    except imaplib.IMAP4.error as e:
                        print(f"[GMAIL DEBUG] fetch error for msg_id={msg_id!r}: {e}")
                        continue

                    if status != "OK" or not data or not isinstance(data[0], tuple):
                        continue

                    msg = email.message_from_bytes(data[0][1])
                    from_hdr = _decode(msg.get("From", "")).lower()
                    subj = _decode(msg.get("Subject", "")).lower()

                    if not any(h in from_hdr for h in SHEIN_FROM_HINTS) and "shein" not in subj:
                        continue

                    msg_ts = _message_timestamp(msg)
                    if min_ts is not None and msg_ts is not None and msg_ts < min_ts:
                        continue

                    body = _extract_text(msg)
                    code = _pick_best_code(body)
                    if code:
                        _safe_logout(mail)
                        return {
                            "code": code,
                            "message_ts": msg_ts,
                            "subject": _mask_emails(_decode(msg.get("Subject", ""))),
                            "from": _mask_emails(_decode(msg.get("From", ""))),
                        }
        except imaplib.IMAP4.abort as e:
            print(f"[GMAIL DEBUG] reconnecting after IMAP abort: {e}")
        except TimeoutError as e:
            print(f"[GMAIL DEBUG] reconnecting after socket timeout: {e}")
        finally:
            _safe_logout(mail)

        time.sleep(poll_delay_sec)

    return None


def get_latest_shein_code(
    gmail_email: str,
    gmail_app_password: str,
    timeout_sec: int = 180,
    received_after_ts: float | None = None,
) -> str | None:
    details = get_latest_shein_code_details(
        gmail_email,
        gmail_app_password,
        timeout_sec=timeout_sec,
        received_after_ts=received_after_ts,
    )
    if not details:
        return None
    return details.get("code")
