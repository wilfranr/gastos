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


# ---------------------------------------------------------------------------
# Recibos de suscripciones y compras personales (formato fijo, sin IA).
# Cada parser devuelve un dict de movimiento o None si el correo no encaja.
# ---------------------------------------------------------------------------
_MES_ABBR = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dic": 12}


def _money(txt: str) -> float:
    """'$ 20.700' / '20,700' / '60,200.00' / '3.500,00' -> float. El último separador es decimal salvo que haya 3 dígitos tras él."""
    t = re.sub(r"[^\d.,]", "", txt).rstrip(".,")
    if "." in t and "," in t:
        dec = "." if t.rfind(".") > t.rfind(",") else ","
        t = t.replace("," if dec == "." else ".", "").replace(dec, ".")
    elif "." in t or "," in t:
        sep = "." if "." in t else ","
        if len(t.split(sep)[-1]) == 3:      # miles
            t = t.replace(sep, "")
        else:
            t = t.replace(sep, ".")
    return float(t)


def _when24(y, mo, d, hh=12, mm=0) -> str:
    from datetime import datetime
    return datetime(int(y), int(mo), int(d), int(hh), int(mm)).isoformat()


def _mes(nombre: str) -> int:
    n = nombre.lower().rstrip(".")
    return _MESES.get(n) or _MES_ABBR[n[:4] if n[:4] == "sept" else n[:3]]


def parse_virgin_renovacion(subject: str, text: str, when=None):
    m = re.search(r"Cobramos \$\s*([\d.,]+)\s*COP el (\d{1,2}) de ([a-záéíóú]+) de (\d{4}) a las (\d{1,2}):(\d{2})", text, re.I)
    if not m:
        return None
    monto, d, mes, y, hh, mm = m.groups()
    prox = re.search(r"Pr[oó]ximo cobro\s+(\d{1,2}) de ([a-záéíóú]+) de (\d{4})", text, re.I)
    per = re.search(r"Periodicidad cada (mes|a[ñn]o|semana)", text, re.I)
    plan = re.search(r"Plan \$\s*([\d.,]+)\s*(Mensual|Anual)", text, re.I)
    orden = re.search(r"No\. de orden\s+([0-9a-f-]{36})", text, re.I)
    rec = {"mes": "mensual", "semana": "semanal"}.get((per.group(1).lower() if per else "mes"), "anual")
    nota = "Renovación del plan" + (f" ({plan.group(2).lower()} ${_money(plan.group(1)):,.0f})" if plan else "")
    return {"tipo": "gasto", "monto": _money(monto), "contraparte": "Virgin Mobile", "categoria": "Suscripciones",
            "medio": "", "nota": nota, "ocurrido": _when24(y, _mes(mes), d, hh, mm), "recurrencia": rec,
            "proximo": _when24(prox.group(3), _mes(prox.group(2)), prox.group(1))[:10] if prox else None,
            "ref": f"virgin:{orden.group(1)}" if orden else None}


def parse_wompi_virgin(subject: str, text: str, when=None):
    if "virgin" not in subject.lower():
        return None
    m = re.search(r"COP\s*\$\s*([\d.,]+)", text)
    f = re.search(r"Fecha y Hora\s+(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})", text)
    ref = re.search(r"Referencia\s+([0-9a-f-]{36})", text, re.I)
    met = re.search(r"M[eé]todo de pago\s+([A-Za-zÁÉÍÓÚáéíóú]+)", text)
    if not (m and f):
        return None
    d, mo, y, hh, mm = f.groups()
    return {"tipo": "gasto", "monto": _money(m.group(1)), "contraparte": "Virgin Mobile", "categoria": "Suscripciones",
            "medio": met.group(1) if met else "", "nota": "Pago de plan (Wompi)", "ocurrido": _when24(y, mo, d, hh, mm),
            "recurrencia": "mensual", "proximo": None, "ref": f"wompi:{ref.group(1)}" if ref else None}


