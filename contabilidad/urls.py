from django.urls import path
from . import views

urlpatterns = [
    path("", views.captura_view, name="captura"),
    path("tarjetas-almacen/", views.tarjetas_almacen_view, name="tarjetas_almacen"),
    path("ordenes-produccion/", views.ordenes_produccion_view, name="ordenes_produccion"),
    path("costos-indirectos/", views.costos_indirectos_view, name="costos_indirectos"),
    path("articulos-terminados/", views.articulos_terminados_view, name="articulos_terminados"),
    path("resultados/", views.resultados_view, name="resultados"),
    path(
        "resultados/exportar-excel/",
        views.exportar_resultados_excel_view,
        name="exportar_resultados_excel",
    ),
    path(
        "cargar-ejemplo-pizarron/",
        views.cargar_ejemplo_pizarron_view,
        name="cargar_ejemplo_pizarron",
    ),
    path(
        "limpiar-datos/",
        views.limpiar_datos_view,
        name="limpiar_datos",
    ),
    # Endpoints para borrar capturas o tarjetas individuales
    path(
        "eliminar/tarjeta-mp/<int:pk>/",
        views.eliminar_materia_prima_view,
        name="eliminar_materia_prima",
    ),
    path(
        "eliminar/movimiento-mp/<int:pk>/",
        views.eliminar_movimiento_mp_view,
        name="eliminar_movimiento_mp",
    ),
    path(
        "eliminar/orden/<int:pk>/",
        views.eliminar_orden_produccion_view,
        name="eliminar_orden_produccion",
    ),
    path(
        "eliminar/mano-obra/<int:pk>/",
        views.eliminar_mano_obra_view,
        name="eliminar_mano_obra",
    ),
    path(
        "eliminar/gasto-indirecto/<int:pk>/",
        views.eliminar_gasto_indirecto_view,
        name="eliminar_gasto_indirecto",
    ),
    path(
        "eliminar/prorrateo/<int:pk>/",
        views.eliminar_prorrateo_view,
        name="eliminar_prorrateo",
    ),
    path(
        "eliminar/articulo-terminado/<int:pk>/",
        views.eliminar_articulo_terminado_view,
        name="eliminar_articulo_terminado",
    ),
]
