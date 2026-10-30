# Primer render MATRIX desde el máster normalizado

El ejecutor está fijado al archivo `generated-artifacts/normalization-4k/IMG_1254-2160x3840-30.mp4` y a su SHA-256 aprobado. No acepta otro contenido: antes de cualquier API comprueba checksum, duración, 2160×3840/30 FPS, H.264, AAC estéreo 48 kHz, y SDR BT.709 limitado; además decodifica localmente el archivo completo sin escribirlo.

La entrada normalizada se entrega directamente a Whisper, Gemini y al render Modal. No se llama a `normalize_input()` ni se vuelve a convertir HDR. El plan aplica subtítulos dinámicos rosa pastel; los cortes sugeridos por Gemini solo se incorporan cuando coinciden con silencios locales protegidos por las palabras transcritas, y los zooms suaves se pasan al compositor existente.

## Único procedimiento en DigitalOcean

Desde la raíz `/var/www/matrix-motion`, ejecutar primero el preflight sin llamadas a proveedores:

```bash
cd /var/www/matrix-motion
/usr/bin/python3 -m scripts.render_normalized_4k_once
```

Continuar solo si termina con `status: preflight_ready`, hash coincidente, perfil 2160×3840/30, decodificación completa y estimación bajo el límite interno. Para autorizar la única ejecución completa, ejecutar:

```bash
/usr/bin/python3 -m scripts.render_normalized_4k_once \
  --execute \
  --authorize-openai \
  --authorize-gemini \
  --authorize-modal \
  --authorize-r2 \
  --acknowledge-estimated-cost
```

El proceso usa el ledger compartido `data/modal_processing.sqlite3`, consulta en modo solo lectura el historial `data/processing.sqlite3` para `IMG_1254.MOV`, reclama una sola tentativa y no tiene reintentos automáticos. Un trabajo previo en el panel bloquea la ejecución hasta conciliarlo. El MP4 queda en `outputs/`; los eventos, transcripción, análisis y plan se guardan bajo `data/normalized-render-once/<job_id>/`. R2 conserva el MP4 privado.

La estimación existente para 59,98 s es aproximadamente **USD 1,2359**, bajo el límite interno de **USD 1,50**. Es una estimación de tarifas configuradas, no un tope de facturación garantizado; el coste real de Modal, Gemini, Whisper y almacenamiento/egress de R2 puede diferir. El comando de ejecución requiere aceptar explícitamente ese riesgo y no se ejecuta por instalar ni arrancar la aplicación.
