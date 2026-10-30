# Supervisión persistente de normalización en Modal

La normalización 4K se envía con `Function.spawn()`. El script persiste el ID de
la llamada, hash del origen, perfil y estimación presupuestaria antes de salir.
`scripts/modal_normalization_supervisor.py` consulta las llamadas mediante
`FunctionCall.from_id()` y nunca llama a `cancel()` ni vuelve a enviar trabajo.
El worker guarda las fases y errores en el volumen de Modal.

## Preparación para DigitalOcean (no ejecutar sin autorización)

1. Desplegar el worker revisado y publicar el código aprobado en DigitalOcean.
   No se requiere desplegarlo desde esta preparación.
2. Asegurar que `data/` —los ledgers SQLite existentes, el nuevo almacén de
   supervisión y los JSONL históricos— apunta a almacenamiento persistente,
   legible y escribible por el usuario de la aplicación. No copiar, recrear ni
   vaciar esos registros durante el cambio.
3. Instalar `deploy/systemd/matrix-modal-normalization-supervisor.service`,
   ajustando usuario, directorio y ruta de Python a la instalación real.
   Proporcionar las credenciales de Modal mediante el mecanismo de secretos ya
   utilizado por el servidor; no guardarlas en la unidad ni en el repositorio.
4. Habilitar la unidad en DigitalOcean:

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl enable --now matrix-modal-normalization-supervisor.service
   sudo systemctl status matrix-modal-normalization-supervisor.service
   ```

5. Para que la subida inicial tampoco dependa de la sesión SSH, iniciar el
   comando de envío como una unidad transitoria. Sustituir las rutas por las de
   la instalación real; no ejecutar hasta tener autorización:

   ```sh
   sudo systemd-run --unit=matrix-modal-normalize-once --collect \
     --property=WorkingDirectory=/opt/matrix-motion \
     /opt/matrix-motion/.venv/bin/python scripts/normalize_modal_4k_once.py \
     --source data/incoming/44e426722ccc4f01aaaf8662298b638f.mov \
     --output generated-artifacts/normalization-4k/IMG_1254-2160x3840-30.mp4
   ```

6. Consultar los registros locales con `journalctl -u
   matrix-modal-normalization-supervisor.service -f` y revisar
   `data/modal-execution-supervision.sqlite3`. El supervisor recupera y verifica
   el MP4 local mediante el manifiesto y checksum antes de marcarlo completado.

## Comprobación, diagnóstico y reversión

- Antes de instalar, ejecutar `sudo systemd-analyze verify
  /etc/systemd/system/matrix-modal-normalization-supervisor.service`.
- Tras iniciar, comprobar `systemctl is-active
  matrix-modal-normalization-supervisor.service` y consultar los eventos con
  `journalctl -u matrix-modal-normalization-supervisor.service -n 100`.
  Para una comprobación SQLite de solo lectura, ejecutar
  `sqlite3 data/modal-execution-supervision.sqlite3 'PRAGMA integrity_check;'`.
- El servicio `--watch` solo inspecciona registros persistidos, consulta sus
  IDs existentes, lee progreso/caché y reconcilia resultados terminales. No
  llama a `spawn`, `.remote()` ni `cancel()`, y no importa automáticamente
  llamadas históricas desde el JSONL. Registrar una llamada antigua requiere
  `--attach-call-id` con el ID exacto, el hash/perfil comprobados y el intento
  actual; esa operación no crea un trabajo ni incrementa el contador.
- Para revertir, detener y deshabilitar únicamente el supervisor, restaurar el
  commit previo de la aplicación y reiniciar el servicio web. Conservar todas
  las bases SQLite, JSONL, originales y resultados; no borrar el almacén nuevo.
  Detener systemd no cancela las ejecuciones remotas ya aceptadas.

El código nuevo usa una base de supervisión separada; no elimina ni reinicia el
ledger heredado. La reconciliación de un resultado terminal puede actualizar el
estado/salida/error del registro heredado, pero conserva su contador de intentos
y agrega un evento al historial en vez de sustituir los eventos anteriores.

## Reconectar una llamada histórica

Si se conoce un `fc-...` de una ejecución ya aceptada, registrarlo primero sin
iniciar otro trabajo:

```sh
python scripts/modal_normalization_supervisor.py \
  --attach-call-id fc-ID-REAL \
  --source data/incoming/44e426722ccc4f01aaaf8662298b638f.mov \
  --output generated-artifacts/normalization-4k/IMG_1254-2160x3840-30.mp4 \
  --attempt-number 2
