"""Bot de gastos: FastAPI corriendo en Vercel, en modo webhook (no polling) +
Turso (no SQLite en disco) + backup semanal disparado por Vercel Cron
(no job_queue, que requiere un proceso persistente).

Reutiliza las funciones puras de gastos_utils.py y la capa de datos async de
db_turso.py.
"""

import asyncio
import logging
import os
from datetime import datetime
from functools import wraps

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db_turso
from gastos_utils import (
    MESES_ES,
    categorizar,
    extraer_fecha,
    extraer_item,
    formato_pesos,
    normalizar_valor,
    parsear_fecha,
    restar_meses,
)

# httpx (usado internamente por python-telegram-bot) loguea cada request en
# INFO, incluyendo la URL completa con el token del bot — eso terminaría
# expuesto en texto plano en los logs de Vercel. Lo silenciamos.
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
CRON_SECRET = os.getenv("CRON_SECRET")
BACKUP_CHAT_IDS = [
    int(x) for x in os.getenv("BACKUP_CHAT_ID", "").split(",") if x.strip()
]
AUTHORIZED_USERS = [
    int(x) for x in os.getenv("AUTHORIZED_USERS", "").split(",") if x.strip()
]

app = FastAPI()

_application = None
_init_lock = asyncio.Lock()


def requiere_autorizacion(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in AUTHORIZED_USERS:
            await update.message.reply_text(
                "⛔ No estás autorizado para usar este bot.\n"
                f"Tu chat_id es: {update.effective_user.id}"
            )
            return
        return await func(update, context)

    return wrapper


@requiere_autorizacion
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola 👋\n\n"
        "Envíame gastos así:\n"
        "• Pago arriendo $1.322.069\n"
        "• 30/06/2026 Gas $20.460\n"
        "• 2026-06-30 Pago claro $103.995\n\n"
        "Comandos:\n"
        "/id - ver tu chat_id\n"
        "/mes - total del mes actual\n"
        "/meses 3 - resumen de los últimos 3 meses\n"
        "/hoy - gastos de hoy\n"
        "/ultimos - últimos 10 gastos\n"
        "/buscar apolo - buscar por texto\n"
        "/categoria servicios - total por categoría\n"
        "/editar 12 valor 20000 - editar un gasto por id\n"
        "/eliminar 12 - eliminar un gasto por id\n"
        "/exportar - generar Excel"
    )


@requiere_autorizacion
async def mi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu chat_id es: {update.effective_chat.id}")


@requiere_autorizacion
async def registrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_original = update.message.text.strip()

    try:
        fecha_movimiento, texto = extraer_fecha(texto_original)
    except ValueError:
        await update.message.reply_text("Fecha inválida. Usa formato 30/06/2026 o 2026-06-30.")
        return

    valor = normalizar_valor(texto)

    if valor is None:
        await update.message.reply_text("No encontré el valor. Ejemplo: 30/06/2026 Gas $20460")
        return

    item = extraer_item(texto)
    categoria = categorizar(item)

    usuario_id = update.effective_user.id
    usuario_nombre = update.effective_user.full_name

    await db_turso.insertar_gasto(item, valor, categoria, usuario_id, usuario_nombre, fecha_movimiento)

    await update.message.reply_text(
        "Registrado ✅\n"
        f"Fecha: {fecha_movimiento.strftime('%d/%m/%Y')}\n"
        f"Item: {item}\n"
        f"Valor: {formato_pesos(valor)}\n"
        f"Categoría: {categoria}\n"
        f"Usuario: {usuario_nombre}"
    )


