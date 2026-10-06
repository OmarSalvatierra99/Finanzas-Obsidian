# Publicar en el catálogo de Obsidian

El repositorio contiene `manifest.json`, `main.js`, `styles.css`, `versions.json`, README y licencia MIT en la raíz.

## Releases

Actualiza `version` en los dos manifiestos y `versions.json` antes de una nueva versión. Mantén `main.js` y `styles.css` de la raíz sincronizados con `obsidian-plugin/`. Ejecuta:

```bash
python scripts/build_plugin_release.py
```

Confirma los cambios y publica un tag que coincida exactamente con la versión del manifiesto, sin prefijo `v`:

```bash
git tag 1.0.0
git push origin main
git push origin 1.0.0
```

El workflow `Publish Obsidian release` crea la release de GitHub y adjunta `main.js`, `manifest.json` y `styles.css`. Utiliza el token temporal de GitHub Actions; no requiere guardar un token personal en el repositorio.

## Envío inicial al catálogo

1. Inicia sesión en <https://community.obsidian.md> con tu cuenta de Obsidian.
2. Vincula tu cuenta de GitHub en el perfil.
3. Añade el complemento desde la sección Plugins indicando el repositorio público.
4. Revisa y corrige los resultados del escaneo. Selecciona Publish cuando el formulario esté listo.

La publicación en GitHub y el envío al catálogo son pasos distintos. La disponibilidad dentro de Obsidian depende de su proceso de revisión.

Referencia: [guía oficial de envío](https://docs.obsidian.md/plugins/releasing/submit-plugin) y [políticas para desarrolladores](https://docs.obsidian.md/community-directory/developer-policies).
