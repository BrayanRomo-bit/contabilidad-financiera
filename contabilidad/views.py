import json
from decimal import Decimal, ROUND_HALF_UP
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import redirect, render
from django.utils import timezone

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
    """Retorna métricas globales para la barra superior y paneles de resumen."""
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
    """
    Centro Integral de Captura:
    Por defecto inicia vacío esperando que el usuario cree su información,
    con la opción disponible de cargar los datos preguardados.
    """
    tab_activa = request.GET.get("tab", "mp_alta")

    form_mp_catalogo = MateriaPrimaCatalogoForm(prefix="cat_mp")
    form_mp_alta = AltaMPMovimientoForm(prefix="alta_mp")
    form_mp_baja = BajaMPProduccionForm(prefix="baja_mp")
    form_op_alta = OrdenProduccionForm(prefix="alta_op")
    form_mo = ManoObraDirectaForm(prefix="mo")
    form_gi = GastoIndirectoForm(prefix="gi")
    form_baja_prod = BajaProduccionTerminadaForm(prefix="baja_prod")

    if request.method == "POST":
        tipo_captura = request.POST.get("tipo_captura", "")
        tab_activa = tipo_captura or tab_activa

        # 1. Crear Nueva Tarjeta de Materia Prima (+ Saldo Inicial opcional)
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
                            observaciones="Inventario inicial registrado al abrir la tarjeta de almacén.",
                        )
                messages.success(
                    request,
                    f"Tarjeta de Almacén '{mp.codigo} - {mp.nombre}' registrada y normalizada correctamente.",
                )
                return redirect("/?tab=mp_catalogo")
            else:
                messages.error(
                    request,
                    "Por favor corrige los errores señalados en el formulario de Nueva Materia Prima.",
                )

        # 2. M.P. - Alta (Saldo Inicial o Compra)
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
                    f"Alta en Tarjeta de Almacén ({mov.materia_prima.codigo}) por {mov.unidades_entrada} {mov.materia_prima.unidad_medida} (${mov.debe:,.2f}) registrada con éxito.",
                )
                return redirect("/?tab=mp_alta")
            else:
                messages.error(
                    request,
                    "Error de validación en Alta de Materia Prima. Verifica los campos resaltados.",
                )

        # 3. Baja de M.P. a Producción en Proceso (Afecta Orden de Producción)
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
                    f"Baja de Almacén registrada: {mov.unidades_salida} {mp.unidad_medida} de {mp.codigo} cargadas a la Orden {cd['orden_produccion'].codigo} por ${mov.haber:,.2f}.",
                )
                return redirect("/?tab=mp_baja")
            else:
                messages.error(
                    request,
                    "No se pudo procesar la Baja a Producción. Revisa las existencias o los campos obligatorios.",
                )

        # 4. Alta de Orden de Producción (O.P.)
        elif tipo_captura == "op_alta":
            form_op_alta = OrdenProduccionForm(request.POST, prefix="alta_op")
            if form_op_alta.is_valid():
                op = form_op_alta.save()
                messages.success(
                    request,
                    f"Orden de Producción '{op.codigo}' ({op.producto_terminado} - {op.cantidad_unidades} uds.) dada de alta exitosamente.",
                )
                return redirect("/?tab=op_alta")
            else:
                messages.error(
                    request,
                    "Error al dar de alta la Orden de Producción. Verifica los datos ingresados.",
                )

        # 5. Captura de Mano de Obra Directa (M.O. - Pago de nómina a operarios)
        elif tipo_captura == "mo_captura":
            form_mo = ManoObraDirectaForm(request.POST, prefix="mo")
            if form_mo.is_valid():
                mo = form_mo.save()
                messages.success(
                    request,
                    f"Mano de Obra Directa (${mo.importe_total:,.2f}) cargada a la Orden '{mo.orden_produccion.codigo}' correctamente.",
                )
                return redirect("/?tab=mo_captura")
            else:
                messages.error(
                    request,
                    "Error al registrar Mano de Obra Directa. Verifica los importes y campos obligatorios.",
                )

        # 6. Captura de Gastos Indirectos (G.I. - MPI, S y S de supervisores, luz, etc.)
        elif tipo_captura == "gi_captura":
            form_gi = GastoIndirectoForm(request.POST, prefix="gi")
            if form_gi.is_valid():
                gi = form_gi.save()
                messages.success(
                    request,
                    f"Gasto Indirecto '{gi.codigo_referencia}' ({gi.get_categoria_display()}: ${gi.importe:,.2f}) registrado para prorrateo.",
                )
                return redirect("/?tab=gi_captura")
            else:
                messages.error(
                    request,
                    "Error al registrar el Gasto Indirecto. Revisa el folio y el importe.",
                )

        # 7. Baja de Producción (Terminar Orden y enviar a Artículos Terminados)
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
                    f"Orden '{op.codigo}' concluida (Baja de Producción) y transferida a Artículos Terminados con folio '{art.codigo_entrada}' (Costo Total: ${art.costo_total:,.2f} | Unitario: ${art.costo_unitario:,.2f}).",
                )
                return redirect("/?tab=baja_prod")
            else:
                messages.error(
                    request,
                    "Error al registrar la Baja de Producción a Artículo Terminado.",
                )

    context = {
        "pagina_activa": "captura",
        "tab_activa": tab_activa,
        "form_mp_catalogo": form_mp_catalogo,
        "form_mp_alta": form_mp_alta,
        "form_mp_baja": form_mp_baja,
        "form_op_alta": form_op_alta,
        "form_mo": form_mo,
        "form_gi": form_gi,
        "form_baja_prod": form_baja_prod,
        "ultimos_movimientos_mp": MovimientoAlmacenMP.objects.select_related(
            "materia_prima", "orden_produccion"
        ).order_by("-id")[:6],
        "ultimos_cargos_mo": ManoObraDirecta.objects.select_related(
            "orden_produccion"
        ).order_by("-id")[:5],
        "ultimos_gastos_gi": GastoIndirecto.objects.order_by("-id")[:5],
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
    """Módulo de Órdenes de Producción (ej. OP-1050, OP-1051, OP-1052)."""
    ordenes = OrdenProduccion.objects.prefetch_related(
        "consumos_mp__materia_prima",
        "cargos_mo",
        "prorrateos_gi__prorrateo",
    ).all()
    orden_id = request.GET.get("op")
    orden_activa = None
    if orden_id:
        orden_activa = ordenes.filter(pk=orden_id).first()
    if not orden_activa and ordenes.exists():
        orden_activa = ordenes.first()

    context = {
        "pagina_activa": "ordenes",
        "ordenes": ordenes,
        "orden_activa": orden_activa,
        **_obtener_contexto_base(),
    }
    return render(request, "contabilidad/ordenes_produccion.html", context)


def _calcular_simulacion_prorrateo(base_seleccionada: str, solo_pendientes: bool):
    """Calcula cómo se dividirán los Gastos Indirectos entre las Órdenes de Producción."""
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
    """Módulo de Gastos Indirectos (Costos Indirectos) y Prorrateo."""
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
                    f"Gasto Indirecto '{gi.codigo_referencia}' ({gi.concepto} - ${gi.importe:,.2f}) agregado exitosamente.",
                )
                return redirect("costos_indirectos")
            else:
                messages.error(
                    request,
                    "Revisa los campos del formulario de Gasto Indirecto.",
                )

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
                        "No hay importe de Gastos Indirectos disponible para prorratear.",
                    )
                elif sim["total_base"] <= 0:
                    messages.error(
                        request,
                        "La base seleccionada tiene suma $0.00 en las órdenes de producción. Registra consumos de M.P. o M.O. antes de prorratear sobre esa base.",
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
                        f"Prorrateo '{prorrateo.codigo}' aplicado con éxito. Se distribuyeron ${prorrateo.total_gastos_indirectos:,.2f} (Factor: {prorrateo.factor_prorrateo}) hacia las Órdenes de Producción.",
                    )
                    return redirect("costos_indirectos")
            else:
                messages.error(
                    request,
                    "Error de validación en el formulario de Prorrateo.",
                )

    simulacion = _calcular_simulacion_prorrateo(base_preview, solo_pendientes_preview)
    gastos = GastoIndirecto.objects.all().order_by("-fecha", "-id")
    prorrateos_historicos = ProrrateoGI.objects.prefetch_related(
        "detalles__orden_produccion"
    ).all()

    resumen_categorias = (
        GastoIndirecto.objects.values("categoria")
        .annotate(total=Sum("importe"))
        .order_by("-total")
    )
    mapa_nombres_cat = dict(GastoIndirecto.CATEGORIAS_GI)
    desglose_categorias = [
        {
            "codigo": item["categoria"],
            "nombre": mapa_nombres_cat.get(item["categoria"], item["categoria"]),
            "total": item["total"],
        }
        for item in resumen_categorias
    ]

    context = {
        "pagina_activa": "costos_indirectos",
        "form_gi": form_gi,
        "form_prorrateo": form_prorrateo,
        "gastos": gastos,
        "desglose_categorias": desglose_categorias,
        "simulacion": simulacion,
        "prorrateos_historicos": prorrateos_historicos,
        "base_preview": base_preview,
        "solo_pendientes_preview": solo_pendientes_preview,
        **_obtener_contexto_base(),
    }
    return render(request, "contabilidad/costos_indirectos.html", context)


