import io
import json
from decimal import Decimal, ROUND_HALF_UP
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from openpyxl import Workbook
from openpyxl.chart import BarChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .forms import (
    MateriaPrimaCatalogoForm,
    AltaMPMovimientoForm,
    BajaMPProduccionForm,
    OrdenProduccionForm,
    ManoObraDirectaForm,
    GastoIndirectoForm,
    ProrrateoGIForm,
    BajaProduccionTerminadaForm,
)
from .models import (
    MateriaPrima,
    OrdenProduccion,
    MovimientoAlmacenMP,
    ManoObraDirecta,
    GastoIndirecto,
    ProrrateoGI,
    ProrrateoOrdenDetalle,
    ArticuloTerminado,
    redondear_moneda,
)


def _obtener_contexto_base():
    """Retorna métricas globales y catálogos en JSON para ayudas en el cliente."""
    materias = MateriaPrima.objects.all()
    ordenes = OrdenProduccion.objects.all()
    gastos_gi = GastoIndirecto.objects.all()
    terminados = ArticuloTerminado.objects.all()

    mp_final_total = sum((m.saldo_actual for m in materias), Decimal("0.00"))
    costo_ordenes_total = sum((o.costo_total for o in ordenes), Decimal("0.00"))
    gi_pendiente_total = (
        gastos_gi.filter(prorrateado=False).aggregate(t=Sum("importe"))["t"]
        or Decimal("0.00")
    )
    gi_acumulado_total = (
        gastos_gi.aggregate(t=Sum("importe"))["t"] or Decimal("0.00")
    )

    hay_datos = (
        materias.exists()
        or ordenes.exists()
        or gastos_gi.exists()
        or terminados.exists()
    )

    mp_catalogo_js = {
        str(m.id): {
            "id": m.id,
            "codigo": m.codigo,
            "nombre": m.nombre,
            "unidad": m.unidad_medida,
            "existencia": float(m.existencia_actual),
            "costo_promedio": float(m.costo_promedio_actual),
            "saldo": float(m.saldo_actual),
        }
        for m in materias
    }
    ordenes_catalogo_js = {
        str(o.id): {
            "id": o.id,
            "codigo": o.codigo,
            "producto": o.producto_terminado,
            "unidades": o.cantidad_unidades,
            "estado": o.get_estado_display(),
            "mp": float(o.total_mp),
            "mo": float(o.total_mo),
            "gi": float(o.total_gi),
            "total": float(o.costo_total),
            "unitario": float(o.costo_unitario),
        }
        for o in ordenes
    }

    return {
        "hay_datos": hay_datos,
        "metricas_globales": {
            "total_tarjetas_mp": materias.count(),
            "mp_final_total": redondear_moneda(mp_final_total),
            "total_ordenes": ordenes.count(),
            "ordenes_en_proceso": ordenes.filter(estado="EN_PROCESO").count(),
            "ordenes_terminadas": ordenes.filter(estado="TERMINADA").count(),
            "costo_ordenes_total": redondear_moneda(costo_ordenes_total),
            "gi_pendiente_total": redondear_moneda(gi_pendiente_total),
            "gi_acumulado_total": redondear_moneda(gi_acumulado_total),
            "total_terminados": terminados.count(),
        },
        "mp_catalogo_json": json.dumps(mp_catalogo_js),
        "ordenes_catalogo_json": json.dumps(ordenes_catalogo_js),
    }


def captura_view(request):
    """Centro de captura simplificado y directo (sin duplicar Gastos Indirectos)."""
    tab_activa = request.GET.get("tab", "mp_alta")
    if tab_activa == "gi_captura":
        return redirect("costos_indirectos")

    op_preseleccionada = request.GET.get("op")
    initial_orden = {"orden_produccion": op_preseleccionada} if op_preseleccionada else {}

    form_mp_catalogo = MateriaPrimaCatalogoForm(prefix="cat_mp")
    form_mp_alta = AltaMPMovimientoForm(prefix="alta_mp")
    form_mp_baja = BajaMPProduccionForm(prefix="baja_mp", initial=initial_orden)
    form_op_alta = OrdenProduccionForm(prefix="alta_op")
    form_mo = ManoObraDirectaForm(prefix="mo", initial=initial_orden)
    form_baja_prod = BajaProduccionTerminadaForm(prefix="baja_prod", initial=initial_orden)

    if request.method == "POST":
        tipo_captura = request.POST.get("tipo_captura", "")
        tab_activa = tipo_captura or tab_activa

        if tipo_captura == "mp_catalogo":
            form_mp_catalogo = MateriaPrimaCatalogoForm(request.POST, prefix="cat_mp")
            if form_mp_catalogo.is_valid():
                with transaction.atomic():
                    mp = form_mp_catalogo.save()
                    if form_mp_catalogo.cleaned_data.get("registrar_saldo_inicial"):
                        unidades = form_mp_catalogo.cleaned_data["unidades_iniciales"]
                        costo = form_mp_catalogo.cleaned_data["costo_unitario_inicial"]
                        fecha_ini = form_mp_catalogo.cleaned_data["fecha_inicial"]
                        MovimientoAlmacenMP.objects.create(
                            materia_prima=mp,
                            fecha=fecha_ini,
                            tipo_movimiento="SALDO_INICIAL",
                            referencia=f"INI-{mp.codigo}",
                            unidades_entrada=unidades,
                            costo_unitario=costo,
                            observaciones="Inventario inicial al abrir la tarjeta.",
                        )
                messages.success(
                    request,
                    f"Tarjeta '{mp.codigo} — {mp.nombre}' guardada.",
                )
                return redirect("/?tab=mp_catalogo")
            messages.error(request, "Revisa los campos marcados en Nueva tarjeta.")

        elif tipo_captura == "mp_alta":
            form_mp_alta = AltaMPMovimientoForm(request.POST, prefix="alta_mp")
            if form_mp_alta.is_valid():
                cd = form_mp_alta.cleaned_data
                mov = MovimientoAlmacenMP.objects.create(
                    materia_prima=cd["materia_prima"],
                    fecha=cd["fecha"],
                    tipo_movimiento=cd["tipo_movimiento"],
                    referencia=cd["referencia"],
                    unidades_entrada=cd["unidades_entrada"],
                    costo_unitario=cd["costo_unitario"],
                    observaciones=cd.get("observaciones", ""),
                )
                messages.success(
                    request,
                    f"Entrada registrada en {mov.materia_prima.codigo}: +{mov.unidades_entrada} {mov.materia_prima.unidad_medida} (${mov.debe:,.2f}).",
                )
                return redirect("/?tab=mp_alta")
            messages.error(request, "Revisa los datos de la entrada de materia prima.")

        elif tipo_captura == "mp_baja":
            form_mp_baja = BajaMPProduccionForm(request.POST, prefix="baja_mp")
            if form_mp_baja.is_valid():
                cd = form_mp_baja.cleaned_data
                mp = cd["materia_prima"]
                costo_vigente = mp.costo_promedio_actual
                mov = MovimientoAlmacenMP.objects.create(
                    materia_prima=mp,
                    orden_produccion=cd["orden_produccion"],
                    fecha=cd["fecha"],
                    tipo_movimiento="BAJA_PRODUCCION",
                    referencia=cd["referencia"],
                    unidades_salida=cd["unidades_salida"],
                    costo_unitario=costo_vigente,
                    observaciones=cd.get("observaciones", ""),
                )
                messages.success(
                    request,
                    f"Salida registrada: {mov.unidades_salida} {mp.unidad_medida} de {mp.codigo} cargadas a {cd['orden_produccion'].codigo} (${mov.haber:,.2f}).",
                )
                return redirect("/?tab=mp_baja")
            messages.error(request, "No se pudo registrar la salida. Verifica la existencia disponible.")

        elif tipo_captura == "op_alta":
            form_op_alta = OrdenProduccionForm(request.POST, prefix="alta_op")
            if form_op_alta.is_valid():
                op = form_op_alta.save()
                messages.success(
                    request,
                    f"Orden de producción '{op.codigo}' ({op.producto_terminado}) creada.",
                )
                return redirect("/?tab=op_alta")
            messages.error(request, "Revisa los datos de la orden de producción.")

        elif tipo_captura == "mo_captura":
            form_mo = ManoObraDirectaForm(request.POST, prefix="mo")
            if form_mo.is_valid():
                mo = form_mo.save()
                messages.success(
                    request,
                    f"Mano de obra directa (${mo.importe_total:,.2f}) guardada en la orden {mo.orden_produccion.codigo}.",
                )
                return redirect("/?tab=mo_captura")
            messages.error(request, "Revisa los campos del cargo de mano de obra.")

        elif tipo_captura == "baja_prod":
            form_baja_prod = BajaProduccionTerminadaForm(request.POST, prefix="baja_prod")
            if form_baja_prod.is_valid():
                cd = form_baja_prod.cleaned_data
                op = cd["orden_produccion"]
                with transaction.atomic():
                    art = ArticuloTerminado.objects.create(
                        orden_produccion=op,
                        codigo_entrada=cd["codigo_entrada"],
                        fecha_entrada=cd["fecha_entrada"],
                        nombre_articulo=op.producto_terminado,
                        unidades_recibidas=op.cantidad_unidades,
                        costo_mp=op.total_mp,
                        costo_mo=op.total_mo,
                        costo_gi=op.total_gi,
                        costo_total=op.costo_total,
                        costo_unitario=op.costo_unitario,
                        observaciones=cd.get("observaciones", ""),
                    )
                    op.estado = "TERMINADA"
                    op.fecha_cierre = cd["fecha_entrada"]
                    op.save()
                messages.success(
                    request,
                    f"Orden '{op.codigo}' terminada y enviada a Artículos terminados ({art.codigo_entrada}).",
                )
                return redirect("/?tab=baja_prod")
            messages.error(request, "Revisa los datos para concluir la orden.")

    context = {
        "pagina_activa": "captura",
        "tab_activa": tab_activa,
        "form_mp_catalogo": form_mp_catalogo,
        "form_mp_alta": form_mp_alta,
        "form_mp_baja": form_mp_baja,
        "form_op_alta": form_op_alta,
        "form_mo": form_mo,
        "form_baja_prod": form_baja_prod,
        "ultimos_movimientos_mp": MovimientoAlmacenMP.objects.select_related(
            "materia_prima", "orden_produccion"
        ).order_by("-id")[:8],
        "ultimos_cargos_mo": ManoObraDirecta.objects.select_related(
            "orden_produccion"
        ).order_by("-id")[:8],
        **_obtener_contexto_base(),
    }
    return render(request, "contabilidad/captura.html", context)


