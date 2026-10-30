# Primer render MATRIX desde el máster normalizado

El ejecutor está fijado al archivo `generated-artifacts/normalization-4k/IMG_1254-2160x3840-30.mp4` y a su SHA-256 aprobado. No acepta otro contenido: antes de cualquier API comprueba checksum, duración, 2160×3840/30 FPS, H.264, AAC estéreo 48 kHz, y SDR BT.709 limitado; además decodifica localmente el archivo completo sin escribirlo.

La entrada normalizada se entrega directamente a Whisper, Gemini y al render Modal. No se llama a `normalize_input()` ni se vuelve a convertir HDR. El plan aplica subtítulos dinámicos rosa pastel; los cortes sugeridos por Gemini solo se incorporan cuando coinciden con silencios locales protegidos por las palabras transcritas, y los zooms suaves se pasan al compositor existente.

El ejecutor carga `/var/www/matrix-motion/.env` antes de importar los clientes de proveedores. Nunca imprime valores y prioriza las variables ya definidas por el servicio. `python-dotenv==1.2.4` está fijado en `requirements.txt`; instala las dependencias como parte de la actualización normal del código. Mantén `.env` fuera de Git.

## Único comando autorizado de ejecución en DigitalOcean

Desde la raíz `/var/www/matrix-motion`, para el fallo confirmado de autenticación, ejecutar una sola vez este comando después de actualizar el código y sus dependencias:

```bash
cd /var/www/matrix-motion &&
/usr/bin/python3 -m scripts.render_normalized_4k_once \
  --execute \
  --retry-failed \
  --authorize-openai \
  --authorize-gemini \
  --authorize-modal \
  --authorize-r2 \
  --acknowledge-estimated-cost
```

En esa invocación, el ejecutor exige `--retry-failed` y acepta únicamente el ledger exacto del máster normalizado con estado `failed` y un intento previo. La transición a intento 2 se reclama de forma atómica; la fila histórica fallida se copia antes de actualizarla a una tabla de historial en la misma base SQLite. Una fila activa, completada, incierta, ausente, con más de un intento, o un segundo uso de la repetición queda bloqueado. El historial anterior de la aplicación se consulta en solo lectura y no se borra. No hay reintentos automáticos. No iniciar otro trabajo de MATRIX para `IMG_1254.MOV` mientras corre esta ejecución.

Para comprobar primero que el reintento está autorizado sin llamar a proveedores, ejecutar el mismo módulo con `--retry-failed` pero sin `--execute` ni autorizaciones de servicios. La ejecución real requiere las cinco autorizaciones explícitas que aparecen en el comando de arriba.

La operación reutiliza el MP4 SDR normalizado y no vuelve a convertir HDR. El MP4 queda en `outputs/`; los eventos, transcripción, análisis y plan se guardan bajo `data/normalized-render-once/<job_id>/`. R2 conserva el MP4 privado.

La estimación existente para 59,98 s es aproximadamente **USD 1,2359**, bajo el límite interno de **USD 1,50**. Es una estimación de tarifas configuradas, no un tope de facturación garantizado; el coste real de Modal, Gemini, Whisper y almacenamiento/egress de R2 puede diferir. El comando de ejecución requiere aceptar explícitamente ese riesgo y no se ejecuta por instalar ni arrancar la aplicación.
