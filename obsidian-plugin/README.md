# Finanzas

Plugin local para administrar deudas e ingresos dentro de Obsidian.

## Abrir Finanzas

Usa el icono de billetera en la barra lateral o el comando **Finanzas: Abrir Finanzas**.

La barra del plugin contiene **Inicio**, **Deudas**, **Ingresos** y **Ajustes**.

## Uso

- **Inicio:** calendario editable. Toca un pago para abrir una ventana pequeña; en móvil toca el día. En escritorio puedes arrastrar el pago. Los totales del mes y de cada quincena muestran en rojo lo que supera el ingreso quincenal configurado.
- **Deudas:** pendientes de una sola vez con casilla de completado, recordatorios recurrentes y deudas con abonos. Solo estas últimas muestran historial y barra de progreso.
- **Ingresos:** consulta los ingresos importados desde sus comprobantes.
- **Ajustes:** configura la aplicación y sus integraciones.

## Apariencia

Finanzas adopta los colores y la tipografía del tema de Obsidian. El calendario destaca el día actual con un verde tenue y adapta los estados de pago al tema claro u oscuro.

## Instalación y datos

Este plugin funciona en Obsidian de escritorio y necesita Python con las dependencias de la aplicación instaladas. La aplicación está en `.finanzas/` dentro de la bóveda. Al abrir Finanzas, el complemento puede iniciar el servidor. El inicio con la bóveda es opcional y está desactivado por defecto. El servicio escucha en `http://127.0.0.1:5028`.

Los datos financieros se guardan en `.finanzas/instance/finanzas.sqlite3`; los respaldos están en `.finanzas/backups/`. La documentación técnica de la aplicación está en `.finanzas/README.md`.

## Solución de problemas

Si Finanzas no responde, pulsa **Inicio** para volver a cargar la aplicación. Revisa el ejecutable de Python en **Ajustes de Obsidian → Finanzas** y los registros en `.finanzas/logs/` si el servicio no inicia.

Inicia sesión con tu usuario habitual la primera vez que abras la aplicación dentro de Obsidian.

Los cambios del servidor necesitan reiniciar el proceso de Finanzas y volver a abrir **Inicio**. Para cargar cambios del complemento, desactiva y reactiva **Finanzas** en los plugins de comunidad de Obsidian.
