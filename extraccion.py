"""
Lectura de gastos con IA (Antigravity `agy`): texto libre y/o foto o PDF de una factura.
Un único prompt clasifica en alimentación / servicio del hogar / personal (gasto o ingreso).
"""
import json
import logging
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("facundobot.extraccion")

AGY_BIN = shutil.which("agy") or "/home/yoseth/.local/bin/agy"
MODELO = "gemini-3.8-flash-medium"
TIMEOUT = 240

CAT_ITEMS = ["Aseo/Hogar", "Bebidas", "Granos", "Lácteos", "Mecato", "Proteínas", "Verduras/Frutas"]
CAT_SERVICIOS = ["Administración", "Agua", "Claro", "Cuota apto", "DIAN", "Gas",
                 "Impuesto predial", "Libranza", "Luz", "Seguridad social", "Seguro moto"]
CAT_GASTO = ["Transporte", "Salud", "Ocio y salidas", "Ropa y cuidado personal", "Educación",
             "Hogar y mantenimiento", "Regalos y familia", "Préstamos y deudas", "Suscripciones", "Comisiones", "Otros gastos"]
CAT_INGRESO = ["Salario", "Trabajo independiente", "Reembolsos", "Otros ingresos"]
MEDIOS = ["Efectivo", "Tarjeta", "Transferencia", "Nequi", "Daviplata", "Otro"]


def _hoy() -> datetime:
    return datetime.now(ZoneInfo("America/Bogota"))


def _prompt(texto: str, archivo: Path | None) -> str:
    hoy = _hoy()
    if archivo:
        fuente = (f"Lee detenidamente el archivo de factura o comprobante en:\n{archivo}\n"
                  "utilizando tu herramienta de lectura de archivos.\n")
        if texto:
            fuente += f"El usuario añadió esta nota sobre el archivo: «{texto}»\n"
    else:
        fuente = f"Interpreta este mensaje en español donde el usuario reporta un gasto o ingreso: «{texto}»\n"
    return f"""{fuente}
Fecha de referencia: {hoy:%Y-%m-%d} (si dice «hoy» o no da fecha, usa esta; «ayer» es el día anterior).

Clasifícalo en UNA de estas opciones y responde con el JSON indicado:

1. "alimentacion": compra de comida, mercado, supermercado, tienda de barrio, carnicería, panadería, restaurante, almuerzo, café, mecato, aseo del hogar.
   Un mercado o supermercado sin factura también es alimentacion (comercio, p. ej., "Supermercado (sin factura)").
   {{"tipo":"alimentacion","data":{{"fecha":"YYYY-MM-DD","comercio":"nombre","total":29000,"medio_pago":"uno de {MEDIOS} o vacío","nota":"",
     "items":[{{"categoria":"una de {CAT_ITEMS}","producto":"DESCRIPCIÓN EN MAYÚSCULAS","cantidad":1,"unidad":"Kg|Und|PAQ","precio_unit":1000,"subtotal":1000}}]}}}}
   Si hay una factura legible, lista los ítems; si es solo un mensaje, "items" va vacío.

2. "servicio": recibo del hogar: luz, agua, gas, Claro (internet/teléfono fijo), administración, cuota del apartamento, impuesto predial, DIAN, libranza, seguro de la moto.
   {{"tipo":"servicio","data":{{"periodo":"YYYY-MM (mes del pago)","categoria":"una de {CAT_SERVICIOS}","monto":150000,"nota":"breve, sin datos sensibles"}}}}

3. "personal": cualquier otro gasto o ingreso personal que NO sea comida ni un recibo del hogar (taxi, médico, farmacia, cine, ropa, peluquería, regalo, préstamo, curso, suscripciones como Netflix/Spotify/plan de celular) o plata recibida (me pagaron, consignaron, reembolso).
   {{"tipo":"personal","data":{{"tipo":"gasto|ingreso","categoria":"una de {CAT_GASTO} si es gasto, o de {CAT_INGRESO} si es ingreso, o null si dudas","fecha":"YYYY-MM-DD","monto":12000,"contraparte":"persona o lugar si aparece","medio":"uno de {MEDIOS} o vacío","nota":"breve"}}}}

Si no hay ningún monto o no describe un gasto/ingreso: {{"tipo":"desconocido"}}.

Reglas:
- Montos en pesos colombianos, como número sin símbolos ni separadores (29.000 = 29000).
- Nunca incluyas números de cédula, de cuenta ni de tarjeta.
- Responde ÚNICAMENTE con el objeto JSON válido, sin explicaciones ni bloques de código."""


