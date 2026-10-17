from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from .models import (
    MateriaPrima,
    OrdenProduccion,
    MovimientoAlmacenMP,
    ManoObraDirecta,
    GastoIndirecto,
    ProrrateoGI,
    ArticuloTerminado,
)


class ContabilidadFlujoCompletoTests(TestCase):
    def test_inicia_vacio_por_defecto(self):
        # Por defecto toda la información inicia vacía esperando que el usuario la cree
        self.assertEqual(MateriaPrima.objects.count(), 0)
        self.assertEqual(OrdenProduccion.objects.count(), 0)
        self.assertEqual(MovimientoAlmacenMP.objects.count(), 0)
        self.assertEqual(GastoIndirecto.objects.count(), 0)

    def test_normalizacion_y_kardex_afecta_ordenes(self):
        # 1. Crear Materia Prima con espacios y minúsculas para verificar normalización
        mp = MateriaPrima.objects.create(
            codigo="  mp 001  ",
            nombre="  madera de pino  ",
            unidad_medida="PZA",
            email_proveedor="  VENTAS@PROVEEDOR.COM ",
        )
        self.assertEqual(mp.codigo, "MP-001")
        self.assertEqual(mp.nombre, "Madera de pino")
        self.assertEqual(mp.email_proveedor, "ventas@proveedor.com")

        # 2. Alta de Orden de Producción
        op = OrdenProduccion.objects.create(
            codigo=" op 1050 ",
            producto_terminado=" escritorio ejecutivo ",
            cantidad_unidades=10,
        )
        self.assertEqual(op.codigo, "OP-1050")
        self.assertEqual(op.producto_terminado, "Escritorio ejecutivo")

        # 3. Movimientos de Tarjeta de Almacén: Saldo Inicial + Compra + Baja a Producción
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp,
            tipo_movimiento="SALDO_INICIAL",
            referencia=" inv 01 ",
            unidades_entrada=Decimal("100.00"),
            costo_unitario=Decimal("50.00"),
        )
        self.assertEqual(mp.existencia_actual, Decimal("100.00"))
        self.assertEqual(mp.saldo_actual, Decimal("5000.00"))

        MovimientoAlmacenMP.objects.create(
            materia_prima=mp,
            tipo_movimiento="COMPRA",
            referencia=" fac 02 ",
            unidades_entrada=Decimal("100.00"),
            costo_unitario=Decimal("70.00"),
        )
        self.assertEqual(mp.existencia_actual, Decimal("200.00"))
        self.assertEqual(mp.costo_promedio_actual, Decimal("60.00"))
        self.assertEqual(mp.saldo_actual, Decimal("12000.00"))

        # Baja a producción: 50 unidades @ $60.00 = $3,000.00 hacia OP-1050
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp,
            orden_produccion=op,
            tipo_movimiento="BAJA_PRODUCCION",
            referencia=" req 1050 ",
            unidades_salida=Decimal("50.00"),
            costo_unitario=mp.costo_promedio_actual,
        )
        self.assertEqual(mp.existencia_actual, Decimal("150.00"))
        self.assertEqual(mp.saldo_actual, Decimal("9000.00"))
        self.assertEqual(op.total_mp, Decimal("3000.00"))

        # 4. Mano de Obra Directa
        ManoObraDirecta.objects.create(
            orden_produccion=op,
            referencia_nomina=" nom 01 ",
            operario_departamento="Cuadrilla A",
            horas_trabajadas=Decimal("20.00"),
            cuota_por_hora=Decimal("100.00"),
            importe_total=Decimal("2000.00"),
        )
        self.assertEqual(op.total_mo, Decimal("2000.00"))
        self.assertEqual(op.costo_primo, Decimal("5000.00"))

    def test_cargar_preguardados_grafica_pastel_y_limpiar(self):
        # 1. Cargar datos preguardados
        resp = self.client.post(reverse("cargar_ejemplo_pizarron"))
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(OrdenProduccion.objects.count(), 3)
        self.assertEqual(MateriaPrima.objects.count(), 2)
        self.assertEqual(ProrrateoGI.objects.count(), 1)
        self.assertEqual(ArticuloTerminado.objects.count(), 1)

        # 2. Verificar Apartado de Resultados incluye Gráfica de Pastel y NO el esquema JSON
        r_res = self.client.get(reverse("resultados"))
        self.assertEqual(r_res.status_code, 200)
        self.assertContains(r_res, "Gráfica de Pastel")
        self.assertContains(r_res, "canvasPastelElementos")
        self.assertNotContains(r_res, "Esquema Relacional / JSON Estructurado de Resultados")

        # 3. Limpiar datos para volver al estado vacío por defecto
        resp_clean = self.client.post(reverse("limpiar_datos"))
        self.assertEqual(resp_clean.status_code, 302)
        self.assertEqual(OrdenProduccion.objects.count(), 0)
        self.assertEqual(MateriaPrima.objects.count(), 0)
