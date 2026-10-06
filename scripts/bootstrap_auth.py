#!/usr/bin/env python3
"""Create local login credentials without storing the password in plaintext."""
import argparse
from getpass import getpass
from pathlib import Path
import re
import secrets
import shutil

from dotenv import dotenv_values, set_key
from werkzeug.security import generate_password_hash


def main():
    parser = argparse.ArgumentParser(description='Configura el acceso local de Finanzas.')
    parser.add_argument('--username', default='admin')
    args = parser.parse_args()
    username = args.username.strip().lower()
    if not re.fullmatch(r'[a-z0-9_.-]{1,64}', username):
        parser.error('El usuario debe tener de 1 a 64 letras, cifras, puntos, guiones o guiones bajos.')
    password = getpass('Contraseña (mínimo 12 caracteres): ')
    if len(password) < 12:
        parser.error('La contraseña debe tener al menos 12 caracteres.')
    if password != getpass('Repite la contraseña: '):
        parser.error('Las contraseñas no coinciden.')
    root = Path(__file__).resolve().parents[1]
    config = root / '.env'
    if not config.exists():
        shutil.copyfile(root / '.env.example', config)
    values = dotenv_values(config)
    if not values.get('SECRET_KEY'):
        set_key(str(config), 'SECRET_KEY', secrets.token_hex(32))
    set_key(str(config), 'AUTH_USERNAME', username)
    set_key(str(config), 'AUTH_PASSWORD_HASH', generate_password_hash(password))
    set_key(str(config), 'AUTH_MODE', 'shared_login')
    config.chmod(0o600)
    print('Acceso configurado en .env. Reinicia Finanzas si ya está ejecutándose.')


if __name__ == '__main__':
    main()