@requiere_autorizacion
async def mes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    df = await db_turso.consultar_df(
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
    detalle = "\n".join([f"{r.categoria}: {formato_pesos(int(r.total))}" for r in df.itertuples()])

    await update.message.reply_text(
        f"Resumen {MESES_ES[now.month]} {now.year}\n\n"
        f"Total: {formato_pesos(total)}\n\n{detalle}"
    )


@requiere_autorizacion
async def meses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    n = 3

    if args:
        if not args[0].isdigit() or int(args[0]) < 1:
            await update.message.reply_text("Usa: /meses 3")
            return
        n = int(args[0])

    desde = restar_meses(datetime.now(), n - 1)

    df = await db_turso.consultar_df(
        """
        SELECT anio, n_mes, mes, categoria, valor
        FROM gastos
        WHERE fecha >= ?
        """,
        (desde.strftime("%Y-%m-%d 00:00:00"),),
    )

    if df.empty:
        await update.message.reply_text(f"No hay gastos registrados en los últimos {n} meses.")
        return

    total = int(df["valor"].sum())

    resumen_mes = (
        df.groupby(["anio", "n_mes", "mes"], as_index=False)["valor"]
        .sum()
        .sort_values(["anio", "n_mes"])
    )
    lineas_mes = [
        f"{r.mes} {r.anio}: {formato_pesos(int(r.valor))}"
        for r in resumen_mes.itertuples()
    ]

    resumen_cat = (
        df.groupby("categoria", as_index=False)["valor"]
        .sum()
        .sort_values("valor", ascending=False)
    )
    lineas_cat = [
        f"{r.categoria}: {formato_pesos(int(r.valor))}"
        for r in resumen_cat.itertuples()
    ]

    await update.message.reply_text(
        f"Resumen de los últimos {n} meses\n\n"
        f"Por mes:\n" + "\n".join(lineas_mes) + "\n\n"
        f"Por categoría:\n" + "\n".join(lineas_cat) + "\n\n"
        f"Total: {formato_pesos(total)}"
    )


@requiere_autorizacion
async def hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d")
    df = await db_turso.consultar_df(
        """
        SELECT id, fecha, item, valor, categoria, usuario_nombre
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
        f"#{r.id} | {r.item} - {formato_pesos(int(r.valor))} ({r.categoria})"
        for r in df.itertuples()
    ]

    await update.message.reply_text(
        "Gastos de hoy:\n\n"
        + "\n".join(lineas)
        + f"\n\nTotal: {formato_pesos(total)}"
    )


@requiere_autorizacion
async def ultimos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = await db_turso.consultar_df("""
        SELECT id, fecha, item, valor, categoria, usuario_nombre
        FROM gastos
        ORDER BY id DESC
        LIMIT 10
    """)

    if df.empty:
        await update.message.reply_text("Todavía no hay gastos registrados.")
        return

    lineas = [
        f"#{r.id} | {r.fecha[:10]} | {r.item} - {formato_pesos(int(r.valor))} ({r.categoria})"
        for r in df.itertuples()
    ]

    await update.message.reply_text("Últimos gastos:\n\n" + "\n".join(lineas))


@requiere_autorizacion
async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    termino = " ".join(context.args).strip()

    if not termino:
        await update.message.reply_text("Usa: /buscar apolo")
        return

    df = await db_turso.consultar_df(
        """
        SELECT id, fecha, item, valor, categoria
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
        f"#{r.id} | {r.fecha[:10]} | {r.item} - {formato_pesos(int(r.valor))} ({r.categoria})"
        for r in df.itertuples()
    ]

    await update.message.reply_text(
        "Resultados:\n\n"
        + "\n".join(lineas)
        + f"\n\nTotal: {formato_pesos(total)}"
    )


@requiere_autorizacion
async def categoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = " ".join(context.args).strip()

    if not cat:
        await update.message.reply_text("Usa: /categoria servicios")
        return

    df = await db_turso.consultar_df(
        """
        SELECT id, fecha, item, valor, categoria
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
        f"#{r.id} | {r.fecha[:10]} | {r.item} - {formato_pesos(int(r.valor))}"
        for r in df.itertuples()
    ][:20]

    await update.message.reply_text(
        f"Categoría: {cat}\n\n"
        + "\n".join(lineas)
        + f"\n\nTotal: {formato_pesos(total)}"
    )


@requiere_autorizacion
async def eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Usa: /eliminar <id>\n"
            "El id lo ves con /ultimos, /hoy, /buscar o /categoria."
        )
        return

    id_gasto = int(args[0])
    gasto = await db_turso.obtener_gasto(id_gasto)

    if gasto is None:
        await update.message.reply_text(f"No existe un gasto con id #{id_gasto}.")
        return

    await db_turso.eliminar_gasto(id_gasto)

    await update.message.reply_text(
        f"Eliminado ✅\n"
        f"#{gasto['id']} | {gasto['item']} - {formato_pesos(gasto['valor'])} ({gasto['categoria']})"
    )


@requiere_autorizacion
async def editar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    if len(args) < 3 or not args[0].isdigit():
        await update.message.reply_text(
            "Usa: /editar <id> <campo> <valor_nuevo>\n"
            "Campos: item, valor, categoria, fecha\n\n"
            "Ejemplos:\n"
            "/editar 12 valor 20000\n"
            "/editar 12 item Pago luz\n"
            "/editar 12 categoria Servicios\n"
            "/editar 12 fecha 2026-07-01"
        )
        return

    id_gasto = int(args[0])
    campo = args[1].lower()
    valor_nuevo = " ".join(args[2:]).strip()

    gasto = await db_turso.obtener_gasto(id_gasto)
    if gasto is None:
        await update.message.reply_text(f"No existe un gasto con id #{id_gasto}.")
        return

    if campo == "item":
        nueva_categoria = categorizar(valor_nuevo)
        await db_turso.actualizar_item(id_gasto, valor_nuevo, nueva_categoria)
        await update.message.reply_text(
            f"Actualizado ✅\n#{id_gasto} | Item: {valor_nuevo} (Categoría: {nueva_categoria})"
        )

    elif campo == "valor":
        nuevo_valor = normalizar_valor(valor_nuevo)
        if nuevo_valor is None:
            await update.message.reply_text("Valor inválido. Ejemplo: /editar 12 valor 20000")
            return
        await db_turso.actualizar_valor(id_gasto, nuevo_valor)
        await update.message.reply_text(
            f"Actualizado ✅\n#{id_gasto} | Valor: {formato_pesos(nuevo_valor)}"
        )

    elif campo == "categoria":
        await db_turso.actualizar_categoria(id_gasto, valor_nuevo)
        await update.message.reply_text(
            f"Actualizado ✅\n#{id_gasto} | Categoría: {valor_nuevo}"
        )

    elif campo == "fecha":
        fecha_nueva = parsear_fecha(valor_nuevo)
        if fecha_nueva is None:
            await update.message.reply_text(
                "Fecha inválida. Usa formato 30/06/2026 o 2026-06-30."
            )
            return
        await db_turso.actualizar_fecha(id_gasto, fecha_nueva)
        await update.message.reply_text(
            f"Actualizado ✅\n#{id_gasto} | Fecha: {fecha_nueva.strftime('%d/%m/%Y')}"
        )

    else:
        await update.message.reply_text("Campo inválido. Usa: item, valor, categoria o fecha")


@requiere_autorizacion
async def exportar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = await db_turso.consultar_df("SELECT * FROM gastos")

    if df.empty:
        await update.message.reply_text("No hay datos para exportar.")
        return

    export_path = await db_turso.generar_excel()

    await update.message.reply_document(
        document=export_path.open("rb"),
        filename=db_turso.EXPORT_FILE,
        caption="Exportación de gastos ✅"
    )


async def _build_application():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta configurar TELEGRAM_TOKEN")
    if not AUTHORIZED_USERS:
        raise RuntimeError("Falta configurar AUTHORIZED_USERS")

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(20)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("id", mi_id))
    application.add_handler(CommandHandler("mes", mes))
    application.add_handler(CommandHandler("meses", meses))
    application.add_handler(CommandHandler("hoy", hoy))
    application.add_handler(CommandHandler("ultimos", ultimos))
    application.add_handler(CommandHandler("buscar", buscar))
    application.add_handler(CommandHandler("categoria", categoria))
    application.add_handler(CommandHandler("editar", editar))
    application.add_handler(CommandHandler("eliminar", eliminar))
    application.add_handler(CommandHandler("exportar", exportar))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, registrar))

    await application.initialize()
    await db_turso.init_db()
    return application


async def _get_application():
    global _application
    if _application is None:
        async with _init_lock:
            if _application is None:
                _application = await _build_application()
    return _application


@app.get("/")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if TELEGRAM_WEBHOOK_SECRET and x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Secret inválido")

    application = await _get_application()
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return JSONResponse({"ok": True})


@app.get("/cron/backup")
async def cron_backup(authorization: str | None = Header(default=None)):
    if CRON_SECRET and authorization != f"Bearer {CRON_SECRET}":
        raise HTTPException(status_code=401, detail="No autorizado")

    if not BACKUP_CHAT_IDS:
        return {"status": "skipped", "reason": "BACKUP_CHAT_ID no configurado"}

    df = await db_turso.consultar_df("SELECT * FROM gastos")
    if df.empty:
        return {"status": "skipped", "reason": "sin datos"}

    export_path = await db_turso.generar_excel()

    application = await _get_application()
    for chat_id in BACKUP_CHAT_IDS:
        with export_path.open("rb") as file:
            await application.bot.send_document(
                chat_id=chat_id,
                document=file,
                filename=db_turso.EXPORT_FILE,
                caption="Backup semanal de gastos ✅",
            )

    return {"status": "ok", "chats": BACKUP_CHAT_IDS}
