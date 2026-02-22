# gmail.py
import re
import time
import imaplib
import email
from email.header import decode_header

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

def get_latest_shein_code(gmail_email: str, gmail_app_password: str, timeout_sec: int = 180) -> str | None:
    """
    Polls Gmail inbox for latest SHEIN verification email and returns 5-6 digit code.
    Requires Gmail App Password.
    """
    start = time.time()

    while time.time() - start < timeout_sec:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        try:
            mail.login(gmail_email, gmail_app_password)
        except imaplib.IMAP4.error as e:
            mail.logout()
            raise RuntimeError(
                "Gmail IMAP login failed. Check Gmail address/app password and make sure IMAP is enabled."
            ) from e
        mail.select("INBOX")

        # Prefer unseen; fallback to all
        status, messages = mail.search(None, "UNSEEN")
        if status != "OK" or not messages[0]:
            status, messages = mail.search(None, "ALL")

        if status == "OK" and messages and messages[0]:
            ids = messages[0].split()

            # newest first
            for msg_id in reversed(ids[-60:]):
                status, data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(data[0][1])
                from_hdr = _decode(msg.get("From", "")).lower()
                subj = _decode(msg.get("Subject", "")).lower()

                if not any(h in from_hdr for h in SHEIN_FROM_HINTS) and "shein" not in subj:
                    continue

                body = _extract_text(msg)
                code = _pick_best_code(body)
                if code:
                    mail.logout()
                    return code

        mail.logout()
        time.sleep(5)

    return None
