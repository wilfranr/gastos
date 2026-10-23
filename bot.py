#!/usr/bin/env python3
"""
Bot de Telegram (@Facundo1bot) para Control de Gastos · Yoseth.
Lee fotos/PDFs de facturas con visión (Antigravity agy), extrae datos, valida,
solicita confirmación y actualiza alimentacion.json o gastos.csv (con build.py y git push).
"""

import os
import sys
import json
import csv
import time
import re
import shutil
import logging
import subprocess
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import requests

# Configuración de Logging
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("facundobot")

# Rutas del proyecto
BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR
FACTURAS_DIR = Path("/home/yoseth/Dev/botero/facturas")
FACTURAS_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = REPO_DIR / "bot_state.json"
ENV_FILE = REPO_DIR / ".env"
ALIMENTACION_FILE = REPO_DIR / "alimentacion.json"
GASTOS_CSV_FILE = REPO_DIR / "gastos.csv"
DATA_JSON_FILE = REPO_DIR / "data.json"
BUILD_PY = REPO_DIR / "build.py"
AGY_BIN = shutil.which("agy") or "/home/yoseth/.local/bin/agy"

# Categorías existentes estrictas
CATEGORIAS_ALIMENTACION = [
    "Aseo/Hogar", "Bebidas", "Granos", "Lácteos", "Mecato", "Proteínas", "Verduras/Frutas"
]
CATEGORIAS_SERVICIOS = [
    "Administración", "Agua", "Claro", "Cuota apto", "DIAN", "Gas",
    "Impuesto predial", "Libranza", "Luz", "Seguro moto"
]

MESES_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"
]


def load_env():
    """Carga variables desde .env sin imprimir valores sensibles."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("\"'")
    return env


ENV_VARS = load_env()
BOT_TOKEN = ENV_VARS.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = ENV_VARS.get("TELEGRAM_CHAT_ID")

if not BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN no encontrado en .env")
    sys.exit(1)

try:
    ALLOWED_CHAT_ID = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else None
except ValueError:
    ALLOWED_CHAT_ID = None

if not ALLOWED_CHAT_ID:
    logger.error("TELEGRAM_CHAT_ID no configurado en .env o inválido")
    sys.exit(1)


class StateManager:
    """Maneja el estado persistente del bot."""
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Error cargando {self.path}: {e}")
        return {
            "last_update_id": 0,
            "pending": {},
            "editing_id": None,
            "invoice_history": []
        }

    def save(self):
        try:
            self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error guardando {self.path}: {e}")


state = StateManager(STATE_FILE)


class TelegramClient:
    """Cliente para la API de Telegram con long-polling."""
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_base_url = f"https://api.telegram.org/file/bot{token}"
        self.session = requests.Session()

    def request(self, method: str, payload=None, files=None, timeout=35):
        url = f"{self.base_url}/{method}"
        try:
            resp = self.session.post(url, json=payload, files=files, timeout=timeout)
            return resp.json()
        except Exception as e:
            logger.error(f"Telegram API error ({method}): {e}")
            return {"ok": False, "error": str(e)}

    def send_message(self, chat_id: int, text: str, reply_markup=None, reply_to_message_id=None, parse_mode="Markdown"):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        return self.request("sendMessage", payload=payload)

    def edit_message(self, chat_id: int, message_id: int, text: str, reply_markup=None, parse_mode="Markdown"):
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("editMessageText", payload=payload)

    def answer_callback(self, callback_query_id: str, text=None):
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self.request("answerCallbackQuery", payload=payload)

    def send_action(self, chat_id: int, action="typing"):
        return self.request("sendChatAction", payload={"chat_id": chat_id, "action": action})

    def get_file(self, file_id: str):
        return self.request("getFile", payload={"file_id": file_id})

    def download_file(self, file_path: str, dest: Path):
        url = f"{self.file_base_url}/{file_path}"
        try:
            r = self.session.get(url, stream=True, timeout=60)
            if r.status_code == 200:
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                return True
        except Exception as e:
            logger.error(f"Error descargando archivo {file_path}: {e}")
        return False

    def send_photo(self, chat_id: int, photo_path: Path, caption=None):
        url = f"{self.base_url}/sendPhoto"
        try:
            with open(photo_path, "rb") as f:
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                    data["parse_mode"] = "Markdown"
                return self.session.post(url, data=data, files={"photo": f}, timeout=60).json()
        except Exception as e:
            logger.error(f"Error enviando foto {photo_path}: {e}")
            return {"ok": False, "error": str(e)}

    def send_document(self, chat_id: int, doc_path: Path, caption=None):
        url = f"{self.base_url}/sendDocument"
        try:
            with open(doc_path, "rb") as f:
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                    data["parse_mode"] = "Markdown"
                return self.session.post(url, data=data, files={"document": f}, timeout=60).json()
        except Exception as e:
            logger.error(f"Error enviando documento {doc_path}: {e}")
            return {"ok": False, "error": str(e)}

    def get_updates(self, offset: int, timeout=25):
        payload = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"]
        }
        return self.request("getUpdates", payload=payload, timeout=timeout + 10)


bot = TelegramClient(BOT_TOKEN)


def sanitize_text(text: str) -> str:
    """Oculta cédulas, números de cliente/cuenta largos o direcciones si aparecen."""
    if not text:
        return ""
    # Ocultar secuencias de 7 o más dígitos (cédulas o cuentas)
    text = re.sub(r"\b(\d{2,4})\d{4,8}(\d{2,4})\b", r"\1***\2", text)
    # Ocultar direcciones tipo Calle / Cra / Carrera
    text = re.sub(r"(?i)\b(calle|cra|carrera|diagonal|transversal|av|avenida)\s+[\w\d\s#-]+", "[Dirección omitida]", text)
    return text


def extract_with_agy(file_path: Path) -> dict:
    """Ejecuta agy para analizar la factura/comprobante con visión."""
    prompt = f"""Lee detenidamente el archivo de factura o comprobante en:
{file_path}
utilizando tu herramienta de lectura de archivos.

