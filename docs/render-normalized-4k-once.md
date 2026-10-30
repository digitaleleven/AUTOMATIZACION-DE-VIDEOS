# Primer render MATRIX desde el máster normalizado

El ejecutor está fijado al archivo `generated-artifacts/normalization-4k/IMG_1254-2160x3840-30.mp4` y a su SHA-256 aprobado. No acepta otro contenido: antes de cualquier API comprueba checksum, duración, 2160×3840/30 FPS, H.264, AAC estéreo 48 kHz, y SDR BT.709 limitado; además decodifica localmente el archivo completo sin escribirlo.

La entrada normalizada se entrega directamente a Whisper, Gemini y al render Modal. No se llama a `normalize_input()` ni se vuelve a convertir HDR. El plan aplica subtítulos dinámicos rosa pastel; los cortes sugeridos por Gemini solo se incorporan cuando coinciden con silencios locales protegidos por las palabras transcritas, y los zooms suaves se pasan al compositor existente.

El ejecutor carga `/var/www/matrix-motion/.env` antes de importar los clientes de proveedores. Nunca imprime valores y prioriza las variables ya definidas por el servicio. `python-dotenv==1.2.4` está fijado en `requirements.txt`; instala las dependencias como parte de la actualización normal del código. Mantén `.env` fuera de Git.

## Único comando autorizado de ejecución en DigitalOcean

Desde la raíz `/var/www/matrix-motion`, ejecutar una sola vez este comando después de actualizar el código y sus dependencias:

```bash
cd /var/www/matrix-motion &&
/usr/bin/python3 -m scripts.render_normalized_4k_once \
  --execute \
  --authorize-openai \
  --authorize-gemini \
  --authorize-modal \
  --authorize-r2 \
  --acknowledge-estimated-cost
```

En esa misma invocación, el ejecutor realiza el preflight completo antes de llamar a los proveedores y se detiene si cualquier control falla. Los fallos anteriores reconciliados se conservan y no bloquean por sí solos; una fila completada, activa, incierta, o cualquier intento ya registrado para el hash del máster normalizado bloquea el envío. El historial se consulta en solo lectura. El ledger de Modal reclama como máximo una tentativa para ese hash, por lo que dos invocaciones concurrentes no pueden enviar el mismo trabajo; no hay reintentos automáticos. No iniciar otro trabajo de MATRIX para `IMG_1254.MOV` mientras corre esta ejecución.

La operación reutiliza el MP4 SDR normalizado y no vuelve a convertir HDR. El MP4 queda en `outputs/`; los eventos, transcripción, análisis y plan se guardan bajo `data/normalized-render-once/<job_id>/`. R2 conserva el MP4 privado.

La estimación existente para 59,98 s es aproximadamente **USD 1,2359**, bajo el límite interno de **USD 1,50**. Es una estimación de tarifas configuradas, no un tope de facturación garantizado; el coste real de Modal, Gemini, Whisper y almacenamiento/egress de R2 puede diferir. El comando de ejecución requiere aceptar explícitamente ese riesgo y no se ejecuta por instalar ni arrancar la aplicación.