def _json_de(salida: str) -> dict:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", salida, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    a, b = salida.find("{"), salida.rfind("}")
    return json.loads(salida[a:b + 1] if a != -1 and b != -1 else salida)


def _num(v, default=0.0) -> float:
    """Número o texto con formato colombiano ('29.000', '1.234,50', '$ 3,500') -> float."""
    if isinstance(v, (int, float)):
        return float(v)
    t = re.sub(r"[^\d.,\-]", "", str(v or "")).rstrip(".,")
    if not t or t == "-":
        return default
    if "." in t and "," in t:
        dec = "." if t.rfind(".") > t.rfind(",") else ","
        t = t.replace("," if dec == "." else ".", "").replace(dec, ".")
    elif "." in t or "," in t:
        sep = "." if "." in t else ","
        t = t.replace(sep, "") if (len(t.split(sep)[-1]) == 3 or t.count(sep) > 1) else t.replace(sep, ".")
    try:
        return float(t)
    except ValueError:
        return default


def _fecha(v) -> str:
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return _hoy().strftime("%Y-%m-%d")


def normalizar(res: dict) -> dict | None:
    """Deja el resultado de la IA en una forma segura y con categorías válidas. None si no se entendió."""
    tipo, d = res.get("tipo"), res.get("data") or {}
    if tipo == "alimentacion":
        total = _num(d.get("total"))
        if total <= 0:
            return None
        items = []
        for i in d.get("items") or []:
            sub = _num(i.get("subtotal"))
            if sub > 0:
                items.append({"categoria": i.get("categoria") if i.get("categoria") in CAT_ITEMS else "Aseo/Hogar",
                              "producto": str(i.get("producto") or "")[:200], "cantidad": _num(i.get("cantidad"), 1) or 1,
                              "unidad": str(i.get("unidad") or "")[:20], "precio_unit": _num(i.get("precio_unit")), "subtotal": sub})
        return {"tipo": "alimentacion", "data": {"fecha": _fecha(d.get("fecha")), "comercio": str(d.get("comercio") or "Compra")[:120],
                "total": total, "medio_pago": d.get("medio_pago") if d.get("medio_pago") in MEDIOS else "",
                "nota": str(d.get("nota") or "")[:300], "items": items}}
    if tipo == "servicio":
        monto = _num(d.get("monto"))
        if monto <= 0:
            return None
        per = str(d.get("periodo") or d.get("fecha") or "")[:7]
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", per):
            per = _hoy().strftime("%Y-%m")
        return {"tipo": "servicio", "data": {"periodo": per, "categoria": d.get("categoria") if d.get("categoria") in CAT_SERVICIOS else None,
                "monto": monto, "nota": str(d.get("nota") or "")[:300]}}
    if tipo == "personal":
        monto = _num(d.get("monto"))
        if monto <= 0:
            return None
        t = "ingreso" if str(d.get("tipo")).lower() == "ingreso" else "gasto"
        validas = CAT_INGRESO if t == "ingreso" else CAT_GASTO
        return {"tipo": "personal", "data": {"tipo": t, "categoria": d.get("categoria") if d.get("categoria") in validas else None,
                "fecha": _fecha(d.get("fecha")), "monto": monto, "contraparte": str(d.get("contraparte") or "")[:120],
                "medio": d.get("medio") if d.get("medio") in MEDIOS else "", "nota": str(d.get("nota") or "")[:300]}}
    return None


def extraer(texto: str = "", archivo: Path | None = None) -> dict:
    """Devuelve {"ok": True, "resultado": {tipo, data}} o {"ok": False, "mensaje": ...}."""
    try:
        r = subprocess.run([AGY_BIN, "-p", _prompt(texto, archivo), "--model", MODELO], capture_output=True, text=True, timeout=TIMEOUT)
        crudo = (r.stdout or r.stderr).strip()
        norm = normalizar(_json_de(crudo))
        if not norm:
            return {"ok": False, "desconocido": True, "mensaje": "No pude identificar un monto o el tipo de movimiento. Puedes llenarlo a mano."}
        return {"ok": True, "resultado": norm}
    except subprocess.TimeoutExpired:
        return {"ok": False, "mensaje": "El análisis tardó demasiado. Intenta de nuevo o llénalo a mano."}
    except Exception as e:
        logger.error(f"Error en extracción: {e}")
        return {"ok": False, "mensaje": "No pude leer la respuesta de la IA. Puedes llenarlo a mano."}