def articulos_terminados_view(request):
    """Módulo de Almacén de Artículos Terminados ("Art. terminado" / "Baja de prod.")."""
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
                f"Artículo Terminado '{art.codigo_entrada}' ingresado al almacén desde la Orden {op.codigo}.",
            )
            return redirect("articulos_terminados")
        else:
            messages.error(
                request,
                "Verifica los datos para transferir la Orden de Producción a Artículos Terminados.",
            )

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
    """
    Apartado de Resultados:
    "Resultado = Me arroja c/u de los costos de c/orden y la M. Prima final"
    Incluye:
    - Costo detallado de cada Orden de Producción (M.P., M.O., Costo Primo, G.I. Prorrateado, Costo Total y Unitario)
    - Inventario Final de Materia Prima (Unidades y Saldo Monetario Final de cada Tarjeta de Almacén)
    - Gráfica de barras por Orden y Gráfica de Pastel (distribución de costos por elemento y por orden)
    """
    ordenes = list(OrdenProduccion.objects.all())
    materias = list(MateriaPrima.objects.all())
    gastos_gi = list(GastoIndirecto.objects.all())
    terminados = list(ArticuloTerminado.objects.all())

    # Totales globales de Órdenes
    suma_mp_ordenes = redondear_moneda(sum((o.total_mp for o in ordenes), Decimal("0.00")))
    suma_mo_ordenes = redondear_moneda(sum((o.total_mo for o in ordenes), Decimal("0.00")))
    suma_primo_ordenes = redondear_moneda(sum((o.costo_primo for o in ordenes), Decimal("0.00")))
    suma_gi_ordenes = redondear_moneda(sum((o.total_gi for o in ordenes), Decimal("0.00")))
    suma_total_ordenes = redondear_moneda(sum((o.costo_total for o in ordenes), Decimal("0.00")))

    # Totales de Almacén de Materia Prima (Estado de M. Prima Final)
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

    # Producción en proceso vs Terminada
    total_en_proceso = redondear_moneda(
        sum((o.costo_total for o in ordenes if o.estado == "EN_PROCESO"), Decimal("0.00"))
    )
    total_terminado = redondear_moneda(
        sum((o.costo_total for o in ordenes if o.estado == "TERMINADA"), Decimal("0.00"))
    )

    # Datos para gráfica visual de barras (como en el pizarrón)
    max_costo_orden = max([float(o.costo_total) for o in ordenes], default=1.0)
    if max_costo_orden <= 0:
        max_costo_orden = 1.0

    paleta_colores = [
        "#2563eb",
        "#059669",
        "#d97706",
        "#7c3aed",
        "#db2777",
        "#0891b2",
        "#dc2626",
    ]

    datos_grafica_ordenes = []
    rebanadas_pastel_ordenes = []
    angulo_acum_ordenes = 0.0

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
        inicio_deg = round(angulo_acum_ordenes, 2)
        angulo_acum_ordenes += pct_del_total * 3.6
        fin_deg = min(round(angulo_acum_ordenes, 2), 360.0)
        rebanadas_pastel_ordenes.append(
            {
                "codigo": o.codigo,
                "producto": o.producto_terminado,
                "importe": o.costo_total,
                "porcentaje": pct_del_total,
                "color": color,
                "inicio_deg": inicio_deg,
                "fin_deg": fin_deg,
            }
        )

    # Datos para Gráfica de Pastel por Elementos del Costo (M.P., M.O., G.I.)
    if suma_total_ordenes > 0:
        pct_mp_global = round((float(suma_mp_ordenes) / float(suma_total_ordenes)) * 100, 2)
        pct_mo_global = round((float(suma_mo_ordenes) / float(suma_total_ordenes)) * 100, 2)
        pct_gi_global = round(100.0 - pct_mp_global - pct_mo_global, 2)
    else:
        pct_mp_global = 0.0
        pct_mo_global = 0.0
        pct_gi_global = 0.0

    deg_mp = round(pct_mp_global * 3.6, 2)
    deg_mo = round(deg_mp + (pct_mo_global * 3.6), 2)

    pastel_elementos_json = json.dumps(
        {
            "labels": [
                "Materia Prima Directa (M.P.)",
                "Mano de Obra Directa (M.O.)",
                "Gastos Indirectos (G.I.)",
            ],
            "valores": [
                float(suma_mp_ordenes),
                float(suma_mo_ordenes),
                float(suma_gi_ordenes),
            ],
            "porcentajes": [pct_mp_global, pct_mo_global, pct_gi_global],
            "colores": ["#2563eb", "#0891b2", "#f59e0b"],
        }
    )

    pastel_ordenes_json = json.dumps(
        {
            "labels": [f"{r['codigo']} ({r['producto']})" for r in rebanadas_pastel_ordenes],
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
        "deg_mp": deg_mp,
        "deg_mo": deg_mo,
        "rebanadas_pastel_ordenes": rebanadas_pastel_ordenes,
        "pastel_elementos_json": pastel_elementos_json,
        "pastel_ordenes_json": pastel_ordenes_json,
        **_obtener_contexto_base(),
    }
    return render(request, "contabilidad/resultados.html", context)


def cargar_ejemplo_pizarron_view(request):
    """
    Carga los datos preguardados que replican el ejercicio completo del pizarrón:
    - Tarjetas de Almacén de Materia Prima con Saldo Inicial, Compras y Bajas a Producción
    - Órdenes de Producción OP-1050, OP-1051 y OP-1052
    - Cargos de Mano de Obra Directa ($10,000, $20,000, $8,000)
    - Gastos Indirectos (M.P.I., S y S de supervisores, Luz) y su Prorrateo a las Órdenes
    - Orden OP-1050 enviada a Artículo Terminado
    """
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

        # 1. Crear Materias Primas (Tarjetas de Almacén)
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

        # 2. Alta de Órdenes de Producción (OP-1050, OP-1051, OP-1052)
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

        # 3. Movimientos en Tarjetas de Almacén
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_madera,
            fecha=fecha_base,
            tipo_movimiento="SALDO_INICIAL",
            referencia="INV-INI-01",
            unidades_entrada=Decimal("500.00"),
            costo_unitario=Decimal("100.00"),
            observaciones="Alta de Inventario Inicial en Almacén.",
        )
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_madera,
            fecha=fecha_base,
            tipo_movimiento="COMPRA",
            referencia="FAC-2010",
            unidades_entrada=Decimal("300.00"),
            costo_unitario=Decimal("120.00"),
            observaciones="Compra de materia prima a proveedor.",
        )
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_madera,
            orden_produccion=op_1050,
            fecha=fecha_base,
            tipo_movimiento="BAJA_PRODUCCION",
            referencia="REQ-1050-A",
            unidades_salida=Decimal("150.00"),
            costo_unitario=mp_madera.costo_promedio_actual,
            observaciones="Envío de madera al proceso para Orden 1050.",
        )
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_madera,
            orden_produccion=op_1051,
            fecha=fecha_base,
            tipo_movimiento="BAJA_PRODUCCION",
            referencia="REQ-1051-A",
            unidades_salida=Decimal("200.00"),
            costo_unitario=mp_madera.costo_promedio_actual,
            observaciones="Envío de madera al proceso para Orden 1051.",
        )
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_madera,
            orden_produccion=op_1052,
            fecha=fecha_base,
            tipo_movimiento="BAJA_PRODUCCION",
            referencia="REQ-1052-A",
            unidades_salida=Decimal("100.00"),
            costo_unitario=mp_madera.costo_promedio_actual,
            observaciones="Envío de madera al proceso para Orden 1052.",
        )

        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_acero,
            fecha=fecha_base,
            tipo_movimiento="SALDO_INICIAL",
            referencia="INV-INI-02",
            unidades_entrada=Decimal("400.00"),
            costo_unitario=Decimal("50.00"),
            observaciones="Inventario inicial de perfiles de acero.",
        )
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_acero,
            fecha=fecha_base,
            tipo_movimiento="COMPRA",
            referencia="FAC-2015",
            unidades_entrada=Decimal("200.00"),
            costo_unitario=Decimal("65.00"),
            observaciones="Compra adicional de perfil tubular.",
        )
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_acero,
            orden_produccion=op_1050,
            fecha=fecha_base,
            tipo_movimiento="BAJA_PRODUCCION",
            referencia="REQ-1050-B",
            unidades_salida=Decimal("120.00"),
            costo_unitario=mp_acero.costo_promedio_actual,
            observaciones="Perfiles enviados a Orden 1050.",
        )
        MovimientoAlmacenMP.objects.create(
            materia_prima=mp_acero,
            orden_produccion=op_1051,
            fecha=fecha_base,
            tipo_movimiento="BAJA_PRODUCCION",
            referencia="REQ-1051-B",
            unidades_salida=Decimal("180.00"),
            costo_unitario=mp_acero.costo_promedio_actual,
            observaciones="Perfiles enviados a Orden 1051.",
        )

        # 4. Mano de Obra Directa (M.O.)
        ManoObraDirecta.objects.create(
            orden_produccion=op_1050,
            fecha=fecha_base,
            referencia_nomina="NOM-1050",
            operario_departamento="Cuadrilla Carpintería y Ensamble A",
            horas_trabajadas=Decimal("100.00"),
            cuota_por_hora=Decimal("100.00"),
            importe_total=Decimal("10000.00"),
            observaciones="Pago de nómina a operarios asignados a OP-1050 ($10,000).",
        )
        ManoObraDirecta.objects.create(
            orden_produccion=op_1051,
            fecha=fecha_base,
            referencia_nomina="NOM-1051",
            operario_departamento="Cuadrilla Estructuras y Acabados B",
            horas_trabajadas=Decimal("200.00"),
            cuota_por_hora=Decimal("100.00"),
            importe_total=Decimal("20000.00"),
            observaciones="Pago de nómina a operarios asignados a OP-1051 ($20,000).",
        )
        ManoObraDirecta.objects.create(
            orden_produccion=op_1052,
            fecha=fecha_base,
            referencia_nomina="NOM-1052",
            operario_departamento="Cuadrilla Ensamble Especial C",
            horas_trabajadas=Decimal("80.00"),
            cuota_por_hora=Decimal("100.00"),
            importe_total=Decimal("8000.00"),
            observaciones="Pago de nómina a operarios asignados a OP-1052.",
        )

        # 5. Gastos Indirectos (G.I.)
        GastoIndirecto.objects.create(
            codigo_referencia="GI-MPI-01",
            fecha=fecha_base,
            categoria="MPI",
            concepto="Materia Prima Indirecta (Pegamento industrial, lija, tornillería y barniz)",
            importe=Decimal("6500.00"),
            observaciones="Consumo general de materiales indirectos en planta.",
        )
        GastoIndirecto.objects.create(
            codigo_referencia="GI-SYS-02",
            fecha=fecha_base,
            categoria="SYS_SUPERVISORES",
            concepto="Sueldos y Salarios de Supervisores de Producción",
            importe=Decimal("12000.00"),
            observaciones="Nómina de supervisión general de fábrica.",
        )
        GastoIndirecto.objects.create(
            codigo_referencia="GI-LUZ-03",
            fecha=fecha_base,
            categoria="ENERGIA_LUZ",
            concepto="Recibo de Energía Eléctrica (Luz de nave industrial)",
            importe=Decimal("4500.00"),
            observaciones="Bimestre productivo nave principal.",
        )

        # 6. Prorrateo de Gastos Indirectos
        sim = _calcular_simulacion_prorrateo("COSTO_PRIMO", solo_pendientes=True)
        prorrateo = ProrrateoGI.objects.create(
            codigo="PRORRATEO-01",
            fecha=fecha_base,
            base_seleccionada="COSTO_PRIMO",
            total_gastos_indirectos=sim["total_gi"],
            total_base=sim["total_base"],
            factor_prorrateo=sim["factor"],
            observaciones="Prorrateo automático de G.I. sobre base Costo Primo.",
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

        # 7. Terminar OP-1050 y enviarla a Artículo Terminado
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
            observaciones="Orden OP-1050 terminada e ingresada al Almacén de Artículos Terminados.",
        )
        op_1050.estado = "TERMINADA"
        op_1050.fecha_cierre = fecha_base
        op_1050.save()

    messages.success(
        request,
        "¡Datos preguardados cargados exitosamente! Ahora puedes explorar las Tarjetas de Almacén, Órdenes (1050, 1051, 1052), Costos Indirectos y el Apartado de Resultados.",
    )
    return redirect("resultados")


def limpiar_datos_view(request):
    """
    Elimina todos los registros y deja la aplicación completamente vacía
    (estado por defecto) esperando a que el usuario cree su propia información.
    """
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
        messages.info(
            request,
            "La base de datos ha quedado vacía (estado por defecto). Ya puedes comenzar a capturar tu propia información desde cero.",
        )
    return redirect("captura")

def limpiar_datos_view(request):
    """
    Elimina todos los registros y deja la aplicación completamente vacía
    (estado por defecto) esperando a que el usuario cree su propia información.
    """
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
        messages.info(
            request,
            "La base de datos ha quedado vacía (estado por defecto). Ya puedes comenzar a capturar tu propia información desde cero.",
        )
    return redirect("captura")