def tarjetas_almacen_view(request):
    """Módulo de Tarjetas de Almacén (Kardex de Materia Prima)."""
    materias = MateriaPrima.objects.prefetch_related(
        "movimientos__orden_produccion"
    ).all()
    mp_seleccionada_id = request.GET.get("mp")
    mp_activa = None
    if mp_seleccionada_id:
        mp_activa = materias.filter(pk=mp_seleccionada_id).first()
    if not mp_activa and materias.exists():
        mp_activa = materias.first()

    context = {
        "pagina_activa": "tarjetas",
        "materias": materias,
        "mp_activa": mp_activa,
        **_obtener_contexto_base(),
    }
    return render(request, "contabilidad/tarjetas_almacen.html", context)


def ordenes_produccion_view(request):
    """Módulo de Órdenes de Producción con captura directa de Mano de Obra (M.O.) dentro de la orden."""
    ordenes = OrdenProduccion.objects.prefetch_related(
        "consumos_mp__materia_prima",
        "cargos_mo",
        "prorrateos_gi__prorrateo",
    ).all()
    orden_id = request.GET.get("op") or request.POST.get("orden_activa_id")
    orden_activa = None
    if orden_id:
        orden_activa = ordenes.filter(pk=orden_id).first()
    if not orden_activa and ordenes.exists():
        orden_activa = ordenes.first()

    form_mo_orden = ManoObraDirectaForm(
        prefix="mo_op",
        initial={"orden_produccion": orden_activa.id if orden_activa else None},
    )

    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion == "agregar_mo_orden":
            form_mo_orden = ManoObraDirectaForm(request.POST, prefix="mo_op")
            if form_mo_orden.is_valid():
                mo = form_mo_orden.save()
                messages.success(
                    request,
                    f"Mano de obra directa (${mo.importe_total:,.2f}) guardada dentro de la orden {mo.orden_produccion.codigo}.",
                )
                return redirect(f"/ordenes-produccion/?op={mo.orden_produccion.id}")
            messages.error(request, "Revisa los datos del cargo de mano de obra.")

    context = {
        "pagina_activa": "ordenes",
        "ordenes": ordenes,
        "orden_activa": orden_activa,
        "form_mo_orden": form_mo_orden,
        **_obtener_contexto_base(),
    }
    return render(request, "contabilidad/ordenes_produccion.html", context)


def _calcular_simulacion_prorrateo(base_seleccionada: str, solo_pendientes: bool):
    """Calcula la distribución de Gastos Indirectos según la base elegida."""
    if solo_pendientes:
        gastos_qs = GastoIndirecto.objects.filter(prorrateado=False)
    else:
        gastos_qs = GastoIndirecto.objects.all()

    total_gi = redondear_moneda(
        gastos_qs.aggregate(t=Sum("importe"))["t"] or Decimal("0.00")
    )
    ordenes = list(OrdenProduccion.objects.filter(estado="EN_PROCESO"))
    if not ordenes:
        ordenes = list(OrdenProduccion.objects.all())

    valores_base = []
    for op in ordenes:
        if base_seleccionada == "MP_DIRECTA":
            val = op.total_mp
        elif base_seleccionada == "MO_DIRECTA":
            val = op.total_mo
        elif base_seleccionada == "UNIDADES":
            val = Decimal(op.cantidad_unidades)
        elif base_seleccionada == "HORAS_MO":
            val = op.total_horas_mo
        else:  # COSTO_PRIMO
            val = op.costo_primo
        valores_base.append((op, redondear_moneda(val)))

    total_base = sum((v for _, v in valores_base), Decimal("0.00"))
    factor = Decimal("0.00000000")
    if total_base > 0 and total_gi > 0:
        factor = (total_gi / total_base).quantize(
            Decimal("0.00000001"), rounding=ROUND_HALF_UP
        )

    filas = []
    acumulado_asignado = Decimal("0.00")
    for idx, (op, val_base) in enumerate(valores_base):
        if total_base > 0 and total_gi > 0:
            porcentaje = redondear_moneda((val_base / total_base) * Decimal("100"))
            if idx == len(valores_base) - 1:
                asignado = redondear_moneda(total_gi - acumulado_asignado)
            else:
                asignado = redondear_moneda(val_base * factor)
                acumulado_asignado += asignado
        else:
            porcentaje = Decimal("0.00")
            asignado = Decimal("0.00")

        filas.append(
            {
                "orden": op,
                "valor_base": val_base,
                "factor": factor,
                "porcentaje": porcentaje,
                "importe_asignado": asignado,
                "nuevo_costo_total": redondear_moneda(op.costo_total + asignado),
            }
        )

    return {
        "gastos_qs": gastos_qs,
        "total_gi": total_gi,
        "total_base": total_base,
        "factor": factor,
        "filas": filas,
        "base_seleccionada": base_seleccionada,
    }


