from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import date
from contextlib import contextmanager

app = FastAPI(title="Dulcería Manager API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────
# DATABASE CONNECTION (Aiven PostgreSQL)
# ──────────────────────────────────────────────
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://user:password@host:port/defaultdb?sslmode=require"
)

def get_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ──────────────────────────────────────────────
# DATABASE INITIALIZATION
# ──────────────────────────────────────────────
def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS productos (
                id SERIAL PRIMARY KEY,
                nombre VARCHAR(100) NOT NULL,
                tipo VARCHAR(50) NOT NULL,
                precio_venta NUMERIC(10,2) NOT NULL DEFAULT 0,
                stock_total INTEGER NOT NULL DEFAULT 0,
                stock_disponible INTEGER NOT NULL DEFAULT 0,
                fecha_produccion DATE NOT NULL DEFAULT CURRENT_DATE,
                activo BOOLEAN DEFAULT TRUE
            );

            CREATE TABLE IF NOT EXISTS vendedores (
                id SERIAL PRIMARY KEY,
                nombre VARCHAR(100) NOT NULL,
                activo BOOLEAN DEFAULT TRUE
            );

            CREATE TABLE IF NOT EXISTS asignaciones (
                id SERIAL PRIMARY KEY,
                vendedor_id INTEGER REFERENCES vendedores(id),
                producto_id INTEGER REFERENCES productos(id),
                cantidad_asignada INTEGER NOT NULL,
                fecha DATE NOT NULL DEFAULT CURRENT_DATE,
                cerrado BOOLEAN DEFAULT FALSE
            );

            CREATE TABLE IF NOT EXISTS cortes (
                id SERIAL PRIMARY KEY,
                asignacion_id INTEGER REFERENCES asignaciones(id),
                vendedor_id INTEGER REFERENCES vendedores(id),
                producto_id INTEGER REFERENCES productos(id),
                cantidad_asignada INTEGER NOT NULL,
                cantidad_devuelta INTEGER NOT NULL,
                cantidad_vendida INTEGER NOT NULL,
                precio_unitario NUMERIC(10,2) NOT NULL,
                total_cobrar NUMERIC(10,2) NOT NULL,
                fecha DATE NOT NULL DEFAULT CURRENT_DATE
            );
        """)

try:
    init_db()
except Exception as e:
    print(f"[WARN] No se pudo inicializar la BD: {e}")

# ──────────────────────────────────────────────
# SCHEMAS
# ──────────────────────────────────────────────
class CalculadoraInput(BaseModel):
    costo_insumo: float
    costo_bolsas: float
    cantidad_total_insumo: float
    porcion_por_bolsita: float
    porcentaje_ganancia: float

class ProductoCreate(BaseModel):
    nombre: str
    tipo: str
    precio_venta: float
    stock_total: int
    fecha_produccion: Optional[str] = None

class VendedorCreate(BaseModel):
    nombre: str

class AsignacionCreate(BaseModel):
    vendedor_id: int
    producto_id: int
    cantidad: int
    fecha: Optional[str] = None

class CorteInput(BaseModel):
    vendedor_id: int
    producto_id: int
    cantidad_devuelta: int
    fecha: Optional[str] = None

# ──────────────────────────────────────────────
# MÓDULO 1 — CALCULADORA DE PRECIO
# ──────────────────────────────────────────────
@app.post("/api/calculadora")
def calcular_precio(data: CalculadoraInput):
    """
    Calcula el rendimiento, costo unitario y precio de venta sugerido.
    """
    if data.porcion_por_bolsita <= 0:
        raise HTTPException(400, "La porción por bolsita debe ser mayor a 0.")
    if data.cantidad_total_insumo <= 0:
        raise HTTPException(400, "La cantidad total del insumo debe ser mayor a 0.")
    if data.porcentaje_ganancia < 0:
        raise HTTPException(400, "El porcentaje de ganancia no puede ser negativo.")

    rendimiento = int(data.cantidad_total_insumo // data.porcion_por_bolsita)
    if rendimiento == 0:
        raise HTTPException(400, "La porción es mayor al total del insumo. Rendimiento = 0.")

    costo_insumo_por_bolsita = data.costo_insumo / rendimiento
    costo_bolsa_por_unidad = data.costo_bolsas / rendimiento
    costo_unitario = costo_insumo_por_bolsita + costo_bolsa_por_unidad

    precio_venta_sugerido = costo_unitario * (1 + data.porcentaje_ganancia / 100)

    return {
        "rendimiento_bolsitas": rendimiento,
        "costo_insumo_por_bolsita": round(costo_insumo_por_bolsita, 4),
        "costo_bolsa_por_unidad": round(costo_bolsa_por_unidad, 4),
        "costo_unitario_produccion": round(costo_unitario, 4),
        "porcentaje_ganancia": data.porcentaje_ganancia,
        "precio_venta_sugerido": round(precio_venta_sugerido, 2),
        "ganancia_por_unidad": round(precio_venta_sugerido - costo_unitario, 4),
        "ganancia_total_estimada": round((precio_venta_sugerido - costo_unitario) * rendimiento, 2),
    }

# ──────────────────────────────────────────────
# MÓDULO 2 — INVENTARIO / BODEGA
# ──────────────────────────────────────────────
@app.get("/api/productos")
def listar_productos():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM productos WHERE activo = TRUE ORDER BY fecha_produccion DESC, id DESC
        """)
        return {"productos": cur.fetchall()}

