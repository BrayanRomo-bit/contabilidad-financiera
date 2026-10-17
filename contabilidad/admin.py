from django.contrib import admin
from .models import (
    MateriaPrima,
    OrdenProduccion,
    MovimientoAlmacenMP,
    ManoObraDirecta,
    GastoIndirecto,
    ProrrateoGI,
    ProrrateoOrdenDetalle,
    ArticuloTerminado,
)


@admin.register(MateriaPrima)
class MateriaPrimaAdmin(admin.ModelAdmin):
    list_display = ("codigo", "nombre", "unidad_medida", "existencia_actual", "costo_promedio_actual", "saldo_actual")
    search_fields = ("codigo", "nombre")


@admin.register(OrdenProduccion)
class OrdenProduccionAdmin(admin.ModelAdmin):
    list_display = ("codigo", "producto_terminado", "cantidad_unidades", "estado", "total_mp", "total_mo", "total_gi", "costo_total")
    list_filter = ("estado",)
    search_fields = ("codigo", "producto_terminado")


@admin.register(MovimientoAlmacenMP)
class MovimientoAlmacenMPAdmin(admin.ModelAdmin):
    list_display = ("fecha", "referencia", "materia_prima", "tipo_movimiento", "orden_produccion", "unidades_entrada", "unidades_salida", "existencia", "debe", "haber", "saldo")
    list_filter = ("tipo_movimiento", "materia_prima")


@admin.register(ManoObraDirecta)
class ManoObraDirectaAdmin(admin.ModelAdmin):
    list_display = ("fecha", "referencia_nomina", "orden_produccion", "operario_departamento", "horas_trabajadas", "cuota_por_hora", "importe_total")


@admin.register(GastoIndirecto)
class GastoIndirectoAdmin(admin.ModelAdmin):
    list_display = ("fecha", "codigo_referencia", "categoria", "concepto", "importe", "prorrateado")
    list_filter = ("categoria", "prorrateado")


@admin.register(ProrrateoGI)
class ProrrateoGIAdmin(admin.ModelAdmin):
    list_display = ("codigo", "fecha", "base_seleccionada", "total_gastos_indirectos", "total_base", "factor_prorrateo")


@admin.register(ProrrateoOrdenDetalle)
class ProrrateoOrdenDetalleAdmin(admin.ModelAdmin):
    list_display = ("prorrateo", "orden_produccion", "valor_base_orden", "porcentaje_participacion", "importe_asignado")


@admin.register(ArticuloTerminado)
class ArticuloTerminadoAdmin(admin.ModelAdmin):
    list_display = ("codigo_entrada", "orden_produccion", "nombre_articulo", "unidades_recibidas", "costo_total", "costo_unitario", "fecha_entrada")
