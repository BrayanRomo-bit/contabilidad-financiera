from decimal import Decimal
from django import forms
from django.utils import timezone
from .models import (
    MateriaPrima,
    OrdenProduccion,
    MovimientoAlmacenMP,
    ManoObraDirecta,
    GastoIndirecto,
    ProrrateoGI,
    ArticuloTerminado,
    normalizar_codigo,
    normalizar_texto,
)


class DateInputISO(forms.DateInput):
    input_type = "date"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("format", "%Y-%m-%d")
        super().__init__(*args, **kwargs)


class MateriaPrimaCatalogoForm(forms.ModelForm):
    # Campos opcionales para registrar el Saldo Inicial de inmediato al crear la tarjeta
    registrar_saldo_inicial = forms.BooleanField(
        label="¿Registrar Inventario Inicial (Saldo) al abrir la tarjeta?",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input", "id": "chk_saldo_inicial"}),
    )
    fecha_inicial = forms.DateField(
        label="Fecha de Inventario Inicial (YYYY-MM-DD)",
        required=False,
        initial=timezone.localdate,
        widget=DateInputISO(attrs={"class": "form-control"}),
    )
    unidades_iniciales = forms.DecimalField(
        label="Unidades de Saldo Inicial",
        required=False,
        min_value=Decimal("0.00"),
        decimal_places=2,
        initial=Decimal("0.00"),
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "placeholder": "Ej. 500.00"}),
    )
    costo_unitario_inicial = forms.DecimalField(
        label="Costo Unitario Inicial ($)",
        required=False,
        min_value=Decimal("0.00"),
        decimal_places=2,
        initial=Decimal("0.00"),
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "placeholder": "Ej. 25.50"}),
    )

    class Meta:
        model = MateriaPrima
        fields = [
            "codigo",
            "nombre",
            "unidad_medida",
            "stock_minimo",
            "email_proveedor",
            "observaciones",
        ]
        widgets = {
            "codigo": forms.TextInput(
                attrs={
                    "class": "form-control text-uppercase",
                    "placeholder": "Ej. MP-001",
                    "autocomplete": "off",
                }
            ),
            "nombre": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Ej. Madera de Roble Estufada",
                }
            ),
            "unidad_medida": forms.Select(attrs={"class": "form-select"}),
            "stock_minimo": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0"}
            ),
            "email_proveedor": forms.EmailInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "contacto@proveedor.com (Opcional)",
                }
            ),
            "observaciones": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 2,
                    "placeholder": "Notas adicionales de almacenamiento o calidad (Opcional)",
                }
            ),
        }

    def clean_codigo(self):
        codigo = normalizar_codigo(self.cleaned_data.get("codigo", ""))
        if not codigo:
            raise forms.ValidationError("El código de Materia Prima es obligatorio.")
        qs = MateriaPrima.objects.filter(codigo=codigo)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(f"El código '{codigo}' ya está registrado en otra tarjeta.")
        return codigo

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("registrar_saldo_inicial"):
            unidades = cleaned.get("unidades_iniciales") or Decimal("0.00")
            costo = cleaned.get("costo_unitario_inicial") or Decimal("0.00")
            if unidades <= 0:
                self.add_error("unidades_iniciales", "Ingresa una cantidad mayor a 0 para el saldo inicial.")
            if costo <= 0:
                self.add_error("costo_unitario_inicial", "Ingresa un costo unitario mayor a 0 para el saldo inicial.")
            if not cleaned.get("fecha_inicial"):
                cleaned["fecha_inicial"] = timezone.localdate()
        return cleaned