@app.post("/api/productos")
def crear_producto(data: ProductoCreate):
    fecha = data.fecha_produccion or str(date.today())
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO productos (nombre, tipo, precio_venta, stock_total, stock_disponible, fecha_produccion)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
        """, (data.nombre, data.tipo, data.precio_venta, data.stock_total, data.stock_total, fecha))
        return {"producto": cur.fetchone()}

@app.delete("/api/productos/{producto_id}")
def eliminar_producto(producto_id: int):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE productos SET activo = FALSE WHERE id = %s RETURNING id", (producto_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Producto no encontrado.")
        return {"ok": True}

# ──────────────────────────────────────────────
# MÓDULO 3 — VENDEDORES Y ASIGNACIONES
# ──────────────────────────────────────────────
@app.get("/api/vendedores")
def listar_vendedores():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM vendedores WHERE activo = TRUE ORDER BY nombre")
        return {"vendedores": cur.fetchall()}

@app.post("/api/vendedores")
def crear_vendedor(data: VendedorCreate):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO vendedores (nombre) VALUES (%s) RETURNING *", (data.nombre,))
        return {"vendedor": cur.fetchone()}

@app.post("/api/asignaciones")
def crear_asignacion(data: AsignacionCreate):
    fecha = data.fecha or str(date.today())
    with get_db() as conn:
        cur = conn.cursor()

        # Verificar stock disponible
        cur.execute("SELECT stock_disponible, nombre, precio_venta FROM productos WHERE id = %s AND activo = TRUE", (data.producto_id,))
        producto = cur.fetchone()
        if not producto:
            raise HTTPException(404, "Producto no encontrado.")
        if producto["stock_disponible"] < data.cantidad:
            raise HTTPException(400, f"Stock insuficiente. Disponible: {producto['stock_disponible']}")

        # Verificar vendedor
        cur.execute("SELECT nombre FROM vendedores WHERE id = %s AND activo = TRUE", (data.vendedor_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Vendedor no encontrado.")

        # Crear asignación
        cur.execute("""
            INSERT INTO asignaciones (vendedor_id, producto_id, cantidad_asignada, fecha)
            VALUES (%s, %s, %s, %s) RETURNING *
        """, (data.vendedor_id, data.producto_id, data.cantidad, fecha))
        asignacion = cur.fetchone()

        # Descontar del stock disponible
        cur.execute("""
            UPDATE productos SET stock_disponible = stock_disponible - %s WHERE id = %s
        """, (data.cantidad, data.producto_id))

        return {"asignacion": asignacion}

@app.get("/api/asignaciones")
def listar_asignaciones(fecha: Optional[str] = None):
    fecha_filtro = fecha or str(date.today())
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT a.id, a.fecha, a.cantidad_asignada, a.cerrado,
                   v.nombre AS vendedor, p.nombre AS producto,
                   p.tipo, p.precio_venta,
                   a.vendedor_id, a.producto_id
            FROM asignaciones a
            JOIN vendedores v ON v.id = a.vendedor_id
            JOIN productos p ON p.id = a.producto_id
            WHERE a.fecha = %s
            ORDER BY a.id DESC
        """, (fecha_filtro,))
        return {"asignaciones": cur.fetchall()}