def parse_google_play(subject: str, text: str, when=None):
    ped = re.search(r"N[uú]mero de pedido:\s*(\S+)", text)
    f = re.search(r"Fecha del pedido:\s*(\d{1,2})\s+([a-záéíóú]{3,4})\.?\s+(\d{4})\s+(\d{1,2}):(\d{2}):\d{2}\s*([ap])\.?\s*m", text, re.I)
    tot = re.search(r"Total\s*:\s*\$\s*([\d.,]+)", text)
    if not (ped and f and tot) or re.search(r"\bUSD\b|US\$|\bEUR\b", text):
        return None
    d, mes, y, hh, mm, ap = f.groups()
    h = int(hh) % 12 + (12 if ap.lower() == "p" else 0)
    art = re.search(r"Art[ií]culo\s+Precio\s+(.+?)\s+\$\s*[\d.,]+", text)
    suscripcion = bool(re.search(r"renovaci[oó]n autom[aá]tica|se renov[oó] tu suscripci[oó]n", text, re.I))
    rec = None
    if suscripcion:
        rec = "anual" if re.search(r"por a[ñn]o", text, re.I) else "mensual"
    return {"tipo": "gasto", "monto": _money(tot.group(1)), "contraparte": "Google Play",
            "categoria": "Suscripciones" if suscripcion else "Ocio y salidas", "medio": "Tarjeta",
            "nota": (art.group(1).strip() if art else "Compra en Google Play")[:150],
            "ocurrido": _when24(y, _mes(mes), d, h, mm), "recurrencia": rec, "proximo": None, "ref": f"gplay:{ped.group(1)}"}


def parse_factura_documenteme(subject: str, text: str, when=None):
    if "Tipo documento: Factura" not in text:
        return None
    val = re.search(r"Valor obligaci[oó]n:\s*([\d.,]+)", text)
    doc = re.search(r"Documento:\s*(\S+)", text)
    fec = re.match(r"\s*(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})", text)
    emisor = re.search(r"Nombre facturador:\s*(.+?)\s+(?:Sucursal:|Un nuevo)", text)
    if not (val and fec):
        return None
    nombre = (emisor.group(1) if emisor else "Comercio").strip()
    es_cine = "cinemark" in nombre.lower()
    d, mo, y, hh, mm = fec.groups()
    return {"tipo": "gasto", "monto": _money(val.group(1)),
            "contraparte": "Cinemark" if es_cine else re.sub(r"\s+(S\.?A\.?S?\.?|LTDA\.?)$", "", nombre.title(), flags=re.I),
            "categoria": "Ocio y salidas" if es_cine else None, "medio": "", "nota": "Factura electrónica",
            "ocurrido": _when24(y, mo, d, hh, mm), "recurrencia": None, "proximo": None,
            "ref": f"doc:{doc.group(1)}" if doc else None}


RECIBO_RULES = [
    {"folder": "[Gmail]/Todos", "from": "virginmobile", "imap_subject": "Virgin", "subject_has": ["recibo de tu renovaci", "recibo de renovaci"], "parser": parse_virgin_renovacion},
    {"folder": "Pagos", "from": "wompi", "imap_subject": "Virgin", "subject_has": ["aprobada en virgin"], "parser": parse_wompi_virgin},
    {"folder": "[Gmail]/Todos", "from": "google", "imap_subject": "Google Play", "subject_has": ["recibo de tu pedido de google play"], "parser": parse_google_play},
    {"folder": "Facturas", "from": "documenteme", "imap_subject": None, "subject_has": [], "parser": parse_factura_documenteme},
]


USD_COP = 4000.0   # ESTIMACIÓN para cobros en dólares (no es la TRM del día); se aclara en la nota del movimiento
_NEQUI_OMITIR = ("tpaga", "compensar", "bancolombia", "gou payments", "tecnipagos", "enel", "vanti", "claro", "dian", "acueducto")
_NEQUI_CATEGORIA: dict = {}   # las categorías por comercio se aprenden con reglas en la base privada, no aquí


def parse_arq_renovacion(subject: str, text: str, when=None):
    """ARQ Premium: 'Tu plan Mensual ha sido renovado' (cobra en USDc)."""
    from datetime import timedelta, timezone
    m = re.search(r"([\d.,]+)\s*USDc", text)
    if not (m and when):
        return None
    usd = _money(m.group(1))
    local = when.astimezone(timezone(timedelta(hours=-5)))
    return {"tipo": "gasto", "monto": round(usd * USD_COP), "contraparte": "ARQ Premium", "categoria": "Suscripciones",
            "medio": "ARQ", "nota": f"Plan mensual: {usd:.2f} USDc (convertido a ${USD_COP:,.0f} por dólar, estimado)",
            "ocurrido": local.replace(tzinfo=None, microsecond=0).isoformat(), "recurrencia": "mensual", "proximo": None,
            "ref": f"arq:{local.strftime('%Y%m%d%H%M')}"}


