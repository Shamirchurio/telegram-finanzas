"""Script de uso único: copia los gastos de la SQLite local (gastos.db) a
la base de datos Turso.

Uso:
    python scripts/migrate_to_turso.py

Requiere en el entorno (o en el .env de la raíz): TURSO_DATABASE_URL y
TURSO_AUTH_TOKEN. Es idempotente en el sentido de que vuelve a crear el
esquema si no existe, pero NO evita duplicados si se corre dos veces —
está pensado para correrse una sola vez sobre una base Turso vacía.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.platform == "win32":
    # El ProactorEventLoop (default en Windows) falla resolviendo DNS de forma
    # intermitente en algunos entornos (interacción con antivirus/WinSock).
    # El SelectorEventLoop no tiene ese problema.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv

load_dotenv(override=True)

import db_turso  # noqa: E402  (después de load_dotenv a propósito)

DB_FILE = Path(__file__).resolve().parent.parent / "gastos.db"


async def main():
    if not DB_FILE.exists():
        print(f"No se encontró {DB_FILE}")
        return

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    filas = conn.execute("SELECT * FROM gastos ORDER BY id").fetchall()
    conn.close()

    print(f"Leídas {len(filas)} filas de {DB_FILE.name}")

    await db_turso.init_db()

    async with db_turso._client() as client:
        for fila in filas:
            await client.execute(
                """
                INSERT INTO gastos
                (id, fecha, anio, n_mes, mes, item, valor, categoria, usuario_id, usuario_nombre)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    fila["id"],
                    fila["fecha"],
                    fila["anio"],
                    fila["n_mes"],
                    fila["mes"],
                    fila["item"],
                    fila["valor"],
                    fila["categoria"],
                    fila["usuario_id"],
                    fila["usuario_nombre"],
                ],
            )

    print(f"Migradas {len(filas)} filas a Turso (OK)")


if __name__ == "__main__":
    asyncio.run(main())