class AltaMPMovimientoForm(forms.Form):
    """Formulario para MP - Alta -> Saldo Inicial o Compramos."""

    TIPOS_ALTA = [
        ("SALDO_INICIAL", "Saldo Inicial (Inventario Inicial)"),
        ("COMPRA", "Compra de Materia Prima (Entrada a Almacén)"),
    ]

    # Sección 1: Datos Generales
    materia_prima = forms.ModelChoiceField(
        label="Materia Prima (Tarjeta de Almacén)",
        queryset=MateriaPrima.objects.all(),
        widget=forms.Select(attrs={"class": "form-select", "id": "id_alta_mp_select"}),
    )
    tipo_movimiento = forms.ChoiceField(
        label="Subtipo de Alta",
        choices=TIPOS_ALTA,
        initial="COMPRA",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    fecha = forms.DateField(
        label="Fecha de Operación (YYYY-MM-DD)",
        initial=timezone.localdate,
        widget=DateInputISO(attrs={"class": "form-control"}),
    )
    referencia = forms.CharField(
        label="Folio / Factura / Referencia",
        max_length=40,
        widget=forms.TextInput(
            attrs={
                "class": "form-control text-uppercase",
                "placeholder": "Ej. FAC-104 o INV-INI",
            }
        ),
    )

    # Sección 2: Detalles Específicos
    unidades_entrada = forms.DecimalField(
        label="Cantidad de Unidades a Ingresar",
        min_value=Decimal("0.01"),
        decimal_places=2,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "step": "0.01",
                "min": "0.01",
                "placeholder": "Ej. 200.00",
                "id": "id_alta_mp_unidades",
            }
        ),
    )
    costo_unitario = forms.DecimalField(
        label="Costo Unitario de Compra / Inventario ($)",
        min_value=Decimal("0.01"),
        decimal_places=2,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "step": "0.01",
                "min": "0.01",
                "placeholder": "Ej. 45.00",
                "id": "id_alta_mp_costo",
            }
        ),
    )

    # Sección 3: Observaciones
    observaciones = forms.CharField(
        label="Observaciones (Opcional)",
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 2,
                "placeholder": "Proveedor, condiciones o notas de recepción...",
            }
        ),
    )

    def clean_referencia(self):
        return normalizar_codigo(self.cleaned_data.get("referencia", ""))

    def clean_observaciones(self):
        return normalizar_texto(self.cleaned_data.get("observaciones", ""))


class BajaMPProduccionForm(forms.Form):
    """Formulario para Baja producción en proceso -> Afectar a las órdenes correspondientes."""

    # Sección 1: Datos Generales
    materia_prima = forms.ModelChoiceField(
        label="Materia Prima a Retirar de Almacén",
        queryset=MateriaPrima.objects.all(),
        widget=forms.Select(attrs={"class": "form-select", "id": "id_baja_mp_select"}),
    )
    orden_produccion = forms.ModelChoiceField(
        label="Orden de Producción Destino (Se afectará con este costo)",
        queryset=OrdenProduccion.objects.filter(estado="EN_PROCESO"),
        widget=forms.Select(attrs={"class": "form-select", "id": "id_baja_mp_orden"}),
    )
    fecha = forms.DateField(
        label="Fecha de Salida a Producción (YYYY-MM-DD)",
        initial=timezone.localdate,
        widget=DateInputISO(attrs={"class": "form-control"}),
    )
    referencia = forms.CharField(
        label="Vale de Salida / Requisición",
        max_length=40,
        widget=forms.TextInput(
            attrs={
                "class": "form-control text-uppercase",
                "placeholder": "Ej. REQ-1050-01",
            }
        ),
    )

    # Sección 2: Detalles Específicos
    unidades_salida = forms.DecimalField(
        label="Cantidad de Unidades enviadas a Producción",
        min_value=Decimal("0.01"),
        decimal_places=2,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "step": "0.01",
                "min": "0.01",
                "placeholder": "Ej. 80.00",
                "id": "id_baja_mp_unidades",
            }
        ),
    )

    # Sección 3: Observaciones
    observaciones = forms.CharField(
        label="Observaciones de Entrega a Planta (Opcional)",
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 2,
                "placeholder": "Notas del consumo en proceso productivo...",
            }
        ),
    )

    def clean_referencia(self):
        return normalizar_codigo(self.cleaned_data.get("referencia", ""))

    def clean_observaciones(self):
        return normalizar_texto(self.cleaned_data.get("observaciones", ""))

    def clean(self):
        cleaned = super().clean()
        mp = cleaned.get("materia_prima")
        unidades = cleaned.get("unidades_salida")
        if mp and unidades:
            disponible = mp.existencia_actual
            if unidades > disponible:
                self.add_error(
                    "unidades_salida",
                    f"Existencia insuficiente en {mp.codigo} ({mp.nombre}). Disponible actual: {disponible} {mp.unidad_medida}.",
                )
        return cleaned