Clasifícalo en una de dos opciones:
1. "alimentacion": compra de supermercado, mercado, carnicería, panadería, comida, aseo del hogar (ej. Éxito, D1, Surtimayorista, Ara, Oxxo, Carnes Frías, etc.).
2. "servicio": recibo de servicio público (Luz, Agua, Gas, Claro), cuota de administración, impuesto (DIAN, Impuesto predial), seguro, libranza, crédito.

REGLAS DE FORMATO Y CATEGORÍAS:
Para "alimentacion", el esquema debe ser:
{{
  "tipo": "alimentacion",
  "data": {{
    "fecha": "YYYY-MM-DD",
    "anio": "YYYY",
    "mes": 10,
    "periodo": "YYYY-MM",
    "comercio": "Nombre del comercio",
    "total": 182350,
    "medio_pago": "Tarjeta" o "Efectivo" o "Transferencia" o "",
    "nota": "",
    "items": [
      {{
        "categoria": "Una de: Aseo/Hogar, Bebidas, Granos, Lácteos, Mecato, Proteínas, Verduras/Frutas",
        "producto": "DESCRIPCION DEL ITEM EN MAYUSCULAS",
        "cantidad": 1.0,
        "unidad": "Kg" o "Und" o "PAQ",
        "precio_unit": 1000,
        "subtotal": 1000
      }}
    ]
  }}
}}

Para "servicio", el esquema debe ser:
{{
  "tipo": "servicio",
  "data": {{
    "fecha": "YYYY-MM",
    "anio": "YYYY",
    "mes": 10,
    "categoria": "Una de: Administración, Agua, Claro, Cuota apto, DIAN, Gas, Impuesto predial, Libranza, Luz, Seguro moto",
    "monto": 150000.0,
    "estado": "ok",
    "archivo": "{file_path.name}",
    "nota": "Breve nota sin datos sensibles (ej: Enel cliente terminado en 198, pagado 16/10/2026)"
  }}
}}