def costos_indirectos_view(request):
    """Módulo de Gastos Indirectos y Prorrateo."""
    base_preview = request.GET.get("base", "COSTO_PRIMO")
    solo_pendientes_preview = request.GET.get("pendientes", "1") != "0"

    form_gi = GastoIndirectoForm(prefix="nuevo_gi")
    sugerencia_folio = f"PRORRATEO-{ProrrateoGI.objects.count() + 1:02d}"
    form_prorrateo = ProrrateoGIForm(
        initial={
            "codigo": sugerencia_folio,
            "base_seleccionada": base_preview,
            "solo_pendientes": solo_pendientes_preview,
        },
        prefix="prorrateo",
    )

    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion == "agregar_gi":
            form_gi = GastoIndirectoForm(request.POST, prefix="nuevo_gi")
            if form_gi.is_valid():
                gi = form_gi.save()
                messages.success(
                    request,
                    f"Gasto indirecto '{gi.codigo_referencia}' (${gi.importe:,.2f}) agregado.",
                )
                return redirect("costos_indirectos")
            messages.error(request, "Revisa los campos del gasto indirecto.")

        elif accion == "ejecutar_prorrateo":
            form_prorrateo = ProrrateoGIForm(request.POST, prefix="prorrateo")
            if form_prorrateo.is_valid():
                cd = form_prorrateo.cleaned_data
                sim = _calcular_simulacion_prorrateo(
                    cd["base_seleccionada"], cd["solo_pendientes"]
                )
                if sim["total_gi"] <= 0:
                    messages.error(
                        request,
                        "No hay gastos indirectos pendientes para prorratear.",
                    )
                elif sim["total_base"] <= 0:
                    messages.error(
                        request,
                        "La base seleccionada suma 0 en las órdenes activas. Registra primero M.P. o M.O.",
                    )
                else:
                    with transaction.atomic():
                        prorrateo = ProrrateoGI.objects.create(
                            codigo=cd["codigo"],
                            fecha=cd["fecha"],
                            base_seleccionada=cd["base_seleccionada"],
                            total_gastos_indirectos=sim["total_gi"],
                            total_base=sim["total_base"],
                            factor_prorrateo=sim["factor"],
                            observaciones=cd.get("observaciones", ""),
                        )
                        for fila in sim["filas"]:
                            if fila["importe_asignado"] > 0:
                                ProrrateoOrdenDetalle.objects.create(
                                    prorrateo=prorrateo,
                                    orden_produccion=fila["orden"],
                                    valor_base_orden=fila["valor_base"],
                                    factor_aplicado=fila["factor"],
                                    porcentaje_participacion=fila["porcentaje"],
                                    importe_asignado=fila["importe_asignado"],
                                )
                        sim["gastos_qs"].update(prorrateado=True)

                    messages.success(
                        request,
                        f"Prorrateo '{prorrateo.codigo}' aplicado: ${prorrateo.total_gastos_indirectos:,.2f} distribuidos a las órdenes.",
                    )
                    return redirect("costos_indirectos")
            else:
                messages.error(request, "Revisa los datos del prorrateo.")

    simulacion = _calcular_simulacion_prorrateo(base_preview, solo_pendientes_preview)
    gastos = GastoIndirecto.objects.all().order_by("-fecha", "-id")
    prorrateos_historicos = ProrrateoGI.objects.prefetch_related(
        "detalles__orden_produccion"
    ).all()

    context = {
        "pagina_activa": "costos_indirectos",
        "form_gi": form_gi,
        "form_prorrateo": form_prorrateo,
        "gastos": gastos,
        "simulacion": simulacion,
        "prorrateos_historicos": prorrateos_historicos,
        "base_preview": base_preview,
        "solo_pendientes_preview": solo_pendientes_preview,
        **_obtener_contexto_base(),
    }
    return render(request, "contabilidad/costos_indirectos.html", context)


def articulos_terminados_view(request):
    """Módulo de Almacén de Artículos Terminados."""
    sugerencia = f"AT-{ArticuloTerminado.objects.count() + 1050}"
    form_baja_prod = BajaProduccionTerminadaForm(
        initial={"codigo_entrada": sugerencia},
        prefix="at",
    )

    if request.method == "POST":
        form_baja_prod = BajaProduccionTerminadaForm(request.POST, prefix="at")
        if form_baja_prod.is_valid():
            cd = form_baja_prod.cleaned_data
            op = cd["orden_produccion"]
            with transaction.atomic():
                art = ArticuloTerminado.objects.create(
                    orden_produccion=op,
                    codigo_entrada=cd["codigo_entrada"],
                    fecha_entrada=cd["fecha_entrada"],
                    nombre_articulo=op.producto_terminado,
                    unidades_recibidas=op.cantidad_unidades,
                    costo_mp=op.total_mp,
                    costo_mo=op.total_mo,
                    costo_gi=op.total_gi,
                    costo_total=op.costo_total,
                    costo_unitario=op.costo_unitario,
                    observaciones=cd.get("observaciones", ""),
                )
                op.estado = "TERMINADA"
                op.fecha_cierre = cd["fecha_entrada"]
                op.save()
            messages.success(
                request,
                f"Artículo terminado '{art.codigo_entrada}' registrado desde la orden {op.codigo}.",
            )
            return redirect("articulos_terminados")
        messages.error(request, "Revisa los campos para concluir la orden.")

    articulos = ArticuloTerminado.objects.select_related("orden_produccion").all()
    ordenes_en_proceso = OrdenProduccion.objects.filter(estado="EN_PROCESO")
    total_valor_terminados = sum((a.costo_total for a in articulos), Decimal("0.00"))

    context = {
        "pagina_activa": "articulos_terminados",
        "articulos": articulos,
        "ordenes_en_proceso": ordenes_en_proceso,
        "total_valor_terminados": redondear_moneda(total_valor_terminados),
        "form_baja_prod": form_baja_prod,
        **_obtener_contexto_base(),
    }
    return render(request, "contabilidad/articulos_terminados.html", context)


