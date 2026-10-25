"""Cliente mínimo de la API de gastos-app (espejo del registro en MySQL)."""
import logging
import requests

logger = logging.getLogger("facundobot.api")


def _post(base_url: str, token: str, path: str, payload: dict) -> int:
    r = requests.post(
        f"{base_url.rstrip('/')}{path}",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["id"]


def push_record(base_url: str, token: str, extracted_type: str, data: dict, archivo: str = "") -> int:
    if extracted_type == "servicio":
        monto = data.get("monto")
        return _post(base_url, token, "/api/pagos", {
            "periodo": data["fecha"],
            "categoria": data["categoria"],
            "monto": monto if monto not in ("", None) else None,
            "estado": data.get("estado", "ok"),
            "archivo": archivo,
            "nota": data.get("nota", ""),
        })
    return _post(base_url, token, "/api/compras", {
        "fecha": data["fecha"],
        "comercio": data["comercio"],
        "total": data["total"],
        "medio_pago": data.get("medio_pago", ""),
        "nota": data.get("nota", ""),
        "items": data.get("items", []),
    })


def push_movimiento(base_url: str, token: str, mov: dict) -> dict:
    """Envía un gasto/ingreso personal. Devuelve la respuesta de la API (id, duplicado, tipo, categoria)."""
    payload = {k: mov[k] for k in ("ocurrido", "tipo", "monto", "contraparte", "medio", "banco", "nota", "origen", "ref", "categoria", "recurrencia", "proximo") if mov.get(k) not in (None, "")}
    r = requests.post(
        f"{base_url.rstrip('/')}/api/movimientos", json=payload,
        headers={"Authorization": f"Bearer {token}"}, timeout=10,
    )
    r.raise_for_status()
    return r.json()


def list_movimientos(base_url: str, token: str) -> list:
    r = requests.get(f"{base_url.rstrip('/')}/api/movimientos", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    return r.json()


def list_pagos(base_url: str, token: str) -> list:
    r = requests.get(f"{base_url.rstrip('/')}/api/pagos", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    return r.json()


def list_compras(base_url: str, token: str) -> list:
    r = requests.get(f"{base_url.rstrip('/')}/api/compras", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    return r.json()


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def crear_notificacion(base_url: str, token: str, tipo: str, titulo: str, texto: str = "") -> int:
    r = requests.post(f"{base_url.rstrip('/')}/api/notificaciones", json={"tipo": tipo, "titulo": titulo[:160], "texto": texto[:2000]},
                      headers=_h(token), timeout=10)
    r.raise_for_status()
    return r.json()["id"]


def notificaciones_telegram_pendientes(base_url: str, token: str) -> list:
    r = requests.get(f"{base_url.rstrip('/')}/api/notificaciones/telegram-pendientes", headers=_h(token), timeout=10)
    r.raise_for_status()
    return r.json()


def marcar_telegram(base_url: str, token: str, nid: int, ok: bool) -> None:
    requests.post(f"{base_url.rstrip('/')}/api/notificaciones/{nid}/telegram", json={"ok": ok}, headers=_h(token), timeout=10).raise_for_status()


def crear_captura_externa(base_url: str, token: str, titulo: str, texto: str, resultado: dict, archivo: str | None, archivo_nombre: str) -> dict:
    r = requests.post(f"{base_url.rstrip('/')}/api/capturas/externa", headers=_h(token), timeout=15,
                      json={"titulo": titulo[:160], "texto": texto[:2000], "archivo": archivo, "archivo_nombre": archivo_nombre[:200], "resultado": resultado})
    r.raise_for_status()
    return r.json()


def capturas_por_confirmar(base_url: str, token: str) -> int:
    r = requests.get(f"{base_url.rstrip('/')}/api/capturas/por-confirmar", headers=_h(token), timeout=10)
    r.raise_for_status()
    return r.json()["n"]
