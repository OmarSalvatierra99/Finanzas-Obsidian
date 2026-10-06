#!/usr/bin/env python3
"""Importa como ingresos los CFDI adjuntos recibidos en Gmail."""

from __future__ import annotations

import argparse
import hashlib
import imaplib
import sys
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

from app import (
    _clean_text,
    _connect_db,
    _money_to_cents,
    _now,
    _parse_date,
    _xml_gmail_settings,
    app,
    XML_SENDER_EMAIL,
)


MAX_XML_BYTES = 5 * 1024 * 1024
IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
GMAIL_ERRORS = (imaplib.IMAP4.error, OSError, RuntimeError)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_first(root: ET.Element, name: str) -> ET.Element | None:
    return next((node for node in root.iter() if _local_name(node.tag) == name), None)


def parse_cfdi_xml(payload: bytes, filename: str) -> dict:
    if not payload or len(payload) > MAX_XML_BYTES:
        raise ValueError("XML vacio o mayor a 5 MB.")
    lowered = payload[:4096].lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("XML con declaraciones no permitidas.")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        raise ValueError("XML corrupto o ilegible.") from None
    if _local_name(root.tag) != "Comprobante":
        raise ValueError("El adjunto no es un CFDI.")

    kind = (root.attrib.get("TipoDeComprobante") or "").upper()
    # ponytail: solo ingresos/nómina; agregar clasificación por RFC si se requieren egresos.
    if kind not in {"I", "N"}:
        raise ValueError("El CFDI no corresponde a un ingreso o nomina.")
    currency = (root.attrib.get("Moneda") or "MXN").upper()
    if currency != "MXN":
        raise ValueError("El CFDI no esta expresado en MXN.")

    issuer = _find_first(root, "Emisor")
    stamp = _find_first(root, "TimbreFiscalDigital")
    issuer_name = (issuer.attrib.get("Nombre") if issuer is not None else "") or "Emisor no identificado"
    has_payroll = _find_first(root, "Nomina") is not None or kind == "N"
    return {
        "xml_hash": hashlib.sha256(payload).hexdigest(),
        "uuid": ((stamp.attrib.get("UUID") if stamp is not None else "") or "").upper() or None,
        "filename": filename[:255],
        "amount_cents": _money_to_cents(root.attrib.get("Total")),
        "occurred_on": _parse_date(str(root.attrib.get("Fecha") or "")[:10], "el CFDI"),
        "category": "Sueldo" if has_payroll else "Otro ingreso",
        "description": _clean_text(
            f"{'Nómina' if has_payroll else 'CFDI'} · {issuer_name}",
            "la descripcion",
            limit=160,
        ),
    }


def import_cfdi(connection, cfdi: dict, *, dry_run: bool = False) -> bool:
    with connection:
        if not dry_run and not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        duplicate = connection.execute(
            "SELECT 1 FROM xml_imports WHERE xml_hash = ? OR (? IS NOT NULL AND uuid = ?)",
            (cfdi["xml_hash"], cfdi["uuid"], cfdi["uuid"]),
        ).fetchone()
        if duplicate:
            return False
        if dry_run:
            return True
        movement = connection.execute(
            """
            INSERT INTO movements (kind, category, description, amount_cents, occurred_on, created_at)
            VALUES ('income', ?, ?, ?, ?, ?)
            """,
            (
                cfdi["category"],
                cfdi["description"],
                cfdi["amount_cents"],
                cfdi["occurred_on"],
                _now(),
            ),
        )
        connection.execute(
            """
            INSERT INTO xml_imports (xml_hash, uuid, filename, movement_id, imported_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cfdi["xml_hash"], cfdi["uuid"], cfdi["filename"], movement.lastrowid, _now()),
        )
    return True


def gmail_xml_attachments(settings: dict, days: int, max_messages: int):
    if not XML_SENDER_EMAIL:
        raise RuntimeError("Configura XML_SENDER_EMAIL antes de importar correo.")
    since = date.today() - timedelta(days=days)
    since_imap = f"{since.day:02d}-{IMAP_MONTHS[since.month - 1]}-{since.year}"
    mailbox = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
    try:
        mailbox.login(settings["email"], settings["password"])
        status, _ = mailbox.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("No se pudo abrir la bandeja de entrada.")
        status, result = mailbox.uid(
            "search", None, "SINCE", since_imap, "FROM", f'"{XML_SENDER_EMAIL}"'
        )
        if status != "OK":
            raise RuntimeError("No se pudo buscar correo en Gmail.")
        for uid in result[0].split()[-max_messages:]:
            status, fetched = mailbox.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            message = BytesParser(policy=policy.default).parsebytes(fetched[0][1])
            if parseaddr(message.get("From", ""))[1].lower() != XML_SENDER_EMAIL:
                continue
            for part in message.walk():
                filename = part.get_filename() or ""
                if filename.lower().endswith(".xml"):
                    yield filename, part.get_payload(decode=True) or b""
    finally:
        try:
            mailbox.logout()
        except imaplib.IMAP4.error:
            pass


def sync_cfdi(
    connection,
    settings: dict,
    *,
    days: int = 3650,
    max_messages: int = 5000,
    dry_run: bool = False,
) -> dict:
    result = {"imported": 0, "skipped": 0, "invalid": 0}
    for filename, payload in gmail_xml_attachments(settings, days, max_messages):
        try:
            cfdi = parse_cfdi_xml(payload, filename)
        except ValueError:
            result["invalid"] += 1
            continue
        if import_cfdi(connection, cfdi, dry_run=dry_run):
            result["imported"] += 1
        else:
            result["skipped"] += 1
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=3650, help="Dias de correo a revisar (3650).")
    parser.add_argument("--max-messages", type=int, default=5000, help="Maximo de mensajes (5000).")
    parser.add_argument("--dry-run", action="store_true", help="Muestra sin guardar movimientos.")
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 3650 or not 1 <= args.max_messages <= 5000:
        parser.error("days y max-messages estan fuera del rango permitido")

    connection = _connect_db(app)
    try:
        settings = _xml_gmail_settings(connection, app)
        if not settings["enabled"]:
            print("Importacion XML desactivada desde Configuracion.")
            return 0
        if not settings["email"] or not settings["password"]:
            print("Importacion omitida: configura Gmail desde la aplicacion.")
            return 0
        result = sync_cfdi(
            connection,
            settings,
            days=args.days,
            max_messages=args.max_messages,
            dry_run=args.dry_run,
        )
    except GMAIL_ERRORS as exc:
        print(f"No se pudo leer Gmail: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    print(
        f"Resultado: {result['imported']} nuevo(s), {result['skipped']} duplicado(s), "
        f"{result['invalid']} invalido(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
