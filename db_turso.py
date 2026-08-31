"""Capa de datos async para el bot (api/index.py): contra una base de datos
Turso (libSQL) por HTTP, sin estado en disco — apto para funciones
serverless.

Requiere las variables de entorno TURSO_DATABASE_URL y TURSO_AUTH_TOKEN.
"""

import os
from datetime import datetime
from pathlib import Path

import libsql_client
import pandas as pd

from gastos_utils import MESES_ES

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

EXPORT_FILE = "gastos_export.xlsx"


def _client() -> libsql_client.Client:
    if not TURSO_URL or not TURSO_AUTH_TOKEN:
        raise RuntimeError(
            "Faltan TURSO_DATABASE_URL / TURSO_AUTH_TOKEN en las variables de entorno"
        )
    # Forzamos el transporte HTTP (en vez del wss:// por defecto que resulta de
    # una URL libsql://): más simple y evita bloqueos de WebSocket en redes
    # restrictivas (antivirus con inspección SSL, proxys, etc.).
    url = TURSO_URL.replace("libsql://", "https://", 1)
    return libsql_client.create_client(url=url, auth_token=TURSO_AUTH_TOKEN)


def _rows_to_dicts(rs: libsql_client.ResultSet) -> list[dict]:
    return [dict(zip(rs.columns, row)) for row in rs.rows]


async def init_db() -> None:
    async with _client() as client:
        await client.execute("""
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
                usuario_nombre TEXT,
                foto_file_id TEXT
            )
        """)

        rs = await client.execute("PRAGMA table_info(gastos)")
        columnas = [row[1] for row in rs.rows]
        if "usuario_id" not in columnas:
            await client.execute("ALTER TABLE gastos ADD COLUMN usuario_id INTEGER")
        if "usuario_nombre" not in columnas:
            await client.execute("ALTER TABLE gastos ADD COLUMN usuario_nombre TEXT")
        if "foto_file_id" not in columnas:
            await client.execute("ALTER TABLE gastos ADD COLUMN foto_file_id TEXT")

        await client.execute("""
            CREATE TABLE IF NOT EXISTS intentos_acceso (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT NOT NULL,
                usuario_id INTEGER NOT NULL,
                usuario_nombre TEXT,
                username TEXT,
                mensaje TEXT
            )
        """)


async def insertar_gasto(
    item: str,
    valor: int,
    categoria: str,
    usuario_id: int,
    usuario_nombre: str,
    fecha_movimiento: datetime,
    foto_file_id: str | None = None,
) -> None:
    async with _client() as client:
        await client.execute(
            """
            INSERT INTO gastos
            (fecha, anio, n_mes, mes, item, valor, categoria, usuario_id, usuario_nombre, foto_file_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                fecha_movimiento.strftime("%Y-%m-%d %H:%M:%S"),
                fecha_movimiento.year,
                fecha_movimiento.month,
                MESES_ES[fecha_movimiento.month],
                item,
                valor,
                categoria,
                usuario_id,
                usuario_nombre,
                foto_file_id,
            ],
        )


async def obtener_gasto(id_gasto: int) -> dict | None:
    async with _client() as client:
        rs = await client.execute(
            "SELECT id, fecha, item, valor, categoria, foto_file_id FROM gastos WHERE id=?",
            [id_gasto],
        )
        if not rs.rows:
            return None
        row = rs.rows[0]
        return {
            "id": row[0],
            "fecha": row[1],
            "item": row[2],
            "valor": row[3],
            "categoria": row[4],
            "foto_file_id": row[5],
        }


async def eliminar_gasto(id_gasto: int) -> bool:
    async with _client() as client:
        rs = await client.execute("DELETE FROM gastos WHERE id=?", [id_gasto])
        return rs.rows_affected > 0


async def actualizar_item(id_gasto: int, item: str, categoria: str) -> None:
    async with _client() as client:
        await client.execute(
            "UPDATE gastos SET item=?, categoria=? WHERE id=?", [item, categoria, id_gasto]
        )


async def actualizar_valor(id_gasto: int, valor: int) -> None:
    async with _client() as client:
        await client.execute("UPDATE gastos SET valor=? WHERE id=?", [valor, id_gasto])


async def actualizar_categoria(id_gasto: int, categoria: str) -> None:
    async with _client() as client:
        await client.execute("UPDATE gastos SET categoria=? WHERE id=?", [categoria, id_gasto])


async def actualizar_fecha(id_gasto: int, fecha: datetime) -> None:
    async with _client() as client:
        await client.execute(
            "UPDATE gastos SET fecha=?, anio=?, n_mes=?, mes=? WHERE id=?",
            [
                fecha.strftime("%Y-%m-%d %H:%M:%S"),
                fecha.year,
                fecha.month,
                MESES_ES[fecha.month],
                id_gasto,
            ],
        )


async def registrar_intento_acceso(
    usuario_id: int, usuario_nombre: str, username: str | None, mensaje: str
) -> None:
    async with _client() as client:
        await client.execute(
            """
            INSERT INTO intentos_acceso (fecha, usuario_id, usuario_nombre, username, mensaje)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                usuario_id,
                usuario_nombre,
                username,
                mensaje,
            ],
        )


async def obtener_intentos_acceso(limite: int = 20) -> list[dict]:
    async with _client() as client:
        rs = await client.execute(
            """
            SELECT fecha, usuario_id, usuario_nombre, username, mensaje
            FROM intentos_acceso
            ORDER BY id DESC
            LIMIT ?
            """,
            [limite],
        )
        return _rows_to_dicts(rs)


async def consultar_df(query: str, params: tuple = ()) -> pd.DataFrame:
    async with _client() as client:
        rs = await client.execute(query, list(params))
        return pd.DataFrame([list(row) for row in rs.rows], columns=list(rs.columns))


async def generar_excel() -> Path:
    df = await consultar_df("""
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

    export_path = Path("/tmp") / EXPORT_FILE

    with pd.ExcelWriter(export_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Consolidado", index=False)

        if not df.empty:
            resumen = df.groupby(["Año", "N_mes", "Mes", "Categoria"], as_index=False)["Valor"].sum()
            resumen.to_excel(writer, sheet_name="Resumen", index=False)

    return export_path