class OrdenProduccionForm(forms.ModelForm):
    """Formulario para Alta O.P. (Alta al iniciar)."""

    class Meta:
        model = OrdenProduccion
        fields = [
            "codigo",
            "producto_terminado",
            "fecha_apertura",
            "cantidad_unidades",
            "email_supervisor",
            "observaciones",
        ]
        widgets = {
            "codigo": forms.TextInput(
                attrs={
                    "class": "form-control text-uppercase",
                    "placeholder": "Ej. OP-1050",
                }
            ),
            "producto_terminado": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Ej. Escritorio Ejecutivo Modelo Alfa",
                }
            ),
            "fecha_apertura": DateInputISO(attrs={"class": "form-control"}),
            "cantidad_unidades": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "min": "1",
                    "step": "1",
                    "placeholder": "Ej. 100",
                }
            ),
            "email_supervisor": forms.EmailInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "supervisor@planta.com (Opcional)",
                }
            ),
            "observaciones": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 2,
                    "placeholder": "Especificaciones del lote o cliente (Opcional)",
                }
            ),
        }

    def clean_codigo(self):
        codigo = normalizar_codigo(self.cleaned_data.get("codigo", ""))
        if not codigo:
            raise forms.ValidationError("El código de la Orden de Producción es obligatorio.")
        # Si el usuario escribe solo números como 1050, se respeta o estandariza a OP-1050 si no tiene prefijo
        if codigo.isdigit():
            codigo = f"OP-{codigo}"
        qs = OrdenProduccion.objects.filter(codigo=codigo)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(f"La Orden de Producción '{codigo}' ya existe.")
        return codigo