def resultados_view(request):
    """Apartado de Resultados con tablas contables, gráfica de barras y gráficas de pastel."""
    ordenes = list(OrdenProduccion.objects.all())
    materias = list(MateriaPrima.objects.all())
    gastos_gi = list(GastoIndirecto.objects.all())
    terminados = list(ArticuloTerminado.objects.all())

    suma_mp_ordenes = redondear_moneda(sum((o.total_mp for o in ordenes), Decimal("0.00")))
    suma_mo_ordenes = redondear_moneda(sum((o.total_mo for o in ordenes), Decimal("0.00")))
    suma_primo_ordenes = redondear_moneda(sum((o.costo_primo for o in ordenes), Decimal("0.00")))
    suma_gi_ordenes = redondear_moneda(sum((o.total_gi for o in ordenes), Decimal("0.00")))
    suma_total_ordenes = redondear_moneda(sum((o.costo_total for o in ordenes), Decimal("0.00")))

    inv_inicial_mp = redondear_moneda(
        MovimientoAlmacenMP.objects.filter(tipo_movimiento="SALDO_INICIAL").aggregate(
            t=Sum("debe")
        )["t"]
        or Decimal("0.00")
    )
    compras_mp = redondear_moneda(
        MovimientoAlmacenMP.objects.filter(tipo_movimiento="COMPRA").aggregate(
            t=Sum("debe")
        )["t"]
        or Decimal("0.00")
    )
    mp_disponible = redondear_moneda(inv_inicial_mp + compras_mp)
    mp_utilizada = redondear_moneda(
        MovimientoAlmacenMP.objects.filter(tipo_movimiento="BAJA_PRODUCCION").aggregate(
            t=Sum("haber")
        )["t"]
        or Decimal("0.00")
    )
    mp_final_saldo = redondear_moneda(sum((m.saldo_actual for m in materias), Decimal("0.00")))

    total_en_proceso = redondear_moneda(
        sum((o.costo_total for o in ordenes if o.estado == "EN_PROCESO"), Decimal("0.00"))
    )
    total_terminado = redondear_moneda(
        sum((o.costo_total for o in ordenes if o.estado == "TERMINADA"), Decimal("0.00"))
    )

    max_costo_orden = max([float(o.costo_total) for o in ordenes], default=1.0)
    if max_costo_orden <= 0:
        max_costo_orden = 1.0

    paleta_colores = [
        "#1b5e43",
        "#1e4e79",
        "#b45309",
        "#6d28d9",
        "#be185d",
        "#0f766e",
        "#b91c1c",
    ]

    datos_grafica_ordenes = []
    rebanadas_pastel_ordenes = []

    for idx, o in enumerate(ordenes):
        pct_altura = round((float(o.costo_total) / max_costo_orden) * 100, 1)
        pct_mp = round((float(o.total_mp) / float(o.costo_total)) * 100, 1) if o.costo_total > 0 else 0
        pct_mo = round((float(o.total_mo) / float(o.costo_total)) * 100, 1) if o.costo_total > 0 else 0
        pct_gi = round((float(o.total_gi) / float(o.costo_total)) * 100, 1) if o.costo_total > 0 else 0
        datos_grafica_ordenes.append(
            {
                "orden": o,
                "pct_altura": max(pct_altura, 8),
                "pct_mp": pct_mp,
                "pct_mo": pct_mo,
                "pct_gi": pct_gi,
            }
        )

        pct_del_total = (
            round((float(o.costo_total) / float(suma_total_ordenes)) * 100, 2)
            if suma_total_ordenes > 0
            else 0.0
        )
        color = paleta_colores[idx % len(paleta_colores)]
        rebanadas_pastel_ordenes.append(
            {
                "codigo": o.codigo,
                "producto": o.producto_terminado,
                "importe": o.costo_total,
                "porcentaje": pct_del_total,
                "color": color,
            }
        )

    if suma_total_ordenes > 0:
        pct_mp_global = round((float(suma_mp_ordenes) / float(suma_total_ordenes)) * 100, 2)
        pct_mo_global = round((float(suma_mo_ordenes) / float(suma_total_ordenes)) * 100, 2)
        pct_gi_global = round(100.0 - pct_mp_global - pct_mo_global, 2)
    else:
        pct_mp_global = 0.0
        pct_mo_global = 0.0
        pct_gi_global = 0.0

    pastel_elementos_json = json.dumps(
        {
            "labels": [
                "Materia Prima (M.P.)",
                "Mano de Obra (M.O.)",
                "Gastos Indirectos (G.I.)",
            ],
            "valores": [
                float(suma_mp_ordenes),
                float(suma_mo_ordenes),
                float(suma_gi_ordenes),
            ],
            "porcentajes": [pct_mp_global, pct_mo_global, pct_gi_global],
            "colores": ["#1b5e43", "#1e4e79", "#b45309"],
        }
    )

    pastel_ordenes_json = json.dumps(
        {
            "labels": [f"{r['codigo']}" for r in rebanadas_pastel_ordenes],
            "valores": [float(r["importe"]) for r in rebanadas_pastel_ordenes],
            "porcentajes": [r["porcentaje"] for r in rebanadas_pastel_ordenes],
            "colores": [r["color"] for r in rebanadas_pastel_ordenes],
        }
    )

    context = {
        "pagina_activa": "resultados",
        "ordenes": ordenes,
        "materias": materias,
        "gastos_gi": gastos_gi,
        "terminados": terminados,
        "suma_mp_ordenes": suma_mp_ordenes,
        "suma_mo_ordenes": suma_mo_ordenes,
        "suma_primo_ordenes": suma_primo_ordenes,
        "suma_gi_ordenes": suma_gi_ordenes,
        "suma_total_ordenes": suma_total_ordenes,
        "inv_inicial_mp": inv_inicial_mp,
        "compras_mp": compras_mp,
        "mp_disponible": mp_disponible,
        "mp_utilizada": mp_utilizada,
        "mp_final_saldo": mp_final_saldo,
        "total_en_proceso": total_en_proceso,
        "total_terminado": total_terminado,
        "datos_grafica_ordenes": datos_grafica_ordenes,
        "pct_mp_global": pct_mp_global,
        "pct_mo_global": pct_mo_global,
        "pct_gi_global": pct_gi_global,
        "rebanadas_pastel_ordenes": rebanadas_pastel_ordenes,
        "pastel_elementos_json": pastel_elementos_json,
        "pastel_ordenes_json": pastel_ordenes_json,
        **_obtener_contexto_base(),
    }
    return render(request, "contabilidad/resultados.html", context)


