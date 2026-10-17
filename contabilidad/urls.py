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
        "cargar-ejemplo-pizarron/",
        views.cargar_ejemplo_pizarron_view,
        name="cargar_ejemplo_pizarron",
    ),
    path(
        "limpiar-datos/",
        views.limpiar_datos_view,
        name="limpiar_datos",
    ),
]