class ManoObraDirectaForm(forms.ModelForm):
    """Formulario para M.O. -> Pago de nómina a operarios asignado a Orden de Producción."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["orden_produccion"].queryset = OrdenProduccion.objects.all()
        self.fields["horas_trabajadas"].required = False
        self.fields["cuota_por_hora"].required = False
        self.fields["importe_total"].required = False
        self.fields["referencia_nomina"].required = False
        self.fields["operario_departamento"].required = False

    class Meta:
        model = ManoObraDirecta
        fields = [
            "orden_produccion",
            "fecha",
            "referencia_nomina",
            "operario_departamento",
            "horas_trabajadas",
            "cuota_por_hora",
            "importe_total",
            "observaciones",
        ]
        widgets = {
            "orden_produccion": forms.Select(attrs={"class": "form-select"}),
            "fecha": DateInputISO(attrs={"class": "form-control"}),
            "referencia_nomina": forms.TextInput(
                attrs={
                    "class": "form-control text-uppercase",
                    "placeholder": "Ej. NOM-1050-01",
                }
            ),
            "operario_departamento": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Ej. Cuadrilla de Ensamble / Operarios",
                }
            ),
            "horas_trabajadas": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                    "min": "0.00",
                    "id": "id_mo_horas",
                    "placeholder": "Ej. 1.00",
                }
            ),
            "cuota_por_hora": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                    "min": "0.00",
                    "id": "id_mo_cuota",
                    "placeholder": "Opcional si capturas importe directo",
                }
            ),
            "importe_total": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                    "min": "0.01",
                    "id": "id_mo_importe",
                    "placeholder": "Ej. 10000.00",
                }
            ),
            "observaciones": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 2,
                    "placeholder": "Detalle del pago de nómina a operarios (Opcional)",
                }
            ),
        }

    def clean(self):
        cleaned = super().clean()
        op = cleaned.get("orden_produccion")
        horas = cleaned.get("horas_trabajadas")
        cuota = cleaned.get("cuota_por_hora")
        importe = cleaned.get("importe_total")

        if horas is None or horas <= 0:
            horas = Decimal("1.00")
            cleaned["horas_trabajadas"] = horas

        if cuota is None or cuota < 0:
            cuota = Decimal("0.00")
            cleaned["cuota_por_hora"] = cuota

        if horas > 0 and cuota > 0:
            importe = (horas * cuota).quantize(Decimal("0.01"))
            cleaned["importe_total"] = importe

        if not importe or importe <= 0:
            self.add_error(
                "importe_total",
                "Ingresa el importe total de nómina (o captura horas y cuota por hora mayores a 0).",
            )

        ref = normalizar_codigo(cleaned.get("referencia_nomina") or "")
        if not ref and op:
            ref = f"NOM-{op.codigo}"
        elif not ref:
            ref = "NOM-MO"
        cleaned["referencia_nomina"] = ref

        operario = normalizar_texto(cleaned.get("operario_departamento") or "")
        if not operario:
            operario = "Operarios de producción"
        cleaned["operario_departamento"] = operario

        return cleaned


class GastoIndirectoForm(forms.ModelForm):
    """Formulario para G.I. -> Agregar gasto indirecto (M.P.I., S y S de supervisores, luz, etc.)."""

    class Meta:
        model = GastoIndirecto
        fields = [
            "codigo_referencia",
            "fecha",
            "categoria",
            "concepto",
            "importe",
            "observaciones",
        ]
        widgets = {
            "codigo_referencia": forms.TextInput(
                attrs={
                    "class": "form-control text-uppercase",
                    "placeholder": "Ej. GI-001",
                }
            ),
            "fecha": DateInputISO(attrs={"class": "form-control"}),
            "categoria": forms.Select(attrs={"class": "form-select"}),
            "concepto": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Ej. Consumo de energía eléctrica de planta / Sueldo de supervisores",
                }
            ),
            "importe": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                    "min": "0.01",
                    "placeholder": "Ej. 15000.00",
                }
            ),
            "observaciones": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 2,
                    "placeholder": "Notas del comprobante o periodo (Opcional)",
                }
            ),
        }

    def clean_codigo_referencia(self):
        codigo = normalizar_codigo(self.cleaned_data.get("codigo_referencia", ""))
        qs = GastoIndirecto.objects.filter(codigo_referencia=codigo)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(f"El folio de Gasto Indirecto '{codigo}' ya existe.")
        return codigo


class ProrrateoGIForm(forms.Form):
    """Formulario para Prorratear Gastos Indirectos entre las Órdenes de Producción."""

    codigo = forms.CharField(
        label="Folio del Prorrateo",
        max_length=40,
        widget=forms.TextInput(
            attrs={
                "class": "form-control text-uppercase",
                "placeholder": "Ej. PRORRATEO-01",
            }
        ),
    )
    fecha = forms.DateField(
        label="Fecha de Aplicación (YYYY-MM-DD)",
        initial=timezone.localdate,
        widget=DateInputISO(attrs={"class": "form-control"}),
    )
    base_seleccionada = forms.ChoiceField(
        label="Base de Prorrateo para dividir los Gastos Indirectos",
        choices=ProrrateoGI.BASES_PRORRATEO,
        initial="COSTO_PRIMO",
        widget=forms.Select(attrs={"class": "form-select", "id": "id_base_prorrateo"}),
    )
    solo_pendientes = forms.BooleanField(
        label="Prorratear únicamente los Gastos Indirectos aún no prorrateados",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    observaciones = forms.CharField(
        label="Observaciones del Prorrateo (Opcional)",
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 2,
                "placeholder": "Criterio contable utilizado para distribuir los G.I.",
            }
        ),
    )

    def clean_codigo(self):
        codigo = normalizar_codigo(self.cleaned_data.get("codigo", ""))
        if ProrrateoGI.objects.filter(codigo=codigo).exists():
            raise forms.ValidationError(f"El folio de prorrateo '{codigo}' ya fue utilizado.")
        return codigo


class BajaProduccionTerminadaForm(forms.Form):
    """Formulario para Baja de prod. -> Concluir Orden de Producción y pasar a Artículos Terminados."""

    orden_produccion = forms.ModelChoiceField(
        label="Orden de Producción a Concluir (Baja de Producción en Proceso)",
        queryset=OrdenProduccion.objects.filter(estado="EN_PROCESO"),
        widget=forms.Select(attrs={"class": "form-select", "id": "id_baja_prod_orden"}),
    )
    codigo_entrada = forms.CharField(
        label="Folio de Entrada a Almacén de Artículos Terminados",
        max_length=40,
        widget=forms.TextInput(
            attrs={
                "class": "form-control text-uppercase",
                "placeholder": "Ej. AT-1050",
            }
        ),
    )
    fecha_entrada = forms.DateField(
        label="Fecha de Terminación y Entrada (YYYY-MM-DD)",
        initial=timezone.localdate,
        widget=DateInputISO(attrs={"class": "form-control"}),
    )
    observaciones = forms.CharField(
        label="Observaciones de Cierre y Entrega a Almacén (Opcional)",
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 2,
                "placeholder": "Inspección de calidad aprobada, lote completo...",
            }
        ),
    )

    def clean_codigo_entrada(self):
        codigo = normalizar_codigo(self.cleaned_data.get("codigo_entrada", ""))
        if ArticuloTerminado.objects.filter(codigo_entrada=codigo).exists():
            raise forms.ValidationError(f"El folio de Artículo Terminado '{codigo}' ya existe.")
        return codigo

