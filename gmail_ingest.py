"""
Lectura de facturas desde Gmail (IMAP, solo lectura) para @Facundo1bot.

- Usa BODY.PEEK y select(readonly=True): nunca marca correos como leídos ni modifica el buzón.
- Solo mira las etiquetas y remitentes de RULES (lista blanca).
- Devuelve de a un candidato por llamada; el bot decide qué hacer con él.
"""

import html as _html
import imaplib
import email
import logging
import re
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path

logger = logging.getLogger("facundobot.gmail")

IMAP_HOST = "imap.gmail.com"

# El dashboard arranca en 2024; correos anteriores (p. ej. facturas Claro 2016-2018) se omiten.
MIN_DATE = (2024, 1, 1)

# Fase 1: servicios. Suscripciones y transferencias (Nequi, Wompi, Virgin, etc.) quedan para la fase 2.
RULES = [
    # Orden = prioridad: primero lo reciente; las facturas de Claro de 2018 al final.
    # Vanti (Facturas) fuera: es lo que se DEBE, no lo pagado, y su periodo va un mes desfasado
    # respecto al histórico (que etiqueta por mes de pago). Candidata a recordatorio de vencimiento.
    {"folder": "Pagos", "from": "achcolombia", "subject_has": "aprobada", "need": "body"},
    {"folder": "Pagos", "from": "claropay", "subject_has": "exitosamente", "need": "body"},
    # Nequi: solo pagos de recibos/facturas. Las transferencias "Enviaste/Recibiste plata por Bre-B" van en la fase 2.
    {"folder": "Pagos", "from": "somos@nequi.com.co", "subject_has": ["pago exitoso", "comprobante de pago"], "need": "body"},
    {"folder": "Pagos", "from": "notificaciones@nequi.com.co", "subject_has": ["pago exitoso de tu factura"], "need": "body"},
    # Cuota del apto (Bancolombia crédito hipotecario) pagada por Wompi; Virgin queda para la fase 2.
    {"folder": "Pagos", "from": "wompi", "subject_has": ["aprobada en bancolombia"], "need": "body"},
    {"folder": "Facturas", "from": "facturacion_clarofijo", "need": "pdf"},
]


def _h(value) -> str:
    return str(make_header(decode_header(value or "")))


def _find_pdf(msg):
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = (_h(part.get_filename()) if part.get_filename() else "").lower()
        if part.get_content_type() == "application/pdf" or name.endswith(".pdf"):
            payload = part.get_payload(decode=True)
            if payload:
                return payload
    return None


def _find_body(msg):
    """Devuelve (bytes, extensión) del cuerpo; prefiere HTML."""
    plain = None
    for part in msg.walk():
        if part.is_multipart() or part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if part.get_content_type() == "text/html":
            return payload, "html"
        if part.get_content_type() == "text/plain" and plain is None:
            plain = payload
    return (plain, "txt") if plain else (None, None)