def exportar_resultados_excel_view(request):
    """
    Exporta el Apartado de Resultados a un archivo Excel (.xlsx) nativo
    incluyendo tablas contables formateadas y gráficas nativas de Excel
    (Gráfica de Pastel de Elementos del Costo, Gráfica de Pastel por Orden y Gráfica de Barras).
    """
    ordenes = list(OrdenProduccion.objects.all())
    materias = list(MateriaPrima.objects.all())

    wb = Workbook()
    ws = wb.active
    ws.title = "Resultados y Graficas"

    # Estilos contables limpios
    font_titulo = Font(name="Calibri", size=14, bold=True, color="161E19")
    font_subtitulo = Font(name="Calibri", size=11, bold=True, color="1B5E43")
    font_header = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    font_total = Font(name="Calibri", size=10, bold=True, color="161E19")
    font_normal = Font(name="Calibri", size=10, color="161E19")

    fill_header = PatternFill(start_color="1B5E43", end_color="1B5E43", fill_type="solid")
    fill_sub = PatternFill(start_color="1E4E79", end_color="1E4E79", fill_type="solid")
    fill_total = PatternFill(start_color="EEF1EE", end_color="EEF1EE", fill_type="solid")

    borde_fino = Side(border_style="thin", color="D5DDD7")
    borde_doble = Side(border_style="double", color="161E19")
    border_celda = Border(left=borde_fino, right=borde_fino, top=borde_fino, bottom=borde_fino)
    border_total = Border(top=borde_fino, bottom=borde_doble, left=borde_fino, right=borde_fino)

    formato_moneda = "$#,##0.00"
    formato_numero = "#,##0.00"

    # Título del reporte
    ws["A1"] = "REPORTE DE RESULTADOS: COSTOS POR ORDEN DE PRODUCCIÓN Y MATERIA PRIMA FINAL"
    ws["A1"].font = font_titulo
    ws["A2"] = f"Fecha de generación: {timezone.localdate().strftime('%Y-%m-%d')}"
    ws["A2"].font = Font(name="Calibri", size=9, italic=True, color="526057")

    # =========================================================================
    # TABLA 1: COSTOS DETALLADOS POR ORDEN DE PRODUCCIÓN
    # =========================================================================
    ws["A4"] = "1. Costos por Orden de Producción"
    ws["A4"].font = font_subtitulo

    headers_op = [
        "Orden (O.P.)",
        "Artículo / Producto",
        "Estado",
        "Unidades",
        "Materia Prima (M.P.)",
        "Mano de Obra (M.O.)",
        "Costo Primo",
        "Gastos Indirectos (G.I.)",
        "Costo Total Orden",
        "Costo Unitario",
    ]
    fila_header_op = 5
    for col_idx, text in enumerate(headers_op, start=1):
        cell = ws.cell(row=fila_header_op, column=col_idx, value=text)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border_celda

    fila_inicio_op = 6
    fila_actual = fila_inicio_op

    suma_mp = Decimal("0.00")
    suma_mo = Decimal("0.00")
    suma_primo = Decimal("0.00")
    suma_gi = Decimal("0.00")
    suma_total = Decimal("0.00")

    if ordenes:
        for op in ordenes:
            ws.cell(row=fila_actual, column=1, value=op.codigo).alignment = Alignment(horizontal="center")
            ws.cell(row=fila_actual, column=2, value=op.producto_terminado)
            ws.cell(row=fila_actual, column=3, value="Terminada" if op.estado == "TERMINADA" else "En Proceso").alignment = Alignment(horizontal="center")
            ws.cell(row=fila_actual, column=4, value=op.cantidad_unidades).number_format = "#,##0"
            ws.cell(row=fila_actual, column=5, value=float(op.total_mp)).number_format = formato_moneda
            ws.cell(row=fila_actual, column=6, value=float(op.total_mo)).number_format = formato_moneda
            ws.cell(row=fila_actual, column=7, value=float(op.costo_primo)).number_format = formato_moneda
            ws.cell(row=fila_actual, column=8, value=float(op.total_gi)).number_format = formato_moneda
            ws.cell(row=fila_actual, column=9, value=float(op.costo_total)).number_format = formato_moneda
            ws.cell(row=fila_actual, column=10, value=float(op.costo_unitario)).number_format = formato_moneda

            for c in range(1, 11):
                ws.cell(row=fila_actual, column=c).font = font_normal
                ws.cell(row=fila_actual, column=c).border = border_celda

            suma_mp += op.total_mp
            suma_mo += op.total_mo
            suma_primo += op.costo_primo
            suma_gi += op.total_gi
            suma_total += op.costo_total
            fila_actual += 1
    else:
        # Fila vacía representativa para que las fórmulas/gráficas no fallen si se exporta en blanco
        ws.cell(row=fila_actual, column=1, value="Sin órdenes")
        for c in range(2, 11):
            ws.cell(row=fila_actual, column=c, value=0)
        fila_actual += 1

    fila_fin_op = fila_actual - 1

    # Fila de Totales de Órdenes
    ws.cell(row=fila_actual, column=1, value="TOTALES").alignment = Alignment(horizontal="right")
    ws.merge_cells(start_row=fila_actual, start_column=1, end_row=fila_actual, end_column=4)
    ws.cell(row=fila_actual, column=5, value=float(suma_mp)).number_format = formato_moneda
    ws.cell(row=fila_actual, column=6, value=float(suma_mo)).number_format = formato_moneda
    ws.cell(row=fila_actual, column=7, value=float(suma_primo)).number_format = formato_moneda
    ws.cell(row=fila_actual, column=8, value=float(suma_gi)).number_format = formato_moneda
    ws.cell(row=fila_actual, column=9, value=float(suma_total)).number_format = formato_moneda
    ws.cell(row=fila_actual, column=10, value="—").alignment = Alignment(horizontal="center")

    for c in range(1, 11):
        cell = ws.cell(row=fila_actual, column=c)
        cell.font = font_total
        cell.fill = fill_total
        cell.border = border_total

    # =========================================================================
    # TABLA 2: RESUMEN POR ELEMENTO DEL COSTO (FUENTE DE LA GRÁFICA DE PASTEL)
    # =========================================================================
    fila_elem_titulo = fila_actual + 3
    ws.cell(row=fila_elem_titulo, column=1, value="2. Distribución por Elemento del Costo").font = font_subtitulo

    fila_elem_header = fila_elem_titulo + 1
    ws.cell(row=fila_elem_header, column=1, value="Elemento del Costo").font = font_header
    ws.cell(row=fila_elem_header, column=1).fill = fill_sub
    ws.cell(row=fila_elem_header, column=2, value="Importe Acumulado ($)").font = font_header
    ws.cell(row=fila_elem_header, column=2).fill = fill_sub

    elementos_data = [
        ("Materia Prima Directa (M.P.)", float(suma_mp)),
        ("Mano de Obra Directa (M.O.)", float(suma_mo)),
        ("Gastos Indirectos (G.I.)", float(suma_gi)),
    ]
    fila_elem_inicio = fila_elem_header + 1
    for i, (nombre_el, valor_el) in enumerate(elementos_data):
        r = fila_elem_inicio + i
        c1 = ws.cell(row=r, column=1, value=nombre_el)
        c2 = ws.cell(row=r, column=2, value=valor_el)
        c1.font = font_normal
        c2.font = font_normal
        c2.number_format = formato_moneda
        c1.border = border_celda
        c2.border = border_celda
    fila_elem_fin = fila_elem_inicio + 2

    # =========================================================================
    # TABLA 3: MATERIA PRIMA FINAL (TARJETAS DE ALMACÉN)
    # =========================================================================
    fila_mp_titulo = fila_elem_fin + 3
    ws.cell(row=fila_mp_titulo, column=1, value="3. Saldo Final de Materia Prima en Almacén").font = font_subtitulo

    headers_mp = [
        "Código Tarjeta",
        "Materia Prima",
        "Unidad",
        "Entradas Totales",
        "Salidas a Producción",
        "Existencia Final",
        "Costo Promedio Final",
        "Saldo M. Prima Final ($)",
    ]
    fila_mp_header = fila_mp_titulo + 1
    for col_idx, text in enumerate(headers_mp, start=1):
        cell = ws.cell(row=fila_mp_header, column=col_idx, value=text)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border_celda

    fila_mp_actual = fila_mp_header + 1
    mp_final_saldo = Decimal("0.00")
    for m in materias:
        ws.cell(row=fila_mp_actual, column=1, value=m.codigo).alignment = Alignment(horizontal="center")
        ws.cell(row=fila_mp_actual, column=2, value=m.nombre)
        ws.cell(row=fila_mp_actual, column=3, value=m.unidad_medida).alignment = Alignment(horizontal="center")
        ws.cell(row=fila_mp_actual, column=4, value=float(m.total_entradas_unidades)).number_format = formato_numero
        ws.cell(row=fila_mp_actual, column=5, value=float(m.total_salidas_unidades)).number_format = formato_numero
        ws.cell(row=fila_mp_actual, column=6, value=float(m.existencia_actual)).number_format = formato_numero
        ws.cell(row=fila_mp_actual, column=7, value=float(m.costo_promedio_actual)).number_format = formato_moneda
        ws.cell(row=fila_mp_actual, column=8, value=float(m.saldo_actual)).number_format = formato_moneda
        for c in range(1, 9):
            ws.cell(row=fila_mp_actual, column=c).font = font_normal
            ws.cell(row=fila_mp_actual, column=c).border = border_celda
        mp_final_saldo += m.saldo_actual
        fila_mp_actual += 1

    ws.cell(row=fila_mp_actual, column=1, value="GRAN TOTAL DE MATERIA PRIMA FINAL").alignment = Alignment(horizontal="right")
    ws.merge_cells(start_row=fila_mp_actual, start_column=1, end_row=fila_mp_actual, end_column=7)
    ws.cell(row=fila_mp_actual, column=8, value=float(mp_final_saldo)).number_format = formato_moneda
    for c in range(1, 9):
        cell = ws.cell(row=fila_mp_actual, column=c)
        cell.font = font_total
        cell.fill = fill_total
        cell.border = border_total

    # =========================================================================
    # GRÁFICAS NATIVAS DE EXCEL (PASTEL Y BARRAS)
    # =========================================================================
    # 1. Gráfica de Pastel: Proporción por Elemento del Costo (M.P., M.O., G.I.)
    pie_elem = PieChart()
    pie_elem.title = "Distribución por Elemento del Costo (M.P., M.O., G.I.)"
    labels_elem = Reference(ws, min_col=1, min_row=fila_elem_inicio, max_row=fila_elem_fin)
    data_elem = Reference(ws, min_col=2, min_row=fila_elem_header, max_row=fila_elem_fin)
    pie_elem.add_data(data_elem, titles_from_data=True)
    pie_elem.set_categories(labels_elem)
    pie_elem.dataLabels = DataLabelList()
    pie_elem.dataLabels.showPercent = True
    pie_elem.dataLabels.showVal = True
    pie_elem.width = 14
    pie_elem.height = 10
    ws.add_chart(pie_elem, f"D{fila_elem_titulo}")

    # 2. Gráfica de Pastel: Costo Total por Orden de Producción
    pie_op = PieChart()
    pie_op.title = "Participación en el Costo por Orden de Producción"
    labels_op = Reference(ws, min_col=1, min_row=fila_inicio_op, max_row=fila_fin_op)
    data_op_total = Reference(ws, min_col=9, min_row=fila_header_op, max_row=fila_fin_op)
    pie_op.add_data(data_op_total, titles_from_data=True)
    pie_op.set_categories(labels_op)
    pie_op.dataLabels = DataLabelList()
    pie_op.dataLabels.showPercent = True
    pie_op.width = 14
    pie_op.height = 10
    ws.add_chart(pie_op, "L4")

    # 3. Gráfica de Barras: Desglose MP, MO y GI por Orden de Producción
    bar_op = BarChart()
    bar_op.type = "col"
    bar_op.style = 10
    bar_op.grouping = "stacked"
    bar_op.overlap = 100
    bar_op.title = "Composición de Costos por Orden (MP + MO + GI)"
    bar_op.y_axis.title = "Importe ($)"
    bar_op.x_axis.title = "Orden de Producción"

    data_mp_mo = Reference(ws, min_col=5, max_col=6, min_row=fila_header_op, max_row=fila_fin_op)
    data_gi_ref = Reference(ws, min_col=8, max_col=8, min_row=fila_header_op, max_row=fila_fin_op)
    bar_op.add_data(data_mp_mo, titles_from_data=True)
    bar_op.add_data(data_gi_ref, titles_from_data=True)
    bar_op.set_categories(labels_op)
    bar_op.width = 15
    bar_op.height = 10
    ws.add_chart(bar_op, f"L{fila_mp_titulo}")

    # Ajustar ancho de columnas automáticamente
    anchos = [16, 36, 14, 12, 18, 18, 16, 20, 18, 16]
    for idx, ancho in enumerate(anchos, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = ancho

    # =========================================================================
    # HOJA 2: DETALLE DE KARDEX (TARJETAS DE ALMACÉN)
    # =========================================================================
    ws_kardex = wb.create_sheet(title="Tarjetas de Almacen")
    ws_kardex["A1"] = "MOVIMIENTOS EN TARJETAS DE ALMACÉN DE MATERIA PRIMA"
    ws_kardex["A1"].font = font_titulo

    headers_kardex = [
        "Tarjeta MP",
        "Insumo",
        "Fecha",
        "Referencia",
        "Tipo de Movimiento",
        "Orden Afectada",
        "Entrada (Uds)",
        "Salida (Uds)",
        "Existencia",
        "Costo Unitario ($)",
        "Costo Promedio ($)",
        "Debe ($)",
        "Haber ($)",
        "Saldo ($)",
    ]
    for col_idx, text in enumerate(headers_kardex, start=1):
        c = ws_kardex.cell(row=3, column=col_idx, value=text)
        c.font = font_header
        c.fill = fill_header
        c.border = border_celda

    fila_k = 4
    for mov in MovimientoAlmacenMP.objects.select_related("materia_prima", "orden_produccion").order_by("materia_prima__codigo", "fecha", "id"):
        ws_kardex.cell(row=fila_k, column=1, value=mov.materia_prima.codigo)
        ws_kardex.cell(row=fila_k, column=2, value=mov.materia_prima.nombre)
        ws_kardex.cell(row=fila_k, column=3, value=mov.fecha.strftime("%Y-%m-%d"))
        ws_kardex.cell(row=fila_k, column=4, value=mov.referencia)
        ws_kardex.cell(row=fila_k, column=5, value=mov.get_tipo_movimiento_display())
        ws_kardex.cell(row=fila_k, column=6, value=mov.orden_produccion.codigo if mov.orden_produccion else "—")
        ws_kardex.cell(row=fila_k, column=7, value=float(mov.unidades_entrada)).number_format = formato_numero
        ws_kardex.cell(row=fila_k, column=8, value=float(mov.unidades_salida)).number_format = formato_numero
        ws_kardex.cell(row=fila_k, column=9, value=float(mov.existencia)).number_format = formato_numero
        ws_kardex.cell(row=fila_k, column=10, value=float(mov.costo_unitario)).number_format = formato_moneda
        ws_kardex.cell(row=fila_k, column=11, value=float(mov.costo_promedio)).number_format = formato_moneda
        ws_kardex.cell(row=fila_k, column=12, value=float(mov.debe)).number_format = formato_moneda
        ws_kardex.cell(row=fila_k, column=13, value=float(mov.haber)).number_format = formato_moneda
        ws_kardex.cell(row=fila_k, column=14, value=float(mov.saldo)).number_format = formato_moneda
        for col_i in range(1, 15):
            ws_kardex.cell(row=fila_k, column=col_i).font = font_normal
            ws_kardex.cell(row=fila_k, column=col_i).border = border_celda
        fila_k += 1

    for col_idx in range(1, 15):
        ws_kardex.column_dimensions[get_column_letter(col_idx)].width = 16

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    filename = f"resultados_contabilidad_{timezone.localdate().strftime('%Y-%m-%d')}.xlsx"
    response = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# =============================================================================
# VISTAS PARA BORRAR CAPTURAS O TARJETAS INDIVIDUALES
# =============================================================================

def eliminar_materia_prima_view(request, pk):
    """Borra una Tarjeta de Almacén de Materia Prima completa."""
    if request.method == "POST":
        mp = get_object_or_404(MateriaPrima, pk=pk)
        nombre = f"{mp.codigo} — {mp.nombre}"
        mp.delete()
        messages.info(request, f"Tarjeta de almacén '{nombre}' eliminada.")
    return redirect(request.POST.get("next") or "tarjetas_almacen")


def eliminar_movimiento_mp_view(request, pk):
    """Borra un movimiento individual de almacén (captura) y recalcula el kardex de la tarjeta."""
    if request.method == "POST":
        mov = get_object_or_404(MovimientoAlmacenMP, pk=pk)
        mp = mov.materia_prima
        ref = mov.referencia
        mov.delete()
        mp.recalcular_kardex()
        messages.info(request, f"Movimiento '{ref}' eliminado. Se recalculó el saldo de {mp.codigo}.")
    return redirect(request.POST.get("next") or f"/tarjetas-almacen/?mp={mp.id}")


def eliminar_orden_produccion_view(request, pk):
    """Borra una Orden de Producción y recalcula las tarjetas de materia prima si tenían bajas hacia ella."""
    if request.method == "POST":
        op = get_object_or_404(OrdenProduccion, pk=pk)
        codigo = op.codigo
        materias_afectadas = list(
            MateriaPrima.objects.filter(movimientos__orden_produccion=op).distinct()
        )
        op.delete()
        for m in materias_afectadas:
            m.recalcular_kardex()
        messages.info(request, f"Orden de producción '{codigo}' eliminada.")
    return redirect(request.POST.get("next") or "ordenes_produccion")


def eliminar_mano_obra_view(request, pk):
    """Borra una captura de Mano de Obra Directa."""
    if request.method == "POST":
        mo = get_object_or_404(ManoObraDirecta, pk=pk)
        op_id = mo.orden_produccion_id
        ref = mo.referencia_nomina
        mo.delete()
        messages.info(request, f"Captura de mano de obra '{ref}' eliminada.")
        return redirect(request.POST.get("next") or f"/ordenes-produccion/?op={op_id}")
    return redirect("ordenes_produccion")


def eliminar_gasto_indirecto_view(request, pk):
    """Borra un Gasto Indirecto capturado."""
    if request.method == "POST":
        gi = get_object_or_404(GastoIndirecto, pk=pk)
        ref = gi.codigo_referencia
        gi.delete()
        messages.info(request, f"Gasto indirecto '{ref}' eliminado.")
    return redirect(request.POST.get("next") or "costos_indirectos")


def eliminar_prorrateo_view(request, pk):
    """Borra un Prorrateo aplicado y devuelve los Gastos Indirectos a estado pendiente."""
    if request.method == "POST":
        pr = get_object_or_404(ProrrateoGI, pk=pk)
        codigo = pr.codigo
        pr.delete()
        if not ProrrateoGI.objects.exists():
            GastoIndirecto.objects.all().update(prorrateado=False)
        messages.info(request, f"Prorrateo '{codigo}' eliminado.")
    return redirect(request.POST.get("next") or "costos_indirectos")


def eliminar_articulo_terminado_view(request, pk):
    """Borra un registro de Artículo Terminado y regresa la Orden de Producción a estado En Proceso."""
    if request.method == "POST":
        art = get_object_or_404(ArticuloTerminado, pk=pk)
        op = art.orden_produccion
        folio = art.codigo_entrada
        art.delete()
        op.estado = "EN_PROCESO"
        op.fecha_cierre = None
        op.save()
        messages.info(
            request,
            f"Registro de artículo terminado '{folio}' eliminado. La orden {op.codigo} volvió a estado En proceso.",
        )
    return redirect(request.POST.get("next") or "articulos_terminados")


def cargar_ejemplo_pizarron_view(request):
    """Carga los datos preguardados del ejercicio completo."""
    if request.method != "POST":
        return redirect("captura")

    with transaction.atomic():
        ArticuloTerminado.objects.all().delete()
        ProrrateoOrdenDetalle.objects.all().delete()
        ProrrateoGI.objects.all().delete()
        GastoIndirecto.objects.all().delete()
        ManoObraDirecta.objects.all().delete()
        MovimientoAlmacenMP.objects.all().delete()
        OrdenProduccion.objects.all().delete()
        MateriaPrima.objects.all().delete()

        fecha_base = timezone.localdate()

        mp_madera = MateriaPrima.objects.create(
            codigo="MP-001",
            nombre="Madera de Cedro Industrial",
            unidad_medida="PZA",
            stock_minimo=Decimal("50.00"),
            email_proveedor="suministros@maderasdelnorte.mx",
            observaciones="Tarjeta principal de materia prima directa para órdenes 1050, 1051 y 1052.",
        )
        mp_acero = MateriaPrima.objects.create(
            codigo="MP-002",
            nombre="Perfil Tubular de Acero Calibre 18",
            unidad_medida="MT",
            stock_minimo=Decimal("80.00"),
            email_proveedor="ventas@acerosindustriales.mx",
            observaciones="Estructura metálica de soporte.",
        )

        op_1050 = OrdenProduccion.objects.create(
            codigo="OP-1050",
            producto_terminado="Mesa Ejecutiva de Juntas Mod. 1050",
            fecha_apertura=fecha_base,
            cantidad_unidades=50,
            email_supervisor="jefe.planta@fabrica.mx",
            observaciones="Lote inicial prioritario.",
        )
        op_1051 = OrdenProduccion.objects.create(
            codigo="OP-1051",
            producto_terminado="Estación Operativa Modular Mod. 1051",
            fecha_apertura=fecha_base,
            cantidad_unidades=80,
            email_supervisor="jefe.planta@fabrica.mx",
            observaciones="Segunda orden de producción en proceso.",
        )
        op_1052 = OrdenProduccion.objects.create(
            codigo="OP-1052",
            producto_terminado="Librero Corporativo Reforzado Mod. 1052",
            fecha_apertura=fecha_base,
            cantidad_unidades=40,
            email_supervisor="jefe.planta@fabrica.mx",
            observaciones="Tercera orden de producción en proceso.",
        )

        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_madera,
            fecha=fecha_base,
            tipo_movimiento="SALDO_INICIAL",
            referencia="INV-INI-01",
            unidades_entrada=Decimal("500.00"),
            costo_unitario=Decimal("100.00"),
            observaciones="Inventario inicial en almacén.",
        )
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_madera,
            fecha=fecha_base,
            tipo_movimiento="COMPRA",
            referencia="FAC-2010",
            unidades_entrada=Decimal("300.00"),
            costo_unitario=Decimal("120.00"),
            observaciones="Compra de materia prima.",
        )
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_madera,
            orden_produccion=op_1050,
            fecha=fecha_base,
            tipo_movimiento="BAJA_PRODUCCION",
            referencia="REQ-1050-A",
            unidades_salida=Decimal("150.00"),
            costo_unitario=mp_madera.costo_promedio_actual,
            observaciones="Salida a producción para OP-1050.",
        )
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_madera,
            orden_produccion=op_1051,
            fecha=fecha_base,
            tipo_movimiento="BAJA_PRODUCCION",
            referencia="REQ-1051-A",
            unidades_salida=Decimal("200.00"),
            costo_unitario=mp_madera.costo_promedio_actual,
            observaciones="Salida a producción para OP-1051.",
        )
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_madera,
            orden_produccion=op_1052,
            fecha=fecha_base,
            tipo_movimiento="BAJA_PRODUCCION",
            referencia="REQ-1052-A",
            unidades_salida=Decimal("100.00"),
            costo_unitario=mp_madera.costo_promedio_actual,
            observaciones="Salida a producción para OP-1052.",
        )

        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_acero,
            fecha=fecha_base,
            tipo_movimiento="SALDO_INICIAL",
            referencia="INV-INI-02",
            unidades_entrada=Decimal("400.00"),
            costo_unitario=Decimal("50.00"),
            observaciones="Inventario inicial de perfiles.",
        )
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_acero,
            fecha=fecha_base,
            tipo_movimiento="COMPRA",
            referencia="FAC-2015",
            unidades_entrada=Decimal("200.00"),
            costo_unitario=Decimal("65.00"),
            observaciones="Compra de perfil tubular.",
        )
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_acero,
            orden_produccion=op_1050,
            fecha=fecha_base,
            tipo_movimiento="BAJA_PRODUCCION",
            referencia="REQ-1050-B",
            unidades_salida=Decimal("120.00"),
            costo_unitario=mp_acero.costo_promedio_actual,
            observaciones="Perfiles enviados a OP-1050.",
        )
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_acero,
            orden_produccion=op_1051,
            fecha=fecha_base,
            tipo_movimiento="BAJA_PRODUCCION",
            referencia="REQ-1051-B",
            unidades_salida=Decimal("180.00"),
            costo_unitario=mp_acero.costo_promedio_actual,
            observaciones="Perfiles enviados a OP-1051.",
        )

        ManoObraDirecta.objects.create(
            orden_produccion=op_1050,
            fecha=fecha_base,
            referencia_nomina="NOM-1050",
            operario_departamento="Cuadrilla Carpintería A",
            horas_trabajadas=Decimal("100.00"),
            cuota_por_hora=Decimal("100.00"),
            importe_total=Decimal("10000.00"),
            observaciones="Pago de nómina operarios OP-1050.",
        )
        ManoObraDirecta.objects.create(
            orden_produccion=op_1051,
            fecha=fecha_base,
            referencia_nomina="NOM-1051",
            operario_departamento="Cuadrilla Estructuras B",
            horas_trabajadas=Decimal("200.00"),
            cuota_por_hora=Decimal("100.00"),
            importe_total=Decimal("20000.00"),
            observaciones="Pago de nómina operarios OP-1051.",
        )
        ManoObraDirecta.objects.create(
            orden_produccion=op_1052,
            fecha=fecha_base,
            referencia_nomina="NOM-1052",
            operario_departamento="Cuadrilla Ensamble C",
            horas_trabajadas=Decimal("80.00"),
            cuota_por_hora=Decimal("100.00"),
            importe_total=Decimal("8000.00"),
            observaciones="Pago de nómina operarios OP-1052.",
        )

        GastoIndirecto.objects.create(
            codigo_referencia="GI-MPI-01",
            fecha=fecha_base,
            categoria="MPI",
            concepto="Materia Prima Indirecta (Pegamento, lija, tornillería y barniz)",
            importe=Decimal("6500.00"),
        )
        GastoIndirecto.objects.create(
            codigo_referencia="GI-SYS-02",
            fecha=fecha_base,
            categoria="SYS_SUPERVISORES",
            concepto="Sueldos y salarios de supervisores de producción",
            importe=Decimal("12000.00"),
        )
        GastoIndirecto.objects.create(
            codigo_referencia="GI-LUZ-03",
            fecha=fecha_base,
            categoria="ENERGIA_LUZ",
            concepto="Energía eléctrica de planta",
            importe=Decimal("4500.00"),
        )

        sim = _calcular_simulacion_prorrateo("COSTO_PRIMO", solo_pendientes=True)
        prorrateo = ProrrateoGI.objects.create(
            codigo="PRORRATEO-01",
            fecha=fecha_base,
            base_seleccionada="COSTO_PRIMO",
            total_gastos_indirectos=sim["total_gi"],
            total_base=sim["total_base"],
            factor_prorrateo=sim["factor"],
            observaciones="Prorrateo sobre base Costo Primo.",
        )
        for fila in sim["filas"]:
            ProrrateoOrdenDetalle.objects.create(
                prorrateo=prorrateo,
                orden_produccion=fila["orden"],
                valor_base_orden=fila["valor_base"],
                factor_aplicado=fila["factor"],
                porcentaje_participacion=fila["porcentaje"],
                importe_asignado=fila["importe_asignado"],
            )
        GastoIndirecto.objects.all().update(prorrateado=True)

        op_1050.refresh_from_db()
        ArticuloTerminado.objects.create(
            orden_produccion=op_1050,
            codigo_entrada="AT-1050",
            fecha_entrada=fecha_base,
            nombre_articulo=op_1050.producto_terminado,
            unidades_recibidas=op_1050.cantidad_unidades,
            costo_mp=op_1050.total_mp,
            costo_mo=op_1050.total_mo,
            costo_gi=op_1050.total_gi,
            costo_total=op_1050.costo_total,
            costo_unitario=op_1050.costo_unitario,
            observaciones="Lote concluido.",
        )
        op_1050.estado = "TERMINADA"
        op_1050.fecha_cierre = fecha_base
        op_1050.save()

    messages.success(
        request,
        "Datos preguardados cargados correctamente.",
    )
    return redirect("resultados")


def limpiar_datos_view(request):
    """Elimina todos los registros y deja la aplicación vacía por defecto."""
    if request.method == "POST":
        with transaction.atomic():
            ArticuloTerminado.objects.all().delete()
            ProrrateoOrdenDetalle.objects.all().delete()
            ProrrateoGI.objects.all().delete()
            GastoIndirecto.objects.all().delete()
            ManoObraDirecta.objects.all().delete()
            MovimientoAlmacenMP.objects.all().delete()
            OrdenProduccion.objects.all().delete()
            MateriaPrima.objects.all().delete()
        messages.info(request, "Todos los registros fueron eliminados.")
    return redirect("captura")
