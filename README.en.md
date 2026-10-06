# Finanzas

[Español](README.md)

A local personal finance application built with Flask and SQLite. Organize debts, browse a payment calendar, record installments, and import income from Mexican CFDI invoices. Includes a desktop Obsidian plugin. The application interface is in Spanish and amounts use MXN.

## Obsidian plugin

The **Finanzas** plugin opens this application in a desktop Obsidian tab. Manually install the Python application in `.finanzas/` inside your vault using the steps below. Installing the catalog plugin does not install Python, Flask, or the backend application.

Configure the Python executable in the plugin settings. Open Finanzas using the wallet icon or the **Abrir Finanzas** and **Abrir deudas** commands. Opening a view may start the existing local server; vault startup is disabled by default and can be enabled in Settings. The server keeps running when you close the view.

### Dependencies, network access, and privacy

- Desktop only: uses Node.js APIs and an Electron webview.
- Connects to `http://127.0.0.1:5028` on your computer to display Finanzas and check its status.
- Runs your configured Python executable, which may be outside the vault, and `.finanzas/run.py` inside the vault. Does not automatically install or update dependencies.
- Has no telemetry and does not send notes or financial data to an external service. The optional Gmail integration belongs to the Python application and uses Gmail IMAP/SMTP when configured.
- Requires the local login you set up; using the plugin does not require a Finanzas service account.
- Saves local plugin options in `data.json`. Distributions and releases omit that file and private server configuration.

## Features

- Monthly calendar with editable amounts and dates, desktop drag and drop, and a mobile daily view.
- One-time debts with a pending total. Completing a debt removes it from the list.
- Recurring reminders: completing a date removes that occurrence while preserving future reminders.
- Installment debts with scheduled payments, outstanding balances, and payment history.
- Optional comparison of scheduled payments against a configurable twice-monthly income.
- Optional Gmail CFDI import with duplicate detection.
- Password authentication, CSRF checks, and encrypted Gmail credentials in the local database.
- An Obsidian plugin that opens the app in a tab and follows the vault theme.

## Requirements

Python 3.11 or newer. Desktop Obsidian is optional. Initial installation requires internet access to download dependencies.

## Installation

From the repository directory on Windows:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/bootstrap_auth.py --username admin
.\.venv\Scripts\python.exe run.py
```

On Linux or macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/bootstrap_auth.py --username admin
.venv/bin/python run.py
```

The setup script prompts for a password, creates `.env` from `.env.example`, and stores a password hash and a random session key. Open <http://127.0.0.1:5028> and sign in with your chosen username. A new installation starts without debts or transactions.

## Configuration

Store private settings in `.env`, which is excluded from Git. Restart the server after editing it.

| Variable | Purpose |
| --- | --- |
| `PROJECT_NAME` | Application name. |
| `PORT` | Local port; defaults to `5028`. |
| `APP_TIMEZONE` | Time zone; defaults to `America/Mexico_City`. |
| `AUTH_USERNAME` / `AUTH_PASSWORD_HASH` | Login configured by the setup script. |
| `SECRET_KEY` | Session and encryption key. Keep it with your private backups. |
| `DATABASE_URL` | SQLite location; defaults to `sqlite:///instance/finanzas.sqlite3`. |
| `QUINCENAL_INCOME_CENTS` | Income per half month in cents. `0` disables comparison; `1000000` represents MXN $10,000.00. |
| `GMAIL_EMAIL` | Optional Gmail account. You can also set it in Settings. |
| `XML_SENDER_EMAIL` | Authorized sender for CFDI imports. |

For Gmail imports, configure the account and an app password under **Ajustes**, then set `XML_SENDER_EMAIL` in `.env`. **Actualizar CFDI** runs the importer. You can also run:

```bash
python import_gmail_xml.py --dry-run
python import_gmail_xml.py --days 30 --max-messages 100
python send_reminders.py
```

Replace `python` with your virtual environment executable. Periodic execution requires an external scheduler; the application does not install scheduled jobs automatically.

## Obsidian

Place the application in `.finanzas/` inside your vault. Copy `main.js`, `manifest.json`, and `styles.css` from the repository root into `.obsidian/plugins/finanzas-local/`. Enable **Finanzas** under community plugins and configure the virtual environment Python executable in the plugin settings. The plugin uses port `5028`.

Python changes require a server restart; plugin changes require disabling and enabling the plugin again.

## Tests

```bash
python -m pytest tests/ -q
```

Tests use temporary databases and mock Gmail. The optional browser check requires Playwright and Microsoft Edge:

```bash
python -m pip install playwright
python tests/browser_check.py
```

## Data and publishing

The repository contains source code, interface assets, and tests with synthetic data. `.gitignore` excludes private configuration, user catalogs, SQLite databases, backups, uploads, logs, caches, and virtual environments. Personal email addresses and financial amounts are not preconfigured.

Data is stored under `instance/`. Back up SQLite while the application is stopped or use SQLite's backup API. Keep `.env` and uploaded files in a private backup destination as well. Preserve `SECRET_KEY` to decrypt existing Gmail credentials.

The publishable version omits private migrations and the old download that bundled data and credentials. To distribute only versioned files:

```bash
git archive --format=zip --output=finanzas-source.zip HEAD
```

The server in `run.py` listens only on `127.0.0.1`. An internet-facing deployment requires a WSGI server, HTTPS, and a stable secret key; this repository does not include a production deployment. Local health status is available at `/api/health`.

## License

[MIT](LICENSE).
