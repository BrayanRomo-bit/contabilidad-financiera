from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone


def normalizar_codigo(valor: str) -> str:
    """Limpia espacios en blanco, reemplaza espacios internos por guiones y convierte a MAYÚSCULAS."""
    if not valor:
        return ""
    limpio = " ".join(valor.strip().split())
    return limpio.upper().replace(" ", "-")


def normalizar_texto(valor: str) -> str:
    """Limpia espacios en blanco innecesarios al inicio, fin y duplicados intermedios."""
    if not valor:
        return ""
    return " ".join(valor.strip().split())


def redondear_moneda(valor) -> Decimal:
    if valor is None:
        return Decimal("0.00")
    return Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class MateriaPrima(models.Model):
    UNIDADES = [
        ("PZA", "Piezas (PZA)"),
        ("KG", "Kilogramos (KG)"),
        ("LT", "Litros (LT)"),
        ("MT", "Metros (MT)"),
        ("CAJA", "Cajas (CAJA)"),
    ]

    # Sección 1: Datos Generales
    codigo = models.CharField(
        "Código de Materia Prima",
        max_length=30,
        unique=True,
        help_text="Ejemplo: MP-001, MAD-PINO. Se normaliza automáticamente a MAYÚSCULAS.",
    )
    nombre = models.CharField(
        "Nombre del Insumo / Materia Prima",
        max_length=120,
        help_text="Nombre descriptivo del material directo.",
    )
    unidad_medida = models.CharField(
        "Unidad de Medida",
        max_length=15,
        choices=UNIDADES,
        default="PZA",
    )

    # Sección 2: Detalles Específicos
    stock_minimo = models.DecimalField(
        "Stock Mínimo de Alerta",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    email_proveedor = models.EmailField(
        "Correo de Proveedor Principal (Opcional)",
        blank=True,
        null=True,
        help_text="Formato válido de correo electrónico (ej. ventas@proveedor.com).",
    )

    # Sección 3: Observaciones
    observaciones = models.TextField(
        "Observaciones",
        blank=True,
        default="",
    )
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Materia Prima"
        verbose_name_plural = "Materias Primas"
        ordering = ["codigo"]

    def clean_fields(self, exclude=None):
        self.codigo = normalizar_codigo(self.codigo)
        self.nombre = normalizar_texto(self.nombre)
        if self.nombre:
            self.nombre = self.nombre[0].upper() + self.nombre[1:]
        if self.email_proveedor:
            self.email_proveedor = self.email_proveedor.strip().lower()
        if self.observaciones:
            self.observaciones = self.observaciones.strip()
        super().clean_fields(exclude=exclude)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def ultimo_movimiento(self):
        return self.movimientos.order_by("fecha", "id").last()

    @property
    def existencia_actual(self) -> Decimal:
        ult = self.ultimo_movimiento
        return ult.existencia if ult else Decimal("0.00")

    @property
    def saldo_actual(self) -> Decimal:
        ult = self.ultimo_movimiento
        return ult.saldo if ult else Decimal("0.00")

    @property
    def costo_promedio_actual(self) -> Decimal:
        ult = self.ultimo_movimiento
        if ult and ult.existencia > 0:
            return redondear_moneda(ult.saldo / ult.existencia)
        return ult.costo_promedio if ult else Decimal("0.00")

    @property
    def total_entradas_unidades(self) -> Decimal:
        res = self.movimientos.aggregate(t=Sum("unidades_entrada"))["t"]
        return res or Decimal("0.00")

    @property
    def total_salidas_unidades(self) -> Decimal:
        res = self.movimientos.aggregate(t=Sum("unidades_salida"))["t"]
        return res or Decimal("0.00")

    @property
    def total_debe(self) -> Decimal:
        res = self.movimientos.aggregate(t=Sum("debe"))["t"]
        return res or Decimal("0.00")

    @property
    def total_haber(self) -> Decimal:
        res = self.movimientos.aggregate(t=Sum("haber"))["t"]
        return res or Decimal("0.00")

    def recalcular_kardex(self):
        """Recalcula secuencialmente existencias, costos promedios y saldos en la tarjeta de almacén."""
        existencia_acum = Decimal("0.00")
        saldo_acum = Decimal("0.00")
        costo_prom = Decimal("0.00")

        for mov in self.movimientos.order_by("fecha", "id"):
            if mov.tipo_movimiento in ("SALDO_INICIAL", "COMPRA"):
                entrada = mov.unidades_entrada
                debe = redondear_moneda(entrada * mov.costo_unitario)
                haber = Decimal("0.00")
                existencia_acum += entrada
                saldo_acum += debe
                if existencia_acum > 0:
                    costo_prom = redondear_moneda(saldo_acum / existencia_acum)
                mov.unidades_salida = Decimal("0.00")
                mov.debe = debe
                mov.haber = haber
                mov.existencia = existencia_acum
                mov.costo_promedio = costo_prom
                mov.saldo = saldo_acum
            else:  # BAJA_PRODUCCION
                salida = mov.unidades_salida
                # En costo promedio, la salida sale al costo promedio vigente
                costo_salida = costo_prom if costo_prom > 0 else mov.costo_unitario
                if salida == existencia_acum:
                    # Evitar diferencias de centavos por redondeo al vaciar existencia
                    haber = saldo_acum
                else:
                    haber = redondear_moneda(salida * costo_salida)
                debe = Decimal("0.00")
                existencia_acum -= salida
                saldo_acum -= haber
                mov.costo_unitario = costo_salida
                mov.unidades_entrada = Decimal("0.00")
                mov.debe = debe
                mov.haber = haber
                mov.existencia = existencia_acum
                mov.costo_promedio = costo_prom
                mov.saldo = saldo_acum

            mov.esquema_json = {
                "id_producto": self.codigo,
                "nombre_producto": self.nombre,
                "fecha": mov.fecha.strftime("%Y-%m-%d"),
                "tipo_movimiento": mov.tipo_movimiento,
                "cantidad": float(
                    mov.unidades_entrada
                    if mov.tipo_movimiento in ("SALDO_INICIAL", "COMPRA")
                    else mov.unidades_salida
                ),
                "costo_unitario": float(mov.costo_unitario),
                "costo_promedio": float(mov.costo_promedio),
                "debe": float(mov.debe),
                "haber": float(mov.haber),
                "existencia": float(mov.existencia),
                "saldo": float(mov.saldo),
                "orden_produccion": mov.orden_produccion.codigo if mov.orden_produccion else None,
            }
            MovimientoAlmacenMP.objects.filter(pk=mov.pk).update(
                unidades_entrada=mov.unidades_entrada,
                unidades_salida=mov.unidades_salida,
                costo_unitario=mov.costo_unitario,
                costo_promedio=mov.costo_promedio,
                debe=mov.debe,
                haber=mov.haber,
                existencia=mov.existencia,
                saldo=mov.saldo,
                esquema_json=mov.esquema_json,
            )

    def __str__(self):
        return f"{self.codigo} - {self.nombre} (Disp: {self.existencia_actual} {self.unidad_medida})"


class OrdenProduccion(models.Model):
    ESTADOS = [
        ("EN_PROCESO", "En Proceso (Producción Activa)"),
        ("TERMINADA", "Terminada (Enviada a Art. Terminado)"),
    ]

    # Sección 1: Datos Generales
    codigo = models.CharField(
        "Número / Código de Orden (O.P.)",
        max_length=30,
        unique=True,
        help_text="Ejemplo: OP-1050, OP-1051, OP-1052.",
    )
    producto_terminado = models.CharField(
        "Artículo a Producir",
        max_length=150,
        help_text="Nombre del producto terminado que fabrica esta orden.",
    )
    fecha_apertura = models.DateField(
        "Fecha de Alta (YYYY-MM-DD)",
        default=timezone.localdate,
    )

    # Sección 2: Detalles Específicos
    cantidad_unidades = models.PositiveIntegerField(
        "Cantidad de Unidades a Producir",
        validators=[MinValueValidator(1)],
        help_text="Número entero estricto de artículos que comprende la orden.",
    )
    estado = models.CharField(
        "Estado de la Orden",
        max_length=20,
        choices=ESTADOS,
        default="EN_PROCESO",
    )
    email_supervisor = models.EmailField(
        "Correo del Supervisor de Orden (Opcional)",
        blank=True,
        null=True,
    )
    fecha_cierre = models.DateField(
        "Fecha de Terminación / Baja de Producción",
        blank=True,
        null=True,
    )

    # Sección 3: Observaciones
    observaciones = models.TextField(
        "Especificaciones / Observaciones",
        blank=True,
        default="",
    )
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Orden de Producción"
        verbose_name_plural = "Órdenes de Producción"
        ordering = ["codigo"]

    def clean_fields(self, exclude=None):
        self.codigo = normalizar_codigo(self.codigo)
        self.producto_terminado = normalizar_texto(self.producto_terminado)
        if self.producto_terminado:
            self.producto_terminado = (
                self.producto_terminado[0].upper() + self.producto_terminado[1:]
            )
        if self.email_supervisor:
            self.email_supervisor = self.email_supervisor.strip().lower()
        if self.observaciones:
            self.observaciones = self.observaciones.strip()
        super().clean_fields(exclude=exclude)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def total_mp(self) -> Decimal:
        """Suma de Materia Prima Directa cargada desde las bajas de Tarjetas de Almacén."""
        res = self.consumos_mp.filter(tipo_movimiento="BAJA_PRODUCCION").aggregate(
            total=Sum("haber")
        )["total"]
        return redondear_moneda(res or Decimal("0.00"))

    @property
    def total_mo(self) -> Decimal:
        """Suma de Mano de Obra Directa cargada por pagos de nómina a operarios."""
        res = self.cargos_mo.aggregate(total=Sum("importe_total"))["total"]
        return redondear_moneda(res or Decimal("0.00"))

    @property
    def total_horas_mo(self) -> Decimal:
        res = self.cargos_mo.aggregate(total=Sum("horas_trabajadas"))["total"]
        return redondear_moneda(res or Decimal("0.00"))

    @property
    def costo_primo(self) -> Decimal:
        """Costo Primo = Materia Prima Directa + Mano de Obra Directa."""
        return redondear_moneda(self.total_mp + self.total_mo)

    @property
    def total_gi(self) -> Decimal:
        """Suma de Gastos Indirectos prorrateados a esta orden."""
        res = self.prorrateos_gi.aggregate(total=Sum("importe_asignado"))["total"]
        return redondear_moneda(res or Decimal("0.00"))

    @property
    def costo_total(self) -> Decimal:
        """Costo Total de Producción = MP + MO + GI."""
        return redondear_moneda(self.total_mp + self.total_mo + self.total_gi)

    @property
    def costo_unitario(self) -> Decimal:
        if self.cantidad_unidades and self.cantidad_unidades > 0:
            return redondear_moneda(self.costo_total / Decimal(self.cantidad_unidades))
        return Decimal("0.00")

    def __str__(self):
        return f"{self.codigo} - {self.producto_terminado} ({self.cantidad_unidades} uds)"


class MovimientoAlmacenMP(models.Model):
    TIPOS_MOVIMIENTO = [
        ("SALDO_INICIAL", "Alta - Inventario Inicial (Saldo)"),
        ("COMPRA", "Alta - Compra de Materia Prima"),
        ("BAJA_PRODUCCION", "Baja - Envío a Producción en Proceso (Afecta O.P.)"),
    ]

    # Sección 1: Datos Generales
    materia_prima = models.ForeignKey(
        MateriaPrima,
        on_delete=models.CASCADE,
        related_name="movimientos",
        verbose_name="Materia Prima",
    )
    fecha = models.DateField(
        "Fecha del Movimiento (YYYY-MM-DD)",
        default=timezone.localdate,
    )
    tipo_movimiento = models.CharField(
        "Tipo de Movimiento",
        max_length=25,
        choices=TIPOS_MOVIMIENTO,
    )
    referencia = models.CharField(
        "Folio / Referencia",
        max_length=40,
        help_text="Ejemplo: INV-01, FAC-204, VALE-1050.",
    )

    # Sección 2: Detalles Específicos y Vinculación a Orden
    orden_produccion = models.ForeignKey(
        OrdenProduccion,
        on_delete=models.CASCADE,
        related_name="consumos_mp",
        verbose_name="Orden de Producción Afectada",
        blank=True,
        null=True,
        help_text="Obligatorio cuando es Baja a Producción en Proceso.",
    )
    unidades_entrada = models.DecimalField(
        "Unidades Entrada",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    unidades_salida = models.DecimalField(
        "Unidades Salida",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    existencia = models.DecimalField(
        "Existencia Acumulada",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    costo_unitario = models.DecimalField(
        "Costo Unitario ($)",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    costo_promedio = models.DecimalField(
        "Costo Promedio ($)",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    debe = models.DecimalField(
        "Debe / Entrada ($)",
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    haber = models.DecimalField(
        "Haber / Salida ($)",
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    saldo = models.DecimalField(
        "Saldo en Almacén ($)",
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    # Sección 3: Observaciones y Esquema JSON
    observaciones = models.TextField(
        "Observaciones del Movimiento",
        blank=True,
        default="",
    )
    esquema_json = models.JSONField(
        "Esquema Estructurado JSON",
        default=dict,
        blank=True,
    )
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Movimiento de Almacén (Tarjeta MP)"
        verbose_name_plural = "Movimientos de Almacén (Tarjetas MP)"
        ordering = ["fecha", "id"]

    def clean(self):
        super().clean()
        self.referencia = normalizar_codigo(self.referencia)
        if self.observaciones:
            self.observaciones = self.observaciones.strip()

        if self.tipo_movimiento in ("SALDO_INICIAL", "COMPRA"):
            if self.unidades_entrada <= 0:
                raise ValidationError(
                    {"unidades_entrada": "La cantidad de entrada debe ser mayor a 0."}
                )
            if self.costo_unitario <= 0:
                raise ValidationError(
                    {"costo_unitario": "El costo unitario de entrada debe ser mayor a 0."}
                )
            self.orden_produccion = None
        elif self.tipo_movimiento == "BAJA_PRODUCCION":
            if self.unidades_salida <= 0:
                raise ValidationError(
                    {"unidades_salida": "La cantidad de salida a producción debe ser mayor a 0."}
                )
            if not self.orden_produccion:
                raise ValidationError(
                    {
                        "orden_produccion": "Debes seleccionar la Orden de Producción a la que se cargará esta Materia Prima."
                    }
                )
            if self.materia_prima_id and not self.pk:
                disponible = self.materia_prima.existencia_actual
                if self.unidades_salida > disponible:
                    raise ValidationError(
                        {
                            "unidades_salida": f"Stock insuficiente en Tarjeta de Almacén ({self.materia_prima.codigo}). Disponible: {disponible} {self.materia_prima.unidad_medida}."
                        }
                    )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
        self.materia_prima.recalcular_kardex()

    def __str__(self):
        return f"{self.fecha} | {self.materia_prima.codigo} | {self.get_tipo_movimiento_display()}"


class ManoObraDirecta(models.Model):
    # Sección 1: Datos Generales
    orden_produccion = models.ForeignKey(
        OrdenProduccion,
        on_delete=models.CASCADE,
        related_name="cargos_mo",
        verbose_name="Orden de Producción",
    )
    fecha = models.DateField(
        "Fecha de Pago de Nómina (YYYY-MM-DD)",
        default=timezone.localdate,
    )
    referencia_nomina = models.CharField(
        "Folio / Referencia de Nómina",
        max_length=40,
        help_text="Ejemplo: NOM-01, SEM-15.",
    )
    operario_departamento = models.CharField(
        "Operarios / Cuadrilla",
        max_length=120,
        help_text="Nombre del operario o departamento productivo.",
    )

    # Sección 2: Detalles Específicos
    horas_trabajadas = models.DecimalField(
        "Horas Directas Trabajadas",
        max_digits=10,
        decimal_places=2,
        default=Decimal("1.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    cuota_por_hora = models.DecimalField(
        "Costo / Cuota por Hora ($)",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    importe_total = models.DecimalField(
        "Importe Total de Nómina ($)",
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    # Sección 3: Observaciones y JSON
    observaciones = models.TextField(
        "Observaciones",
        blank=True,
        default="",
    )
    esquema_json = models.JSONField(
        "Esquema Estructurado JSON",
        default=dict,
        blank=True,
    )
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Mano de Obra Directa (M.O.)"
        verbose_name_plural = "Cargos de Mano de Obra Directa (M.O.)"
        ordering = ["fecha", "id"]

    def clean(self):
        super().clean()
        self.referencia_nomina = normalizar_codigo(self.referencia_nomina)
        self.operario_departamento = normalizar_texto(self.operario_departamento)
        if self.observaciones:
            self.observaciones = self.observaciones.strip()
        if self.horas_trabajadas and self.cuota_por_hora and self.cuota_por_hora > 0:
            self.importe_total = redondear_moneda(self.horas_trabajadas * self.cuota_por_hora)

    def save(self, *args, **kwargs):
        self.full_clean()
        self.esquema_json = {
            "id_orden": self.orden_produccion.codigo,
            "referencia_nomina": self.referencia_nomina,
            "operario_departamento": self.operario_departamento,
            "fecha": self.fecha.strftime("%Y-%m-%d"),
            "tipo_movimiento": "MANO_OBRA_DIRECTA",
            "horas_trabajadas": float(self.horas_trabajadas),
            "cuota_por_hora": float(self.cuota_por_hora),
            "importe_total": float(self.importe_total),
        }
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.referencia_nomina} -> {self.orden_produccion.codigo} (${self.importe_total})"


class GastoIndirecto(models.Model):
    CATEGORIAS_GI = [
        ("MPI", "Materia Prima Indirecta (M.P.I.)"),
        ("SYS_SUPERVISORES", "Sueldos y Salarios de Supervisores (S y S)"),
        ("ENERGIA_LUZ", "Energía Eléctrica / Luz de Planta"),
        ("RENTA_MANT", "Renta y Mantenimiento Industrial"),
        ("DEPRECIACION", "Depreciación de Maquinaria Fabril"),
        ("OTROS", "Otros Costos Indirectos de Fabricación"),
    ]

    # Sección 1: Datos Generales
    codigo_referencia = models.CharField(
        "Folio / Referencia G.I.",
        max_length=40,
        unique=True,
        help_text="Ejemplo: GI-001, REC-LUZ-01, MPI-10.",
    )
    fecha = models.DateField(
        "Fecha del Gasto (YYYY-MM-DD)",
        default=timezone.localdate,
    )
    categoria = models.CharField(
        "Clasificación del Gasto Indirecto",
        max_length=30,
        choices=CATEGORIAS_GI,
        default="MPI",
    )

    # Sección 2: Detalles Específicos
    concepto = models.CharField(
        "Concepto Específico",
        max_length=160,
        help_text="Ejemplo: Pago de luz bimestral nave industrial, Sueldo supervisor turno 1.",
    )
    importe = models.DecimalField(
        "Importe del Gasto Indirecto ($)",
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    prorrateado = models.BooleanField(
        "¿Ya fue Prorrateado a Órdenes?",
        default=False,
    )

    # Sección 3: Observaciones y JSON
    observaciones = models.TextField(
        "Observaciones",
        blank=True,
        default="",
    )
    esquema_json = models.JSONField(
        "Esquema Estructurado JSON",
        default=dict,
        blank=True,
    )
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Gasto Indirecto (G.I.)"
        verbose_name_plural = "Gastos Indirectos (G.I.)"
        ordering = ["fecha", "id"]

    def clean(self):
        super().clean()
        self.codigo_referencia = normalizar_codigo(self.codigo_referencia)
        self.concepto = normalizar_texto(self.concepto)
        if self.concepto:
            self.concepto = self.concepto[0].upper() + self.concepto[1:]
        if self.observaciones:
            self.observaciones = self.observaciones.strip()

    def save(self, *args, **kwargs):
        self.full_clean()
        self.esquema_json = {
            "codigo_referencia": self.codigo_referencia,
            "categoria": self.categoria,
            "concepto": self.concepto,
            "fecha": self.fecha.strftime("%Y-%m-%d"),
            "tipo_movimiento": "GASTO_INDIRECTO",
            "importe": float(self.importe),
            "prorrateado": self.prorrateado,
        }
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.codigo_referencia} | {self.get_categoria_display()} - ${self.importe}"


class ProrrateoGI(models.Model):
    BASES_PRORRATEO = [
        ("COSTO_PRIMO", "Costo Primo (Materia Prima Directa + Mano de Obra Directa)"),
        ("MP_DIRECTA", "Importe de Materia Prima Directa (M.P.)"),
        ("MO_DIRECTA", "Importe de Mano de Obra Directa (M.O.)"),
        ("UNIDADES", "Unidades Producidas por Orden"),
        ("HORAS_MO", "Horas de Mano de Obra Directa"),
    ]

    codigo = models.CharField(
        "Folio de Prorrateo",
        max_length=40,
        unique=True,
    )
    fecha = models.DateField(
        "Fecha de Prorrateo (YYYY-MM-DD)",
        default=timezone.localdate,
    )
    base_seleccionada = models.CharField(
        "Base de Prorrateo",
        max_length=25,
        choices=BASES_PRORRATEO,
        default="COSTO_PRIMO",
    )
    total_gastos_indirectos = models.DecimalField(
        "Total Gastos Indirectos a Distribuir ($)",
        max_digits=14,
        decimal_places=2,
    )
    total_base = models.DecimalField(
        "Suma Total de la Base",
        max_digits=14,
        decimal_places=2,
    )
    factor_prorrateo = models.DecimalField(
        "Factor de Prorrateo (Total G.I. / Base)",
        max_digits=16,
        decimal_places=8,
    )
    observaciones = models.TextField(
        "Observaciones",
        blank=True,
        default="",
    )
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Prorrateo de Gastos Indirectos"
        verbose_name_plural = "Prorrateos de Gastos Indirectos"
        ordering = ["-fecha", "-id"]

    def __str__(self):
        return f"{self.codigo} ({self.get_base_seleccionada_display()}) - ${self.total_gastos_indirectos}"


class ProrrateoOrdenDetalle(models.Model):
    prorrateo = models.ForeignKey(
        ProrrateoGI,
        on_delete=models.CASCADE,
        related_name="detalles",
    )
    orden_produccion = models.ForeignKey(
        OrdenProduccion,
        on_delete=models.CASCADE,
        related_name="prorrateos_gi",
    )
    valor_base_orden = models.DecimalField(
        "Valor Base de la Orden",
        max_digits=14,
        decimal_places=2,
    )
    factor_aplicado = models.DecimalField(
        "Factor Aplicado",
        max_digits=16,
        decimal_places=8,
    )
    porcentaje_participacion = models.DecimalField(
        "Porcentaje (%)",
        max_digits=7,
        decimal_places=2,
    )
    importe_asignado = models.DecimalField(
        "Gasto Indirecto Asignado ($)",
        max_digits=14,
        decimal_places=2,
    )

    class Meta:
        verbose_name = "Detalle de Prorrateo por Orden"
        verbose_name_plural = "Detalles de Prorrateo por Orden"
        ordering = ["orden_produccion__codigo"]

    def __str__(self):
        return f"{self.orden_produccion.codigo}: ${self.importe_asignado}"


class ArticuloTerminado(models.Model):
    """Registro de Almacén de Artículos Terminados al dar de baja (concluir) una Orden de Producción."""

    orden_produccion = models.OneToOneField(
        OrdenProduccion,
        on_delete=models.CASCADE,
        related_name="registro_terminado",
        verbose_name="Orden de Producción Concluida",
    )
    codigo_entrada = models.CharField(
        "Folio de Entrada a Artículos Terminados",
        max_length=40,
        unique=True,
    )
    fecha_entrada = models.DateField(
        "Fecha de Entrada a Almacén (YYYY-MM-DD)",
        default=timezone.localdate,
    )
    nombre_articulo = models.CharField(
        "Artículo Terminado",
        max_length=150,
    )
    unidades_recibidas = models.PositiveIntegerField(
        "Unidades Terminadas Recibidas",
    )
    costo_mp = models.DecimalField("Costo M.P. Acumulado ($)", max_digits=14, decimal_places=2)
    costo_mo = models.DecimalField("Costo M.O. Acumulado ($)", max_digits=14, decimal_places=2)
    costo_gi = models.DecimalField("Costo G.I. Prorrateado ($)", max_digits=14, decimal_places=2)
    costo_total = models.DecimalField("Costo Total Producción ($)", max_digits=14, decimal_places=2)
    costo_unitario = models.DecimalField("Costo Unitario ($)", max_digits=14, decimal_places=2)
    observaciones = models.TextField("Observaciones", blank=True, default="")
    esquema_json = models.JSONField("Esquema Estructurado JSON", default=dict, blank=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Artículo Terminado"
        verbose_name_plural = "Artículos Terminados"
        ordering = ["-fecha_entrada", "-id"]

    def clean(self):
        super().clean()
        self.codigo_entrada = normalizar_codigo(self.codigo_entrada)
        self.nombre_articulo = normalizar_texto(self.nombre_articulo)
        if self.observaciones:
            self.observaciones = self.observaciones.strip()

    def save(self, *args, **kwargs):
        self.full_clean()
        self.esquema_json = {
            "id_producto": self.codigo_entrada,
            "orden_origen": self.orden_produccion.codigo,
            "nombre_articulo": self.nombre_articulo,
            "fecha": self.fecha_entrada.strftime("%Y-%m-%d"),
            "tipo_movimiento": "ENTRADA_ARTICULO_TERMINADO",
            "cantidad": self.unidades_recibidas,
            "desglose_costos": {
                "materia_prima_directa": float(self.costo_mp),
                "mano_obra_directa": float(self.costo_mo),
                "gastos_indirectos": float(self.costo_gi),
                "costo_total": float(self.costo_total),
                "costo_unitario": float(self.costo_unitario),
            },
        }
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.codigo_entrada} | {self.nombre_articulo} ({self.unidades_recibidas} uds @ ${self.costo_unitario})"
