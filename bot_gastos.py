import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import pandas as pd
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BACKUP_CHAT_ID = os.getenv("BACKUP_CHAT_ID")

if not TELEGRAM_TOKEN:
    raise ValueError("Falta configurar TELEGRAM_TOKEN")

DB_FILE = "gastos.db"
EXPORT_FILE = "gastos_export.xlsx"

MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
}

# =========================
# DATABASE
# =========================
def init_db() -> None:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gastos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            anio INTEGER NOT NULL,
            n_mes INTEGER NOT NULL,
            mes TEXT NOT NULL,
            item TEXT NOT NULL,
            valor INTEGER NOT NULL,
            categoria TEXT NOT NULL,
            usuario_id INTEGER,
            usuario_nombre TEXT
        )
    """)

    # Por si ya tenías una tabla vieja sin usuario
    columnas = [row[1] for row in cur.execute("PRAGMA table_info(gastos)").fetchall()]
    if "usuario_id" not in columnas:
        cur.execute("ALTER TABLE gastos ADD COLUMN usuario_id INTEGER")
    if "usuario_nombre" not in columnas:
        cur.execute("ALTER TABLE gastos ADD COLUMN usuario_nombre TEXT")

    conn.commit()
    conn.close()


def insertar_gasto(item: str, valor: int, categoria: str, usuario_id: int, usuario_nombre: str) -> None:
    now = datetime.now()
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO gastos
        (fecha, anio, n_mes, mes, item, valor, categoria, usuario_id, usuario_nombre)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now.strftime("%Y-%m-%d %H:%M:%S"),
            now.year,
            now.month,
            MESES_ES[now.month],
            item,
            valor,
            categoria,
            usuario_id,
            usuario_nombre,
        ),
    )
    conn.commit()
    conn.close()


def consultar_df(query: str, params: tuple = ()) -> pd.DataFrame:
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df


# =========================
# PARSER
# =========================
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


# =========================
# EXPORT
# =========================
def generar_excel() -> Path:
    df = consultar_df("""
        SELECT
            fecha AS Fecha,
            anio AS Año,
            n_mes AS N_mes,
            mes AS Mes,
            item AS Item,
            valor AS Valor,
            categoria AS Categoria,
            usuario_nombre AS Usuario
        FROM gastos
        ORDER BY Fecha
    """)

    export_path = Path(EXPORT_FILE)

    with pd.ExcelWriter(export_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Consolidado", index=False)

        if not df.empty:
            resumen = df.groupby(["Año", "N_mes", "Mes", "Categoria"], as_index=False)["Valor"].sum()
            resumen.to_excel(writer, sheet_name="Resumen", index=False)

    return export_path


# =========================
# TELEGRAM COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola 👋\n\n"
        "Envíame gastos así:\n"
        "• Pago arriendo $1.322.069\n"
        "• Vacuna Apolo $60\n"
        "• Dólar $48\n"
        "• Animals Apolo 33\n\n"
        "Comandos:\n"
        "/id - ver tu chat_id\n"
        "/mes - total del mes actual\n"
        "/hoy - gastos de hoy\n"
        "/ultimos - últimos 10 gastos\n"
        "/buscar apolo - buscar por texto\n"
        "/categoria mascotas - total por categoría\n"
        "/exportar - generar Excel"
    )


async def mi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu chat_id es: {update.effective_chat.id}")


async def registrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    valor = normalizar_valor(texto)

    if valor is None:
        await update.message.reply_text("No encontré el valor. Ejemplo: Pago arriendo $1322069")
        return

    item = extraer_item(texto)
    categoria = categorizar(item)

    usuario_id = update.effective_user.id
    usuario_nombre = update.effective_user.full_name

    insertar_gasto(item, valor, categoria, usuario_id, usuario_nombre)

    await update.message.reply_text(
        "Registrado ✅\n"
        f"Item: {item}\n"
        f"Valor: {formato_pesos(valor)}\n"
        f"Categoría: {categoria}\n"
        f"Usuario: {usuario_nombre}"
    )


async def mes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    df = consultar_df(
        """
        SELECT categoria, SUM(valor) AS total
        FROM gastos
        WHERE anio=? AND n_mes=?
        GROUP BY categoria
        ORDER BY total DESC
        """,
        (now.year, now.month),
    )

    if df.empty:
        await update.message.reply_text("No hay gastos registrados este mes.")
        return

    total = int(df["total"].sum())
    detalle = "\n".join(
        [f"{r.categoria}: {formato_pesos(int(r.total))}" for r in df.itertuples()]
    )

    await update.message.reply_text(
        f"Resumen {MESES_ES[now.month]} {now.year}\n\n"
        f"Total: {formato_pesos(total)}\n\n{detalle}"
    )


async def hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d")
    df = consultar_df(
        """
        SELECT fecha, item, valor, categoria, usuario_nombre
        FROM gastos
        WHERE substr(fecha,1,10)=?
        ORDER BY id DESC
        """,
        (today,),
    )

    if df.empty:
        await update.message.reply_text("No hay gastos registrados hoy.")
        return

    total = int(df["valor"].sum())
    lineas = [
        f"{r.item} - {formato_pesos(int(r.valor))} ({r.categoria})"
        for r in df.itertuples()
    ]

    await update.message.reply_text(
        "Gastos de hoy:\n\n"
        + "\n".join(lineas)
        + f"\n\nTotal: {formato_pesos(total)}"
    )


async def ultimos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = consultar_df("""
        SELECT fecha, item, valor, categoria, usuario_nombre
        FROM gastos
        ORDER BY id DESC
        LIMIT 10
    """)

    if df.empty:
        await update.message.reply_text("Todavía no hay gastos registrados.")
        return

    lineas = [
        f"{r.fecha[:10]} | {r.item} - {formato_pesos(int(r.valor))} ({r.categoria})"
        for r in df.itertuples()
    ]

    await update.message.reply_text("Últimos gastos:\n\n" + "\n".join(lineas))


async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    termino = " ".join(context.args).strip()

    if not termino:
        await update.message.reply_text("Usa: /buscar apolo")
        return

    df = consultar_df(
        """
        SELECT fecha, item, valor, categoria
        FROM gastos
        WHERE lower(item) LIKE lower(?)
        ORDER BY id DESC
        LIMIT 20
        """,
        (f"%{termino}%",),
    )

    if df.empty:
        await update.message.reply_text(f"No encontré gastos con: {termino}")
        return

    total = int(df["valor"].sum())
    lineas = [
        f"{r.fecha[:10]} | {r.item} - {formato_pesos(int(r.valor))} ({r.categoria})"
        for r in df.itertuples()
    ]

    await update.message.reply_text(
        "Resultados:\n\n"
        + "\n".join(lineas)
        + f"\n\nTotal: {formato_pesos(total)}"
    )


async def categoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = " ".join(context.args).strip()

    if not cat:
        await update.message.reply_text("Usa: /categoria mascotas")
        return

    df = consultar_df(
        """
        SELECT fecha, item, valor, categoria
        FROM gastos
        WHERE lower(categoria)=lower(?)
        ORDER BY id DESC
        """,
        (cat,),
    )

    if df.empty:
        await update.message.reply_text(f"No hay gastos en categoría: {cat}")
        return

    total = int(df["valor"].sum())
    lineas = [
        f"{r.fecha[:10]} | {r.item} - {formato_pesos(int(r.valor))}"
        for r in df.itertuples()
    ][:20]

    await update.message.reply_text(
        f"Categoría: {cat}\n\n"
        + "\n".join(lineas)
        + f"\n\nTotal: {formato_pesos(total)}"
    )


async def exportar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = consultar_df("SELECT * FROM gastos")

    if df.empty:
        await update.message.reply_text("No hay datos para exportar.")
        return

    export_path = generar_excel()

    await update.message.reply_document(
        document=export_path.open("rb"),
        filename=EXPORT_FILE,
        caption="Exportación de gastos ✅"
    )


async def backup_semanal(context: ContextTypes.DEFAULT_TYPE):
    if not BACKUP_CHAT_ID:
        print("BACKUP_CHAT_ID no configurado. No se envía backup.")
        return

    df = consultar_df("SELECT * FROM gastos")
    if df.empty:
        print("No hay datos para backup.")
        return

    export_path = generar_excel()

    with export_path.open("rb") as file:
        await context.bot.send_document(
            chat_id=int(BACKUP_CHAT_ID),
            document=file,
            filename=EXPORT_FILE,
            caption="Backup semanal de gastos ✅"
        )


def main():
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", mi_id))
    app.add_handler(CommandHandler("mes", mes))
    app.add_handler(CommandHandler("hoy", hoy))
    app.add_handler(CommandHandler("ultimos", ultimos))
    app.add_handler(CommandHandler("buscar", buscar))
    app.add_handler(CommandHandler("categoria", categoria))
    app.add_handler(CommandHandler("exportar", exportar))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, registrar))

    # Backup semanal cada 7 días.
    # first=60 significa que envía el primer backup 60 segundos después de iniciar.
    app.job_queue.run_repeating(
        backup_semanal,
        interval=7 * 24 * 60 * 60,
        first=60
    )

    print("Bot corriendo...")
    app.run_polling()


if __name__ == "__main__":
    main()