def next_candidate(user: str, password: str, seen: set, out_dir: Path):
    """
    Busca el correo elegible más reciente que no esté en `seen`.
    Devuelve (candidato | None, claves_a_omitir). Cada clave es "carpeta:uid".
    El candidato trae: key, folder, sender, subject, date (str ISO), path (archivo guardado).
    """
    skipped = []
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = imaplib.IMAP4_SSL(IMAP_HOST, timeout=40)
    try:
        conn.login(user, password)
        for rule in RULES:
            typ, _ = conn.select(f'"{rule["folder"]}"', readonly=True)
            if typ != "OK":
                continue
            _, data = conn.uid("search", None, "FROM", rule["from"])
            uids = sorted((int(u) for u in data[0].split()), reverse=True)
            for uid in uids:
                key = f'{rule["folder"]}:{uid}'
                if key in seen:
                    continue

                _, hdr = conn.uid("fetch", str(uid), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                head = email.message_from_bytes(hdr[0][1])
                subject = _h(head["Subject"])
                try:
                    hd = parsedate_to_datetime(head["Date"])
                    if (hd.year, hd.month, hd.day) < MIN_DATE:
                        skipped.append(key)
                        continue
                except Exception:
                    pass
                wanted = rule.get("subject_has")
                if isinstance(wanted, str):
                    wanted = [wanted]
                if wanted and not any(w in subject.lower() for w in wanted):
                    skipped.append(key)
                    continue

                _, full = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
                msg = email.message_from_bytes(full[0][1])
                try:
                    when = parsedate_to_datetime(msg["Date"])
                except Exception:
                    when = None
                stamp = when.strftime("%Y%m%d") if when else "sinfecha"

                if rule["need"] == "pdf":
                    payload, ext = _find_pdf(msg), "pdf"
                else:
                    payload, ext = _find_body(msg)
                if not payload:
                    skipped.append(key)
                    continue

                path = out_dir / f'{stamp}_{rule["folder"]}_{uid}.{ext}'
                path.write_bytes(payload)
                return {
                    "key": key,
                    "folder": rule["folder"],
                    "sender": _h(head["From"]),
                    "subject": subject,
                    "date": when.isoformat() if when else "",
                    "path": str(path),
                }, skipped
        return None, skipped
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Nequi Bre-B (transferencias enviadas/recibidas): formato fijo, se lee sin IA.
# ---------------------------------------------------------------------------
_MESES = {m: i + 1 for i, m in enumerate(
    ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"])}
_FECHA = r"el\s+(\d{1,2})\s+de\s+([a-záéíóú]+)\s+de\s+(\d{4})\s+a\s+las?\s+(\d{1,2}):(\d{2})\s*([ap])\.?\s*m"
RE_ENVIO = re.compile(r"Enviaste de manera exitosa\s+([\d.,]+)\s+a la llave\s+(\S+)\s+de\s+(.+?)\s+" + _FECHA, re.I)
RE_RECIBO = re.compile(r"Recibiste\s+([\d.,]+)\s+de\s+(.+?)\s+" + _FECHA + r"\.?,?(?:\s*desde el banco\s+([^.]+?)\.)?", re.I)


def _parse_cop(txt: str) -> float:
    txt = txt.strip().rstrip(".,")
    if "," in txt:                      # 35.400,50
        txt = txt.replace(".", "").replace(",", ".")
    else:                               # 35.400
        txt = txt.replace(".", "")
    return float(txt)


def _when(d, mes, y, hh, mm, ap):
    from datetime import datetime
    h = int(hh) % 12 + (12 if ap.lower() == "p" else 0)
    return datetime(int(y), _MESES[mes.lower()], int(d), h, int(mm)).isoformat()


def _html_text(msg) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/html" and not part.get_filename():
            raw = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            raw = re.sub(r"(?is)<(style|script).*?</\1>", "", raw)
            return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()
    return ""


def parse_bre_b(subject: str, text: str):
    """Devuelve dict del movimiento o None. De la llave solo se conservan los 4 últimos dígitos."""
    sub = subject.lower()
    if "enviaste" in sub:
        m = RE_ENVIO.search(text)
        if not m:
            return None
        monto, llave, quien, d, mes, y, hh, mm, ap = m.groups()
        return {"tipo": "gasto", "monto": _parse_cop(monto), "contraparte": quien.strip(), "banco": "",
                "nota": f"Bre-B a llave ***{re.sub(r'\\D', '', llave)[-4:]}", "ocurrido": _when(d, mes, y, hh, mm, ap)}
    if "recibiste" in sub:
        m = RE_RECIBO.search(text)
        if not m:
            return None
        monto, quien, d, mes, y, hh, mm, ap, banco = m.groups()
        return {"tipo": "ingreso", "monto": _parse_cop(monto), "contraparte": quien.strip(), "banco": (banco or "").strip(),
                "nota": "Bre-B recibido", "ocurrido": _when(d, mes, y, hh, mm, ap)}
    return None


def fetch_bre_b(user: str, password: str, seen: set, limit: int = 40):
    """
    Lee hasta `limit` correos Bre-B nuevos con una sola sesión IMAP.
    Devuelve (movimientos, claves_omitidas, quedan_mas). Cada movimiento trae 'key' y 'ref' (Message-ID).
    """
    movs, skipped, pending_more = [], [], False
    conn = imaplib.IMAP4_SSL(IMAP_HOST, timeout=40)
    try:
        conn.login(user, password)
        if conn.select('"Pagos"', readonly=True)[0] != "OK":
            return movs, skipped, False
        _, data = conn.uid("search", None, "FROM", "notificaciones@nequi.com.co", "SUBJECT", "Bre-B")
        uids = sorted((int(u) for u in data[0].split()), reverse=True)
        for uid in uids:
            key = f"Pagos:{uid}"
            if key in seen:
                continue
            if len(movs) + len(skipped) >= limit:
                pending_more = True
                break
            _, full = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
            msg = email.message_from_bytes(full[0][1])
            try:
                when = parsedate_to_datetime(msg["Date"])
                if (when.year, when.month, when.day) < MIN_DATE:
                    skipped.append(key)
                    continue
            except Exception:
                pass
            mov = parse_bre_b(_h(msg["Subject"]), _html_text(msg))
            if not mov:
                logger.warning(f"Bre-B {key}: no se pudo interpretar el correo")
                skipped.append(key)
                continue
            mov.update({"key": key, "ref": (msg["Message-ID"] or key).strip(), "medio": "Nequi", "origen": "nequi"})
            movs.append(mov)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return movs, skipped, pending_more
