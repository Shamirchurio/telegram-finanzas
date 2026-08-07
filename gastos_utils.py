"""Funciones puras (sin I/O) compartidas entre el bot local (SQLite/polling)
y el despliegue en Vercel (Turso/webhook).

Mantener la lógica de parseo y categorización en un solo lugar evita que
las dos versiones del bot se desincronicen.
"""

import re
from datetime import datetime

MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
}


def extraer_fecha(texto: str) -> tuple[datetime, str]:
    texto = texto.strip()

    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+", texto)
    if match:
        dia, mes, anio = map(int, match.groups())
        fecha = datetime(anio, mes, dia)
        texto_sin_fecha = texto[match.end():].strip()
        return fecha, texto_sin_fecha

    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})\s+", texto)
    if match:
        anio, mes, dia = map(int, match.groups())
        fecha = datetime(anio, mes, dia)
        texto_sin_fecha = texto[match.end():].strip()
        return fecha, texto_sin_fecha

    return datetime.now(), texto


def parsear_fecha(texto: str) -> datetime | None:
    texto = texto.strip()

    for formato in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto, formato)
        except ValueError:
            continue

    return None


def restar_meses(fecha: datetime, n: int) -> datetime:
    total_meses = fecha.year * 12 + (fecha.month - 1) - n
    anio = total_meses // 12
    mes = total_meses % 12 + 1
    return datetime(anio, mes, 1)


def normalizar_valor(texto: str) -> int | None:
    matches = re.findall(r"[\$]?\s*([0-9][0-9\.,]*)", texto)
    if not matches:
        return None

    raw = matches[-1].replace(".", "").replace(",", "").strip()
    if not raw.isdigit():
        return None

    return int(raw)


def extraer_item(texto: str) -> str:
    item = re.sub(r"[\$]?\s*[0-9][0-9\.,]*\s*$", "", texto).strip(" -:")
    return item if item else "Sin descripción"


def categorizar(item: str) -> str:
    t = item.lower()

    if "arriendo" in t or "apto" in t or "apartamento" in t:
        return "Vivienda"
    if "claro" in t or "luz" in t or "gas" in t or "agua" in t or "internet" in t:
        return "Servicios"
    if "apolo" in t or "vacuna" in t or "veter" in t or "animals" in t:
        return "Mascotas"
    if "dolar" in t or "dólar" in t or "usd" in t:
        return "Finanzas"
    if "mercado" in t or "almuerzo" in t or "comida" in t or "desayuno" in t or "cena" in t:
        return "Alimentación"
    if "uber" in t or "taxi" in t or "gasolina" in t or "peaje" in t or "bus" in t:
        return "Transporte"
    if "netflix" in t or "spotify" in t or "cine" in t:
        return "Entretenimiento"

    return "Otros"


def formato_pesos(valor: int) -> str:
    return "$" + f"{valor:,}".replace(",", ".")
