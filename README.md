# Finanzas

[English](README.en.md)

Aplicación local de finanzas personales construida con Flask y SQLite. Permite organizar deudas, consultar un calendario de pagos, registrar abonos e importar ingresos desde CFDI. Incluye un complemento para Obsidian de escritorio. La interfaz está en español y los importes se expresan en MXN.

## Funciones

- Calendario mensual con edición de montos y fechas, arrastre de pagos en escritorio y vista por día en móvil.
- Deudas de una sola vez con total pendiente. Completarlas las elimina de la lista.
- Recordatorios recurrentes: completar una fecha la retira del calendario y conserva las siguientes repeticiones.
- Deudas con abonos, calendario de cuotas, saldo pendiente e historial de pagos.
- Comparación opcional de pagos programados con un ingreso quincenal configurable.
- Importación opcional de CFDI desde Gmail, con detección de duplicados.
- Acceso mediante contraseña, verificación CSRF y credenciales de Gmail cifradas en la base local.
- Complemento de Obsidian que abre la aplicación en una pestaña y adopta el tema de la bóveda.

## Requisitos

Python 3.11 o superior. Obsidian de escritorio es opcional. La primera instalación requiere acceso a internet para descargar dependencias.

## Instalación

Desde la carpeta del repositorio, en Windows:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/bootstrap_auth.py --username admin
.\.venv\Scripts\python.exe run.py
```

En Linux o macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/bootstrap_auth.py --username admin
.venv/bin/python run.py
```

El asistente pide una contraseña, crea `.env` a partir de `.env.example` y guarda un hash de la contraseña y una clave de sesión aleatoria. Abre <http://127.0.0.1:5028> e inicia sesión con el usuario elegido. Una instalación nueva comienza sin deudas ni movimientos.

## Configuración

Configura los valores privados en `.env`, que está excluido de Git. Reinicia el servidor después de modificarlo.

| Variable | Uso |
| --- | --- |
| `PROJECT_NAME` | Nombre de la aplicación. |
| `PORT` | Puerto local; por defecto, `5028`. |
| `APP_TIMEZONE` | Zona horaria; por defecto, `America/Mexico_City`. |
| `AUTH_USERNAME` / `AUTH_PASSWORD_HASH` | Acceso configurado por el asistente. |
| `SECRET_KEY` | Clave de sesiones y cifrado. Consérvala junto con tus respaldos privados. |
| `DATABASE_URL` | Ubicación SQLite; por defecto, `sqlite:///instance/finanzas.sqlite3`. |
| `QUINCENAL_INCOME_CENTS` | Ingreso por quincena en centavos. `0` desactiva la comparación; `1000000` representa $10,000.00 MXN. |
| `GMAIL_EMAIL` | Cuenta opcional de Gmail. También puedes configurarla en Ajustes. |
| `XML_SENDER_EMAIL` | Remitente autorizado para importar CFDI. |

Para importar desde Gmail, configura la cuenta y una contraseña de aplicación en **Ajustes**, y define `XML_SENDER_EMAIL` en `.env`. El botón **Actualizar CFDI** inicia la importación. También puedes ejecutar:

```bash
python import_gmail_xml.py --dry-run
python import_gmail_xml.py --days 30 --max-messages 100
python send_reminders.py
```

Sustituye `python` por el ejecutable del entorno virtual. La ejecución periódica de estos scripts requiere un programador externo; la aplicación no instala tareas automáticamente.

## Obsidian

Coloca la aplicación en `.finanzas/` dentro de tu bóveda. Copia `main.js`, `manifest.json` y `styles.css` de `obsidian-plugin/` a `.obsidian/plugins/finanzas-local/`. Activa **Finanzas** en los complementos de comunidad y configura el ejecutable de Python del entorno virtual en sus ajustes. El complemento utiliza el puerto `5028`.

Consulta [la documentación del complemento](obsidian-plugin/README.md). Los cambios de Python requieren reiniciar el servidor; los cambios del complemento requieren desactivarlo y volverlo a activar.

## Pruebas

```bash
python -m pytest tests/ -q
```

Las pruebas utilizan bases temporales y simulan Gmail. La revisión opcional de interfaz requiere Playwright y Microsoft Edge:

```bash
python -m pip install playwright
python tests/browser_check.py
```

## Datos y publicación

El repositorio contiene código, recursos de interfaz y pruebas con datos sintéticos. `.gitignore` excluye configuración privada, catálogos de usuarios, bases SQLite, respaldos, archivos subidos, registros, cachés y entornos virtuales. Los correos y montos personales no están preconfigurados.

Los datos se guardan en `instance/`. Respalda la base SQLite con la aplicación detenida o mediante la API de respaldo de SQLite. Guarda también `.env` y los archivos subidos en un destino privado. Conserva `SECRET_KEY` para poder descifrar las credenciales de Gmail existentes.

La versión publicable omite las migraciones privadas y la antigua descarga que empaquetaba datos y credenciales. Para distribuir exclusivamente los archivos versionados:

```bash
git archive --format=zip --output=finanzas-source.zip HEAD
```

El servidor de `run.py` escucha solamente en `127.0.0.1`. Un despliegue accesible por internet requiere configurar un servidor WSGI, HTTPS y una clave estable; este repositorio no incluye un despliegue de producción. El estado local puede consultarse en `/api/health`.
