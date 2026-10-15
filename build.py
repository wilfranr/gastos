#!/usr/bin/env python3
"""Rebuild data.json (and optionally data.enc) from gastos.csv.
Usage:
  python3 build.py                 # writes data.json from ../dashboard/gastos.csv or ./gastos.csv
  DASH_PASSWORD=secreto python3 build.py   # also writes data.enc (AES-GCM)
"""
import csv, json, os, struct, base64, hashlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "gastos.csv"
if not SRC.exists():
    SRC = HERE.parent / "dashboard" / "gastos.csv"

rows = []
with open(SRC, newline="", encoding="utf-8") as f:
    for r in csv.DictReader(f):
        r["monto"] = float(r["monto"]) if r["monto"] else ""
        r["mes"] = int(r["mes"])
        rows.append(r)

payload = json.dumps(rows, ensure_ascii=False).encode("utf-8")
(HERE / "data.json").write_bytes(payload)
print(f"Wrote data.json ({len(rows)} rows)")

pw = os.environ.get("DASH_PASSWORD")
if not pw:
    print("DASH_PASSWORD not set — skipping data.enc")
else:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "cryptography"])
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes

    salt = os.urandom(16)
    iv = os.urandom(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=310000)
    key = kdf.derive(pw.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, payload, None)
    # format: base64(salt|iv|ciphertext+tag) as JSON envelope
    blob = {
        "v": 1,
        "kdf": "PBKDF2-SHA256",
        "iter": 310000,
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(ct).decode(),
    }
    (HERE / "data.enc").write_text(json.dumps(blob), encoding="utf-8")
    print("Wrote data.enc (AES-GCM, PBKDF2 310k)")
