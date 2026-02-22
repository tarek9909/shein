import base64
import hashlib
from cryptography.fernet import Fernet

def _fernet_from_secret(app_secret: str) -> Fernet:
    # Turn secret into a valid Fernet key (32 urlsafe base64 bytes)
    digest = hashlib.sha256(app_secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)

def encrypt_str(app_secret: str, plaintext: str) -> str:
    f = _fernet_from_secret(app_secret)
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")

def decrypt_str(app_secret: str, ciphertext: str) -> str:
    f = _fernet_from_secret(app_secret)
    return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
