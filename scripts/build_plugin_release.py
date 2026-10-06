#!/usr/bin/env python3
"""Validate and package the exact root assets used by the Obsidian catalog."""
import json
from pathlib import Path
import re
import shutil
import zipfile


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    assert re.fullmatch(r'[a-z0-9-]+', manifest['id']) and 'obsidian' not in manifest['id']
    assert re.fullmatch(r'\d+\.\d+\.\d+', manifest['version'])
    assert manifest['isDesktopOnly'] is True
    assert len(manifest['description']) <= 250 and manifest['description'].endswith('.')
    versions = json.loads((root / 'versions.json').read_text(encoding='utf-8'))
    assert versions[manifest['version']] == manifest['minAppVersion']
    assert (root / 'LICENSE').is_file(), 'Choose and add a LICENSE before publishing.'
    destination = root / '.cache' / 'plugin-release' / manifest['version']
    destination.mkdir(parents=True, exist_ok=True)
    assets = ('main.js', 'manifest.json', 'styles.css')
    for name in assets:
        source = root / name
        assert source.is_file()
        shutil.copyfile(source, destination / name)
    with zipfile.ZipFile(destination / 'finanzas-local.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in assets:
            archive.write(destination / name, f"{manifest['id']}/{name}")
    print(f"Assets ready for GitHub release tag {manifest['version']}: {destination}")


if __name__ == '__main__':
    main()