def parse_nequi_pago(subject: str, text: str, when=None):
    """Nequi '¡Pago exitoso!': pago a un comercio. Se omiten los recibos del hogar, que ya tienen su propio flujo."""
    m = re.search(r"Hiciste un pago en\s+(.+?)\s+por\s+\$\s*([\d.,]+)\s+Fecha:\s*El\s+(\d{1,2}) de ([a-záéíóú]+) de (\d{4})\s+Hora:\s*(\d{1,2}):(\d{2})\s*([ap])\.?\s*m", text, re.I)
    if not m:
        return None
    nombre, monto, d, mes, y, hh, mm, ap = m.groups()
    bajo = nombre.lower()
    if any(k in bajo for k in _NEQUI_OMITIR):
        return {}   # omitido a propósito (recibo del hogar)
    cus = re.search(r"CUS:\s*(\d+)", text)
    return {"tipo": "gasto", "monto": _money(monto), "contraparte": nombre.strip().title()[:120],
            "categoria": next((v for k, v in _NEQUI_CATEGORIA.items() if k in bajo), None), "medio": "Nequi",
            "nota": "Pago desde Nequi", "ocurrido": _when(d, mes, y, hh, mm, ap), "recurrencia": None, "proximo": None,
            "ref": f"nequi-pago:{cus.group(1)}" if cus else None}


RECIBO_RULES += [
    {"folder": "[Gmail]/Todos", "from": "arqfinance", "imap_subject": "plan", "subject_has": ["ha sido renovado"], "parser": parse_arq_renovacion},
    {"folder": "Pagos", "from": "somos@nequi.com.co", "imap_subject": None, "subject_has": ["pago exitoso"], "parser": parse_nequi_pago, "key_prefix": "pago:"},
]


def _msg_text(msg) -> str:
    t = _html_text(msg)
    if t:
        return t
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            return re.sub(r"\s+", " ", part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")).strip()
    return ""


def fetch_recibos(user: str, password: str, seen: set, limit: int = 40):
    """Una sesión IMAP; hasta `limit` correos nuevos. Devuelve (movimientos, claves_omitidas, quedan_mas)."""
    movs, skipped, more = [], [], False
    conn = imaplib.IMAP4_SSL(IMAP_HOST, timeout=60)
    try:
        conn.login(user, password)
        for rule in RECIBO_RULES:
            if conn.select(f'"{rule["folder"]}"', readonly=True)[0] != "OK":
                continue
            crit = ["SINCE", "01-Jan-2026", "FROM", rule["from"]] + (["SUBJECT", f'"{rule["imap_subject"]}"'] if rule.get("imap_subject") else [])
            _, data = conn.uid("search", None, *crit)
            for uid in sorted((int(u) for u in data[0].split()), reverse=True):
                key = f'{rule.get("key_prefix", "")}{rule["folder"]}:{uid}'
                if key in seen:
                    continue
                if len(movs) + len(skipped) >= limit:
                    more = True
                    break
                _, full = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
                msg = email.message_from_bytes(full[0][1])
                subj = _h(msg["Subject"]).lower()
                if rule["subject_has"] and not any(w in subj for w in rule["subject_has"]):
                    skipped.append(key)
                    continue
                try:
                    when = parsedate_to_datetime(msg["Date"])
                    if (when.year, when.month, when.day) < MIN_DATE:
                        skipped.append(key)
                        continue
                except Exception:
                    pass
                try:
                    when_dt = parsedate_to_datetime(msg["Date"])
                except Exception:
                    when_dt = None
                mov = rule["parser"](_h(msg["Subject"]), _msg_text(msg), when_dt)
                if mov == {}:
                    skipped.append(key)
                    continue
                if not mov:
                    logger.warning(f"Recibo {key}: no se pudo interpretar «{subj[:50]}»")
                    skipped.append(key)
                    continue
                mov.update({"key": key, "origen": "correo"})
                mov["ref"] = mov.get("ref") or (msg["Message-ID"] or key).strip()
                movs.append(mov)
            if more:
                break
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return movs, skipped, more
