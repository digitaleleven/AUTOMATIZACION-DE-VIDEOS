# MATRIX MOTION — prueba aislada de Remotion

Este paquete no forma parte del pipeline Python, del panel ni del sandbox de diseño.
No llama a Google Drive, OpenAI ni Shotstack.

## Requisitos de la prueba de Fase 1

- `remotion` y todos los paquetes `@remotion/*` directos fijados en `4.0.538`.
- React y React DOM fijados en `19.1.0`; tipos y TypeScript también tienen versión exacta.
- `pnpm-lock.yaml` fija el árbol reproducible; el paquete no añade dependencias al proyecto Python.
- Entrada fija: `data/incoming/608c74727e18449da2899f75d426f8c9.mov`.
- El SHA-256 auditado se comprueba antes y después.
- Se crean una copia de trabajo y un WAV temporal dentro de `generated-artifacts/`.
- El MP4 se escribe en una carpeta exclusiva de ejecución; nunca se reutiliza una ruta.
- Configuración: 1080×1920, 50 fps, 285 fotogramas, concurrencia 1, H.264/AAC.
- La composición usa tres segmentos locales, dos fundidos, zoom dinámico y un sticker SVG.
- Chromium se ejecuta con red en segundo plano desactivada y resolución de hosts externos bloqueada.
- Se omite `licenseKey` para no solicitar telemetría de Remotion en el render server-side.
- Un error detiene la ejecución; no hay reintentos ni otro render automático.

## Comandos

Desde esta carpeta, verificar las dependencias con `pnpm install --frozen-lockfile`.
La comprobación de tipos es `pnpm run typecheck`. El único comando de render autorizado
para esta prueba es:

```sh
pnpm run render:sample
```

La salida, el informe JSON, los registros y una hoja de contacto quedan en
`generated-artifacts/remotion/<id-de-ejecución>/`. Los medios generados y `node_modules`
están excluidos de Git y del despliegue.