# ──────────────────────────────────────────────
# MÓDULO 4 — CORTE DIARIO / CIERRE DE CAJA
# ──────────────────────────────────────────────
@app.post("/api/corte_vendedor")
def registrar_corte(data: CorteInput):
    """
    Regla de oro: recibe devoluciones, calcula vendidas y total a cobrar.
    Vendidas = Asignadas - Devueltas
    Total = Vendidas × Precio
    """
    fecha = data.fecha or str(date.today())
    with get_db() as conn:
        cur = conn.cursor()

        # Buscar asignación activa del día
        cur.execute("""
            SELECT a.id, a.cantidad_asignada, p.precio_venta, p.nombre AS producto, v.nombre AS vendedor
            FROM asignaciones a
            JOIN productos p ON p.id = a.producto_id
            JOIN vendedores v ON v.id = a.vendedor_id
            WHERE a.vendedor_id = %s AND a.producto_id = %s AND a.fecha = %s AND a.cerrado = FALSE
            ORDER BY a.id DESC LIMIT 1
        """, (data.vendedor_id, data.producto_id, fecha))
        asignacion = cur.fetchone()

        if not asignacion:
            raise HTTPException(404, "No se encontró asignación activa para este vendedor/producto en la fecha indicada.")

        if data.cantidad_devuelta < 0:
            raise HTTPException(400, "La cantidad devuelta no puede ser negativa.")
        if data.cantidad_devuelta > asignacion["cantidad_asignada"]:
            raise HTTPException(400, f"Las devoluciones ({data.cantidad_devuelta}) no pueden superar lo asignado ({asignacion['cantidad_asignada']}).")

        cantidad_vendida = asignacion["cantidad_asignada"] - data.cantidad_devuelta
        total_cobrar = cantidad_vendida * float(asignacion["precio_venta"])

        # Registrar corte
        cur.execute("""
            INSERT INTO cortes (asignacion_id, vendedor_id, producto_id, cantidad_asignada,
                                cantidad_devuelta, cantidad_vendida, precio_unitario, total_cobrar, fecha)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """, (
            asignacion["id"], data.vendedor_id, data.producto_id,
            asignacion["cantidad_asignada"], data.cantidad_devuelta,
            cantidad_vendida, asignacion["precio_venta"], total_cobrar, fecha
        ))
        corte = cur.fetchone()

        # Marcar asignación como cerrada
        cur.execute("UPDATE asignaciones SET cerrado = TRUE WHERE id = %s", (asignacion["id"],))

        # Regresar sobrantes al stock disponible
        cur.execute("""
            UPDATE productos SET stock_disponible = stock_disponible + %s WHERE id = %s
        """, (data.cantidad_devuelta, data.producto_id))

        return {
            "corte": corte,
            "resumen": {
                "vendedor": asignacion["vendedor"],
                "producto": asignacion["producto"],
                "asignadas": asignacion["cantidad_asignada"],
                "devueltas": data.cantidad_devuelta,
                "vendidas": cantidad_vendida,
                "precio_unitario": float(asignacion["precio_venta"]),
                "total_a_cobrar": round(total_cobrar, 2),
            }
        }

@app.get("/api/cortes")
def listar_cortes(fecha: Optional[str] = None):
    fecha_filtro = fecha or str(date.today())
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT c.id, c.fecha, c.cantidad_asignada, c.cantidad_devuelta,
                   c.cantidad_vendida, c.precio_unitario, c.total_cobrar,
                   v.nombre AS vendedor, p.nombre AS producto, p.tipo
            FROM cortes c
            JOIN vendedores v ON v.id = c.vendedor_id
            JOIN productos p ON p.id = c.producto_id
            WHERE c.fecha = %s
            ORDER BY c.id DESC
        """, (fecha_filtro,))
        rows = cur.fetchall()

        total_dia = sum(float(r["total_cobrar"]) for r in rows)
        return {"cortes": rows, "total_dia": round(total_dia, 2)}

# ──────────────────────────────────────────────
# MÓDULO 5 — REPORTE DE POPULARIDAD
# ──────────────────────────────────────────────
@app.get("/api/reporte_popularidad")
def reporte_popularidad(fecha: Optional[str] = None):
    fecha_filtro = fecha or str(date.today())
    with get_db() as conn:
        cur = conn.cursor()

        # Ventas por producto en el día
        cur.execute("""
            SELECT p.nombre AS producto, p.tipo,
                   COALESCE(SUM(c.cantidad_vendida), 0) AS total_vendido,
                   COALESCE(SUM(c.total_cobrar), 0) AS ingreso_total,
                   p.stock_total, p.stock_disponible,
                   p.precio_venta
            FROM productos p
            LEFT JOIN cortes c ON c.producto_id = p.id AND c.fecha = %s
            WHERE p.activo = TRUE AND p.fecha_produccion = %s
            GROUP BY p.id, p.nombre, p.tipo, p.stock_total, p.stock_disponible, p.precio_venta
            ORDER BY total_vendido DESC
        """, (fecha_filtro, fecha_filtro))
        productos = cur.fetchall()

        mas_vendido = None
        if productos:
            # El primero ya está ordenado por ventas DESC
            top = productos[0]
            if top["total_vendido"] > 0:
                mas_vendido = {
                    "nombre": top["producto"],
                    "tipo": top["tipo"],
                    "vendidos": top["total_vendido"],
                    "ingresos": float(top["ingreso_total"]),
                }

        # Totales generales del día
        cur.execute("""
            SELECT COALESCE(SUM(total_cobrar), 0) AS ingreso_total_dia,
                   COALESCE(SUM(cantidad_vendida), 0) AS unidades_vendidas_dia
            FROM cortes WHERE fecha = %s
        """, (fecha_filtro,))
        totales = cur.fetchone()

        return {
            "fecha": fecha_filtro,
            "productos": productos,
            "mas_vendido": mas_vendido,
            "ingreso_total_dia": float(totales["ingreso_total_dia"]),
            "unidades_vendidas_dia": totales["unidades_vendidas_dia"],
        }

# ──────────────────────────────────────────────
# HEALTH CHECK
# ──────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "app": "Dulcería Manager API v1.0"}

@app.get("/api/health")
def health():
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "error", "db": str(e)}