python scripts/modal_normalization_supervisor.py --once
```

El registro adjunto se almacena aparte; el ledger heredado se abre en solo
lectura al comprobar el intento. Los resultados terminales se reconcilian
añadiendo eventos, sin borrar los intentos previos ni incrementar su contador.

El límite de presupuesto es una barrera interna basada en una estimación; no es
un límite de facturación garantizado por Modal. No se realizaron llamadas ni
conversiones al preparar este procedimiento.

## Preflight y tercer intento excepcional

La imagen de Modal comprueba durante la compilación la presencia de FFmpeg,
HEVC, `zscale`, `tonemap` y `libx264`. Cada estado, manifiesto y diagnóstico del
worker registra `worker_code_version` para identificar la revisión que ejecutó
el trabajo. El manifiesto `normalization-v2.json` y su versión esperada hacen
que un worker antiguo falle antes de FFmpeg; no se usa el manifiesto heredado.
El hash local no demuestra por sí solo qué revisión está desplegada, por lo que
el ID de release debe venir del despliegue aprobado.

Primero, en DigitalOcean, el preflight solo consulta el original, FFprobe,
espacio y estimación estática. No crea ledgers ni llama a Modal:

```sh
cd /var/www/matrix-motion
PYTHONPATH="$PWD" /usr/bin/python3 scripts/normalize_modal_4k_once.py \
  --source data/incoming/44e426722ccc4f01aaaf8662298b638f.mov \
  --output generated-artifacts/normalization-4k/IMG_1254-2160x3840-30.mp4 \
  --preflight-only
```

Después de publicar la revisión aprobada y solo con autorización de despliegue,
identificarla en Modal y actualizar el worker:

```sh
cd /var/www/matrix-motion
DEPLOYED_VERSION="$(git rev-parse HEAD)"
MATRIX_MODAL_CODE_VERSION="$DEPLOYED_VERSION" \
  modal deploy video_pipeline/modal_render_app.py
```

Antes de solicitar la única conversión, el intento 2 debe estar adjuntado al
ID histórico exacto, consultado por el supervisor y conciliado como
`cancelled`; la caché debe haberse comprobado sin resultado reutilizable. El
tercer envío solo acepta el SHA-256 que imprima el preflight y ese ID:

```sh
cd /var/www/matrix-motion
PYTHONPATH="$PWD" /usr/bin/python3 scripts/normalize_modal_4k_once.py \
  --source data/incoming/44e426722ccc4f01aaaf8662298b638f.mov \
  --output generated-artifacts/normalization-4k/IMG_1254-2160x3840-30.mp4 \
  --allow-third-attempt \
  --confirm-source-sha256 SHA256_DEL_PREFLIGHT \
  --confirm-cancelled-call-id fc-ID-DEL-INTENTO-2 \
  --confirm-worker-code-version "$DEPLOYED_VERSION"
```

Ese gate exige dos intentos en el ledger, el JSONL previo, estado `cancelled`
del mismo ID en el almacén de supervisión, ausencia de caché reutilizable y
presupuesto bajo el límite interno. Reserva de forma atómica el intento 3; no
hay reintentos automáticos y cualquier intento posterior queda bloqueado.
El preflight local no puede inspeccionar los archivos de DigitalOcean ni la
versión actualmente desplegada de Modal; el manifiesto impide procesar con una
versión Modal distinta de `--confirm-worker-code-version`. Confirme el valor de
`DEPLOYED_VERSION` del despliegue y los datos del host allí antes de autorizar
el envío.