REGLAS DE SEGURIDAD Y PRIVACIDAD OBLIGATORIAS:
- El repositorio de GitHub es PÚBLICO.
- NUNCA incluyas números de cédula, NITs personales, direcciones de residencia completas, ni números de cuenta bancaria o de cliente completos.
- Si están en la factura, omítelos o enmascáralos en la nota.
- Responde ÚNICAMENTE con el objeto JSON válido. Sin explicaciones, sin texto adicional, sin bloques de código markdown."""

    cmd = [AGY_BIN, "-p", prompt, "--model", "gemini-3.8-flash-medium"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        output = res.stdout.strip()
        if not output:
            output = res.stderr.strip()

        # Limpiar posibles bloques ```json ... ```
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", output, re.DOTALL)
        if match:
            json_str = match.group(1)
        else:
            json_start = output.find("{")
            json_end = output.rfind("}")
            if json_start != -1 and json_end != -1:
                json_str = output[json_start:json_end+1]
            else:
                json_str = output

        data = json.loads(json_str)
        return {"ok": True, "result": data}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Tiempo de espera agotado al leer la factura con el agente."}
    except Exception as e:
        logger.error(f"Error procesando con agy: {e}")
        return {"ok": False, "error": f"Error al parsear respuesta: {str(e)}"}


def check_duplicates(extracted_type: str, data: dict) -> tuple[bool, str]:
    """Comprueba si ya existe un registro con el mismo comercio/categoría, fecha y monto."""
    if extracted_type == "alimentacion":
        if ALIMENTACION_FILE.exists():
            try:
                records = json.loads(ALIMENTACION_FILE.read_text(encoding="utf-8"))
                for r in records:
                    same_com = r.get("comercio", "").strip().lower() == data.get("comercio", "").strip().lower()
                    same_fecha = r.get("fecha") == data.get("fecha")
                    same_total = abs(float(r.get("total", 0)) - float(data.get("total", 0))) < 1
                    if same_com and same_fecha and same_total:
                        return True, f"⚠️ *Aviso de duplicado:* Ya existe una compra de *{r.get('comercio')}* del *{r.get('fecha')}* por *${r.get('total'):,d}*."
            except Exception as e:
                logger.warning(f"Error leyendo alimentacion.json para duplicados: {e}")
    elif extracted_type == "servicio":
        if GASTOS_CSV_FILE.exists():
            try:
                with open(GASTOS_CSV_FILE, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        same_cat = row.get("categoria", "").strip().lower() == data.get("categoria", "").strip().lower()
                        same_fecha = row.get("fecha") == data.get("fecha")
                        row_monto = float(row.get("monto", 0)) if row.get("monto") else 0
                        same_monto = abs(row_monto - float(data.get("monto", 0))) < 1
                        if same_cat and same_fecha and same_monto:
                            return True, f"⚠️ *Aviso de duplicado:* Ya existe un pago de *{row.get('categoria')}* del periodo *{row.get('fecha')}* por *${row_monto:,.0f}*."
            except Exception as e:
                logger.warning(f"Error leyendo gastos.csv para duplicados: {e}")
    return False, ""


def check_math(extracted_type: str, data: dict) -> tuple[bool, str]:
    """Verifica si la suma de los ítems cuadra con el total en alimentación."""
    if extracted_type != "alimentacion":
        return True, ""
    items = data.get("items", [])
    if not items:
        return True, "ℹ️ No se desglosaron ítems individuales."
    total = float(data.get("total", 0))
    sum_items = sum(float(it.get("subtotal", 0)) for it in items)
    diff = round(sum_items - total, 2)
    if abs(diff) <= 1:
        return True, f"✅ La suma de los ítems (${sum_items:,.0f}) cuadra perfectamente con el total (${total:,.0f})."
    else:
        return False, f"⚠️ *Discrepancia en ítems:* La suma de los ítems es *${sum_items:,.0f}*, pero el total indicado es *${total:,.0f}* (diferencia: *${diff:+,.0f}*)."


def format_preview(item_id: str, extracted_type: str, data: dict, math_msg: str, dup_msg: str) -> tuple[str, dict]:
    """Construye el texto del resumen y los botones inline para Telegram."""
    lines = []
    if extracted_type == "alimentacion":
        comercio = data.get("comercio", "Desconocido")
        fecha = data.get("fecha", "")
        periodo = data.get("periodo", "")
        total = float(data.get("total", 0))
        items = data.get("items", [])
        medio = data.get("medio_pago", "") or "No especificado"
        nota = sanitize_text(data.get("nota", ""))

        lines.append(f"🧾 *Factura de Alimentación*\n")
        lines.append(f"🏪 *Comercio:* {comercio}")
        lines.append(f"📅 *Fecha:* {fecha} (`{periodo}`)")
        lines.append(f"💰 *Total:* *${total:,.0f}*")
        lines.append(f"💳 *Medio de pago:* {medio}")
        if nota:
            lines.append(f"📝 *Nota:* {nota}")
        lines.append(f"📦 *Productos:* {len(items)} ítems")

        # Muestra hasta 8 ítems resumidos
        if items:
            lines.append("\n*Detalle de compras:*")
            for it in items[:8]:
                prod = it.get("producto", "")
                cat = it.get("categoria", "")
                sub = float(it.get("subtotal", 0))
                cant = it.get("cantidad", 1)
                und = it.get("unidad", "Und")
                lines.append(f"• `{cant} {und}` {prod} ({cat}): *${sub:,.0f}*")
            if len(items) > 8:
                lines.append(f"  _...y {len(items) - 8} ítems más._")

    else:
        cat = data.get("categoria", "Servicio")
        fecha = data.get("fecha", "")
        monto = float(data.get("monto", 0))
        nota = sanitize_text(data.get("nota", ""))

        lines.append(f"⚡ *Recibo de Servicio / Impuesto*\n")
        lines.append(f"🏷️ *Categoría:* {cat}")
        lines.append(f"📅 *Periodo:* `{fecha}`")
        lines.append(f"💰 *Monto:* *${monto:,.0f}*")
        if nota:
            lines.append(f"📝 *Nota:* {nota}")

    if math_msg:
        lines.append(f"\n{math_msg}")
    if dup_msg:
        lines.append(f"\n{dup_msg}")

    lines.append("\n¿Deseas confirmar y publicar este registro?")

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Confirmar", "callback_data": f"confirm:{item_id}"},
                {"text": "✏️ Corregir", "callback_data": f"edit:{item_id}"},
                {"text": "❌ Descartar", "callback_data": f"discard:{item_id}"}
            ]
        ]
    }
    return "\n".join(lines), reply_markup


def generate_analysis(extracted_type: str, current_data: dict) -> str:
    """Genera análisis comparativo contra meses anteriores usando datos reales."""
    lines = ["📊 *Análisis de impacto en tus gastos:*"]
    try:
        if extracted_type == "alimentacion":
            if not ALIMENTACION_FILE.exists():
                return ""
            records = json.loads(ALIMENTACION_FILE.read_text(encoding="utf-8"))
            periodo_actual = current_data.get("periodo", "")

            # Agrupar por periodo
            by_period = defaultdict(list)
            for r in records:
                by_period[r.get("periodo")].append(r)

            sorted_periods = sorted(by_period.keys())
            if periodo_actual in sorted_periods:
                idx = sorted_periods.index(periodo_actual)
                periodo_ant = sorted_periods[idx - 1] if idx > 0 else None
            else:
                periodo_ant = sorted_periods[-1] if sorted_periods else None

            tot_actual = sum(float(r.get("total", 0)) for r in by_period[periodo_actual])
            lines.append(f"• *Alimentación en {periodo_actual}:* ${tot_actual:,.0f}")

            if periodo_ant:
                tot_ant = sum(float(r.get("total", 0)) for r in by_period[periodo_ant])
                diff = tot_actual - tot_ant
                pct = ((diff / tot_ant) * 100) if tot_ant > 0 else 0
                sign = "+" if diff > 0 else ""
                lines.append(f"• *vs. {periodo_ant}:* ${tot_ant:,.0f} ({sign}${diff:,.0f} / {sign}{pct:.1f}%)")

                # Desglose de categorías de ítems
                cats_act = defaultdict(float)
                for r in by_period[periodo_actual]:
                    for it in r.get("items", []):
                        cats_act[it.get("categoria", "Otros")] += float(it.get("subtotal", 0))

                cats_ant = defaultdict(float)
                for r in by_period[periodo_ant]:
                    for it in r.get("items", []):
                        cats_ant[it.get("categoria", "Otros")] += float(it.get("subtotal", 0))

                subieron = []
                bajaron = []
                for cat, val in cats_act.items():
                    v_ant = cats_ant.get(cat, 0)
                    d = val - v_ant
                    if d > 10000:
                        subieron.append(f"{cat} (+${d:,.0f})")
                    elif d < -10000:
                        bajaron.append(f"{cat} (-${abs(d):,.0f})")

                if subieron:
                    lines.append(f"🔺 *Subieron:* {', '.join(subieron[:3])}")
                if bajaron:
                    lines.append(f"🔻 *Bajaron:* {', '.join(bajaron[:3])}")

        elif extracted_type == "servicio":
            if not DATA_JSON_FILE.exists():
                return ""
            records = json.loads(DATA_JSON_FILE.read_text(encoding="utf-8"))
            cat = current_data.get("categoria", "")
            periodo_actual = current_data.get("fecha", "")

            cat_history = [r for r in records if r.get("categoria") == cat]
            cat_history.sort(key=lambda x: x.get("fecha", ""))

            lines.append(f"• *Pago registrado para {cat}:* ${float(current_data.get('monto', 0)):,.0f}")
            if len(cat_history) >= 2:
                ant = cat_history[-2]
                m_ant = float(ant.get("monto", 0)) if ant.get("monto") else 0
                m_act = float(current_data.get("monto", 0))
                diff = m_act - m_ant
                pct = ((diff / m_ant) * 100) if m_ant > 0 else 0
                sign = "+" if diff > 0 else ""
                lines.append(f"• *vs. periodo anterior ({ant.get('fecha')}):* ${m_ant:,.0f} ({sign}${diff:,.0f} / {sign}{pct:.1f}%)")

    except Exception as e:
        logger.warning(f"Error generando análisis: {e}")
    return "\n".join(lines)


def commit_and_push(extracted_type: str, data: dict, original_file: Path) -> tuple[bool, str, int]:
    """Guarda en JSON o CSV, ejecuta build.py si aplica, valida, comitea y pushea a git."""
    try:
        commit_msg = ""
        files_to_commit = []
        rec_index = 0

        if extracted_type == "alimentacion":
            # 1. Leer alimentacion.json
            records = []
            if ALIMENTACION_FILE.exists():
                records = json.loads(ALIMENTACION_FILE.read_text(encoding="utf-8"))

            records.append(data)
            rec_index = len(records)

            # Formatear exactamente con indent=1 como el repositorio
            formatted_json = json.dumps(records, indent=1, ensure_ascii=False) + "\n"
            ALIMENTACION_FILE.write_text(formatted_json, encoding="utf-8")

            # Validar que sea JSON válido
            json.loads(ALIMENTACION_FILE.read_text(encoding="utf-8"))
            files_to_commit = ["alimentacion.json"]

            # Mensaje de commit
            fecha_str = data.get("fecha", "")
            comercio = data.get("comercio", "")
            try:
                dt = datetime.strptime(fecha_str, "%Y-%m-%d")
                mes_nombre = MESES_ES[dt.month]
                fecha_texto = f"{dt.day} de {mes_nombre}"
            except Exception:
                fecha_texto = fecha_str
            commit_msg = f"Agrega la compra de {comercio} del {fecha_texto}."

        elif extracted_type == "servicio":
            # 1. Agregar a gastos.csv
            new_row = [
                data.get("fecha", ""),
                str(data.get("anio", "")),
                str(data.get("mes", "")),
                data.get("categoria", ""),
                str(float(data.get("monto", 0))),
                data.get("estado", "ok"),
                original_file.name if original_file else "",
                sanitize_text(data.get("nota", ""))
            ]

            # Leer lineas actuales para contar
            with open(GASTOS_CSV_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            rec_index = len(lines)

            # Escribir fila
            with open(GASTOS_CSV_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(new_row)

            # 2. Correr build.py
            build_res = subprocess.run([sys.executable, str(BUILD_PY)], cwd=REPO_DIR, capture_output=True, text=True)
            if build_res.returncode != 0:
                raise RuntimeError(f"build.py falló: {build_res.stderr}")

            # Validar data.json
            json.loads(DATA_JSON_FILE.read_text(encoding="utf-8"))
            files_to_commit = ["gastos.csv", "data.json"]

            # Mensaje de commit
            cat = data.get("categoria", "")
            try:
                mes_num = int(data.get("mes", 1))
                mes_nombre = MESES_ES[mes_num]
                periodo_texto = f"{mes_nombre} {data.get('anio', '')}"
            except Exception:
                periodo_texto = data.get("fecha", "")
            commit_msg = f"Agrega el pago de {cat} de {periodo_texto}."

        # Git add & commit
        subprocess.run(["git", "add"] + files_to_commit, cwd=REPO_DIR, check=True)
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=REPO_DIR, check=True)

        # Git push
        push_res = subprocess.run(["git", "push", "origin", "main"], cwd=REPO_DIR, capture_output=True, text=True)
        if push_res.returncode != 0:
            logger.error(f"Error en git push: {push_res.stderr}")
            return True, f"⚠️ Cambios guardados localmente pero el push a GitHub falló (credenciales o red). No se reintentó automáticamente.", rec_index

        return True, "✅ Cambios guardados y publicados exitosamente en GitHub Pages.", rec_index

    except Exception as e:
        logger.error(f"Error guardando o comiteando: {e}")
        return False, f"❌ Error guardando el registro: {str(e)}", 0


def handle_document_or_photo(message: dict):
    """Procesa una imagen o PDF enviado por Telegram."""
    chat_id = message["chat"]["id"]
    file_id = None
    file_name = "factura.jpg"

    if "photo" in message:
        # Tomar la imagen de mayor resolución
        file_id = message["photo"][-1]["file_id"]
        file_name = f"foto_{int(time.time())}.jpg"
    elif "document" in message:
        doc = message["document"]
        file_id = doc["file_id"]
        file_name = doc.get("file_name", f"doc_{int(time.time())}.pdf")

    if not file_id:
        return

    # Notificar inicio
    status_msg = bot.send_message(chat_id, "📥 *Descargando archivo...*")
    status_id = status_msg.get("result", {}).get("message_id")

    # Obtener ruta de descarga
    res = bot.get_file(file_id)
    if not res.get("ok"):
        bot.edit_message(chat_id, status_id, "❌ Error al obtener el archivo desde Telegram.")
        return

    remote_path = res["result"]["file_path"]
    ext = Path(file_name).suffix or ".jpg"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_name = f"{timestamp}_{file_id[:8]}{ext}"
    dest_path = FACTURAS_DIR / local_name

    if not bot.download_file(remote_path, dest_path):
        bot.edit_message(chat_id, status_id, "❌ Error al descargar el archivo en el servidor.")
        return

    bot.edit_message(chat_id, status_id, "🔍 *Leyendo factura con visión (Antigravity agy)...*\nEsto puede tomar unos 10-15 segundos.")
    bot.send_action(chat_id, "typing")

    # Extraer con visión
    extracted = extract_with_agy(dest_path)
    if not extracted.get("ok"):
        bot.edit_message(chat_id, status_id, f"❌ Error extrayendo datos de la factura:\n{extracted.get('error')}")
        return

    payload = extracted["result"]
    extracted_type = payload.get("tipo", "alimentacion")
    data = payload.get("data", {})

    # Sanitizar notas
    if "nota" in data:
        data["nota"] = sanitize_text(data["nota"])

    # Validaciones
    math_ok, math_msg = check_math(extracted_type, data)
    dup_found, dup_msg = check_duplicates(extracted_type, data)

    # Guardar en estado pendiente
    item_id = str(int(time.time()))
    state.data["pending"][item_id] = {
        "tipo": extracted_type,
        "data": data,
        "filepath": str(dest_path),
        "math_msg": math_msg,
        "dup_msg": dup_msg,
        "created_at": time.time()
    }
    state.save()

    preview_text, reply_markup = format_preview(item_id, extracted_type, data, math_msg, dup_msg)
    bot.edit_message(chat_id, status_id, preview_text, reply_markup=reply_markup)


def handle_callback_query(cq: dict):
    """Maneja las acciones de los botones inline."""
    cq_id = cq["id"]
    from_id = cq["from"]["id"]
    if from_id != ALLOWED_CHAT_ID:
        bot.answer_callback(cq_id, "Acceso no autorizado.")
        return

    data_str = cq.get("data", "")
    msg = cq.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")

    if ":" not in data_str:
        bot.answer_callback(cq_id)
        return

    action, item_id = data_str.split(":", 1)
    pending_item = state.data["pending"].get(item_id)

    if action == "discard":
        bot.answer_callback(cq_id, "Descartado")
        if item_id in state.data["pending"]:
            del state.data["pending"][item_id]
            state.save()
        bot.edit_message(chat_id, message_id, "❌ *Registro descartado.*")

    elif action == "edit":
        bot.answer_callback(cq_id)
        state.data["editing_id"] = item_id
        state.save()
        prompt_txt = (
            "✏️ *Modo corrección activado*\n"
            "Escribe lo que deseas corregir en una sola línea o en varias. Ejemplos:\n"
            "• `total: 185000`\n"
            "• `comercio: Éxito Viva`\n"
            "• `fecha: 2026-10-22`\n"
            "• `categoria: Luz`\n"
            "• `medio: Tarjeta`\n"
            "• `nota: Enel cliente 198`"
        )
        bot.send_message(chat_id, prompt_txt, reply_to_message_id=message_id)

    elif action == "confirm":
        if not pending_item:
            bot.answer_callback(cq_id, "El registro ya no está pendiente.")
            bot.edit_message(chat_id, message_id, "⚠️ Este registro ya fue procesado o expiró.")
            return

        bot.answer_callback(cq_id, "Procesando...")
        bot.edit_message(chat_id, message_id, "⏳ *Guardando datos, validando y publicando en GitHub...*")

        extracted_type = pending_item["tipo"]
        data = pending_item["data"]
        file_path = Path(pending_item["filepath"])

        success, msg_result, rec_num = commit_and_push(extracted_type, data, file_path)

        # Guardar en historial para /ver <n>
        history_id = len(state.data.get("invoice_history", [])) + 1
        state.data.setdefault("invoice_history", []).append({
            "id": history_id,
            "tipo": extracted_type,
            "comercio": data.get("comercio") or data.get("categoria"),
            "fecha": data.get("fecha"),
            "total": data.get("total") or data.get("monto"),
            "filepath": str(file_path)
        })

        # Eliminar de pendientes
        del state.data["pending"][item_id]
        if state.data.get("editing_id") == item_id:
            state.data["editing_id"] = None
        state.save()

        # Generar análisis
        analysis = generate_analysis(extracted_type, data)

        final_msg = f"{msg_result}\n\n📌 Guardado como registro *#{history_id}*. Puedes consultar la imagen original con `/ver {history_id}`."
        if analysis:
            final_msg += f"\n\n{analysis}"

        bot.edit_message(chat_id, message_id, final_msg)


def handle_text_message(message: dict):
    """Maneja comandos (/resumen, /ver, /ayuda) y correcciones interactivas."""
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    # Comandos
    if text.startswith("/start") or text.startswith("/ayuda") or text.lower() == "hola":
        help_msg = (
            "👋 ¡Hola Yoseth! Soy @Facundo1bot.\n\n"
            "📸 *¿Cómo registrar un gasto?*\n"
            "Solo envíame una foto o PDF de tu factura o recibo. Yo la leeré con visión, verificaré los totales y te pediré confirmación.\n\n"
            "📋 *Comandos disponibles:*\n"
            "• `/resumen`: Compara tus gastos del mes actual vs. el anterior.\n"
            "• `/ver <n>`: Te reenvía la foto o documento de la factura #n.\n"
            "• `/ver`: Muestra la lista de las últimas facturas guardadas."
        )
        bot.send_message(chat_id, help_msg)
        return

    if text.startswith("/resumen"):
        # Resumen mes actual vs mes anterior
        now = datetime.now()
        cur_periodo = now.strftime("%Y-%m")
        # Mes anterior
        prev_month = 12 if now.month == 1 else now.month - 1
        prev_year = now.year - 1 if now.month == 1 else now.year
        prev_periodo = f"{prev_year:04d}-{prev_month:02d}"

        # Alimentación
        food_cur = 0
        food_prev = 0
        if ALIMENTACION_FILE.exists():
            records = json.loads(ALIMENTACION_FILE.read_text(encoding="utf-8"))
            food_cur = sum(float(r.get("total", 0)) for r in records if r.get("periodo") == cur_periodo)
            food_prev = sum(float(r.get("total", 0)) for r in records if r.get("periodo") == prev_periodo)

        # Servicios
        serv_cur = 0
        serv_prev = 0
        if DATA_JSON_FILE.exists():
            data_records = json.loads(DATA_JSON_FILE.read_text(encoding="utf-8"))
            serv_cur = sum(float(r.get("monto", 0)) for r in data_records if r.get("fecha") == cur_periodo and r.get("monto"))
            serv_prev = sum(float(r.get("monto", 0)) for r in data_records if r.get("fecha") == prev_periodo and r.get("monto"))

        tot_cur = food_cur + serv_cur
        tot_prev = food_prev + serv_prev
        diff = tot_cur - tot_prev
        pct = ((diff / tot_prev) * 100) if tot_prev > 0 else 0
        sign = "+" if diff > 0 else ""

        resumen_txt = (
            f"📊 *Resumen de Gastos*\n\n"
            f"🗓️ *Mes actual ({cur_periodo}):*\n"
            f"• Alimentación: ${food_cur:,.0f}\n"
            f"• Servicios/Otros: ${serv_cur:,.0f}\n"
            f"• *Total:* *${tot_cur:,.0f}*\n\n"
            f"🗓️ *Mes anterior ({prev_periodo}):*\n"
            f"• Alimentación: ${food_prev:,.0f}\n"
            f"• Servicios/Otros: ${serv_prev:,.0f}\n"
            f"• *Total:* *${tot_prev:,.0f}*\n\n"
            f"📈 *Variación:* {sign}${diff:,.0f} ({sign}{pct:.1f}%)"
        )
        bot.send_message(chat_id, resumen_txt)
        return

    if text.startswith("/ver"):
        parts = text.split()
        history = state.data.get("invoice_history", [])
        if len(parts) > 1 and parts[1].isdigit():
            rec_id = int(parts[1])
            target = next((item for item in history if item["id"] == rec_id), None)
            if not target:
                bot.send_message(chat_id, f"❌ No encontré ninguna factura con el número #{rec_id}.")
                return

            fpath = Path(target["filepath"])
            if not fpath.exists():
                bot.send_message(chat_id, f"❌ El archivo en el servidor ya no está disponible.")
                return

            caption = f"🧾 Factura #{rec_id}: {target.get('comercio')} ({target.get('fecha')}) - ${float(target.get('total', 0)):,.0f}"
            if fpath.suffix.lower() in [".jpg", ".jpeg", ".png", ".webp"]:
                bot.send_photo(chat_id, fpath, caption=caption)
            else:
                bot.send_document(chat_id, fpath, caption=caption)
            return

        # Si no pasó número, listar las últimas 5
        if not history:
            bot.send_message(chat_id, "ℹ️ Aún no hay facturas registradas en el historial.")
            return

        lines = ["📁 *Últimas facturas registradas:*"]
        for it in history[-5:]:
            lines.append(f"• *#{it['id']}*: {it.get('comercio')} | {it.get('fecha')} | ${float(it.get('total', 0)):,.0f} (`/ver {it['id']}`)")
        lines.append("\nUsa `/ver <n>` para descargar cualquiera de ellas.")
        bot.send_message(chat_id, "\n".join(lines))
        return

    # Si estamos en modo de corrección interactiva
    editing_id = state.data.get("editing_id")
    if editing_id and editing_id in state.data["pending"]:
        pending = state.data["pending"][editing_id]
        data = pending["data"]
        extracted_type = pending["tipo"]

        lines = text.split("\n")
        updated_fields = []
        for line in lines:
            line = line.strip()
            if ":" in line:
                k, v = line.split(":", 1)
            elif " " in line:
                k, v = line.split(" ", 1)
            else:
                continue
            k = k.strip().lower()
            v = v.strip()

            if "total" in k or "monto" in k:
                # Quitar puntos o simbolos
                clean_num = re.sub(r"[^\d.]", "", v)
                if clean_num:
                    val = float(clean_num)
                    if extracted_type == "alimentacion":
                        data["total"] = val
                    else:
                        data["monto"] = val
                    updated_fields.append(f"total={val}")
            elif "comercio" in k:
                data["comercio"] = v
                updated_fields.append(f"comercio={v}")
            elif "cat" in k:
                data["categoria"] = v
                updated_fields.append(f"categoria={v}")
            elif "fecha" in k:
                data["fecha"] = v
                try:
                    dt = datetime.strptime(v, "%Y-%m-%d")
                    data["anio"] = str(dt.year)
                    data["mes"] = dt.month
                    data["periodo"] = f"{dt.year:04d}-{dt.month:02d}"
                except Exception:
                    pass
                updated_fields.append(f"fecha={v}")
            elif "medio" in k:
                data["medio_pago"] = v
                updated_fields.append(f"medio_pago={v}")
            elif "nota" in k:
                data["nota"] = sanitize_text(v)
                updated_fields.append("nota")

        if updated_fields:
            state.data["editing_id"] = None
            math_ok, math_msg = check_math(extracted_type, data)
            dup_found, dup_msg = check_duplicates(extracted_type, data)
            pending["math_msg"] = math_msg
            pending["dup_msg"] = dup_msg
            state.save()

            preview_text, reply_markup = format_preview(editing_id, extracted_type, data, math_msg, dup_msg)
            bot.send_message(chat_id, f"✅ *Campos actualizados ({', '.join(updated_fields)}):*\n\n{preview_text}", reply_markup=reply_markup)
            return

    # Mensaje normal no reconocido
    bot.send_message(chat_id, "ℹ️ Envíame una foto o PDF de una factura para registrarla, o usa `/resumen` para ver el balance mensual.")


def main():
    logger.info("Iniciando @Facundo1bot daemon...")
    logger.info(f"Escuchando únicamente mensajes de chat_id={ALLOWED_CHAT_ID}")

    offset = state.data.get("last_update_id", 0) + 1

    while True:
        try:
            res = bot.get_updates(offset=offset, timeout=25)
            if not res.get("ok"):
                time.sleep(3)
                continue

            updates = res.get("result", [])
            for u in updates:
                update_id = u["update_id"]
                offset = max(offset, update_id + 1)
                state.data["last_update_id"] = update_id

                if "message" in u:
                    msg = u["message"]
                    from_id = msg.get("from", {}).get("id")
                    if from_id != ALLOWED_CHAT_ID:
                        logger.warning(f"Mensaje ignorado de usuario no autorizado: {from_id}")
                        continue

                    if "photo" in msg or "document" in msg:
                        handle_document_or_photo(msg)
                    elif "text" in msg:
                        handle_text_message(msg)

                elif "callback_query" in u:
                    handle_callback_query(u["callback_query"])

                state.save()

        except KeyboardInterrupt:
            logger.info("Deteniendo bot...")
            break
        except Exception as e:
            logger.error(f"Error inesperado en ciclo principal: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
