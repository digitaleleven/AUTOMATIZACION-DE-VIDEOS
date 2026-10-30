# Reanudar el análisis Gemini del primer render 4K

El flujo conserva el máster normalizado `generated-artifacts/normalization-4k/IMG_1254-2160x3840-30.mp4` y el intento persistente `7be6ba1a33c34124bb541e3101a7cca0`. Esta recuperación solo admite ese ID, ledger en `failed` con dos intentos, perfil de edición idéntico, copia histórica fallida del intento 1 y checkpoint JSONL que terminó en `analyzing_with_gemini`. Debe existir una transcripción válida en el directorio de trabajo de ese mismo ID.

## Comportamiento de recuperación

- Reutiliza `transcript.json`; no llama a Whisper ni repite normalización.
- Reutiliza `silence-candidates.json` si existe. El checkpoint actual no lo guardó; en ese caso no repite detección local y desactiva los cortes que dependían de esa evidencia. Los subtítulos y zooms derivados del análisis sí pueden continuar.
- Añade eventos al JSONL existente y guarda el error técnico de Gemini solo como fase, HTTP, categoría y estado de proveedor permitido. No registra el cuerpo del proveedor, claves ni tokens.
- Cambia de forma atómica el mismo ledger fallido a procesamiento y guarda una instantánea del fallo del intento 2. El total de intentos permanece en 2. No permite una segunda reanudación; trabajos activos, completados, inciertos, con otro perfil o con evidencia de checkpoint inconsistente quedan bloqueados.
- La estimación solo cubre el nuevo análisis Gemini, render Modal, salida/almacenamiento y reserva: aproximadamente **USD 0,8644** con las tarifas y topes locales actuales. No incluye el coste ya incurrido por el fallo Gemini anterior, cuyo importe no se conoce. Es una estimación interna, no un límite facturable garantizado.

## DigitalOcean: comprobar sin iniciar servicios

Desde `/var/www/matrix-motion`, con el código actualizado y el `.env` existente:

```bash
cd /var/www/matrix-motion
/usr/bin/python3 -m scripts.render_normalized_4k_once \
  --resume-job-id 7be6ba1a33c34124bb541e3101a7cca0
```

Solo continúe si la salida `PREFLIGHT` informa `preflight_ready`, indica reutilización de la transcripción, coste aproximado de USD 0,8644 y no enumera bloqueos. El preflight no contacta OpenAI, Gemini, Modal ni R2.

## Ejecución única — requiere autorización operativa de la propietaria

Este comando sí consume servicios: realiza un análisis Gemini, un render 4K en Modal y la subida a R2. No ejecutarlo como parte de la instalación o del reinicio de systemd:

```bash
cd /var/www/matrix-motion
/usr/bin/python3 -m scripts.render_normalized_4k_once \
  --execute \
  --resume-job-id 7be6ba1a33c34124bb541e3101a7cca0 \
  --authorize-gemini \
  --authorize-modal \
  --authorize-r2 \
  --acknowledge-estimated-cost
```

No incluye `--authorize-openai` ni `--retry-failed`: no se vuelve a transcribir y no se crea el intento 3. No hay reintentos automáticos. Si el preflight detecta una fase activa/completada, ausencia de la instantánea del primer fallo, salida ya existente o un checkpoint distinto, deténgase; no edite el ledger ni elimine eventos para desbloquearlo.
