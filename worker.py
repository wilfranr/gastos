#!/usr/bin/env python3
"""
Atiende la cola de capturas de la web: toma texto/facturas pendientes, las lee con IA y deja el resultado.
Corre en el servidor (donde está `agy`); habla con la API local con el token del bot. Servicio: facundo-worker.
"""
import logging
import os
import sys
import time
from pathlib import Path

import requests

import extraccion

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("facundo-worker")

BASE = Path(__file__).resolve().parent
UPLOADS = Path(os.environ.get("UPLOADS_DIR", "/home/yoseth/Dev/botero/facturas/web"))


def load_env() -> dict:
    env = {}
    f = BASE / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("\"'")
    return env


ENV = load_env()
API, TOKEN = ENV.get("GASTOS_API_URL"), ENV.get("GASTOS_API_TOKEN")
if not (API and TOKEN):
    sys.exit("Falta GASTOS_API_URL / GASTOS_API_TOKEN en .env")
H = {"Authorization": f"Bearer {TOKEN}"}


def post(path: str, **kw):
    r = requests.post(f"{API.rstrip('/')}{path}", headers=H, timeout=15, **kw)
    r.raise_for_status()
    return r.json()


def procesar(c: dict):
    if not post(f"/api/capturas/{c['id']}/reclamar").get("ok"):
        return                                        # otro proceso la tomó
    archivo = None
    if c.get("archivo"):
        archivo = (UPLOADS / c["archivo"]).resolve()
        if UPLOADS.resolve() not in archivo.parents or not archivo.is_file():
            post(f"/api/capturas/{c['id']}/resultado", json={"ok": False, "mensaje": "No encontré el archivo adjunto."})
            return
    log.info(f"Captura {c['id']}: {'archivo' if archivo else 'texto'}")
    res = extraccion.extraer(c.get("texto", ""), archivo)
    post(f"/api/capturas/{c['id']}/resultado", json=res if res["ok"] else {"ok": False, "mensaje": res["mensaje"]})
    log.info(f"Captura {c['id']}: {'lista' if res['ok'] else 'error'}")


def main():
    log.info("Worker de capturas iniciado")
    while True:
        try:
            for c in requests.get(f"{API.rstrip('/')}/api/capturas/pendientes", headers=H, timeout=15).json():
                procesar(c)
            time.sleep(2)
        except Exception as e:
            log.error(f"Error en el ciclo: {e}")
            time.sleep(8)


if __name__ == "__main__":
    main()
