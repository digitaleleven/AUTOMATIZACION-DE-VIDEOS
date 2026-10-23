# Automatización local de vídeos verticales

Este proyecto procesa MP4 locales para preparar una edición 9:16 con OpenAI y Shotstack. Después de validar que el render terminó correctamente, puede crear una copia nueva en Procesados de Google Drive. No modifica el original ni amplía permisos OAuth.

## Implementado

- Validación de MP4, duración, tamaño, audio, códecs, dimensiones y FPS mediante `ffprobe`.
- Extracción temporal de audio con FFmpeg en bloques de 15 minutos.
- Transcripción `whisper-1` en `verbose_json`, solicitando timestamps por palabra y segmento. La salida se normaliza; si faltan tiempos por palabra, se estiman dentro del segmento.
- Captions SRT de 2–4 palabras sincronizados con el audio.
- Edición Shotstack con `rich-caption`, animación/highlight por palabra, trazo legible y subtítulos en zona inferior segura.
- Estilo configurable en `video_style.json`: fuente, colores, animación, posición, emojis asociados manualmente y logo local opcional.
- Salida MP4 1080 × 1920 a 30 fps. Las fuentes con otra relación de aspecto usan `contain` para no cortar caras; solo se usa crop si el formato ya es prácticamente 9:16.
- Ingest de MP4, SRT y, opcionalmente, logo a Shotstack. Resultado descargado como archivo nuevo; el original no se modifica ni sobrescribe.
- Carga reanudable del render validado como un archivo nuevo en la carpeta Procesados. Drive solo se usa para crear ese archivo: no se enumeran ni descargan vídeos de entrada.
- SQLite local (`data/processing.sqlite3`) para detectar vídeos ya completados, reanudar un render existente y permitir como máximo un reintento manual.
- Límites por defecto: un vídeo por ejecución, 500 MB, 30 minutos de vídeo, dos bloques de transcripción y un nuevo render por intento. Se pueden ajustar en `video_style.json`.
- `requirements.txt` fija las versiones usadas, incluidos los requisitos de ejecución de OpenAI/Requests.

La animación activa destaca la palabra que se está pronunciando; el proyecto no realiza análisis semántico para decidir qué palabras son “importantes”. Los emojis son opcionales y solo se añaden si se configuran en `emoji_by_keyword`.

## Shotstack: Stage exclusivamente

La configuración y el cliente rechazan cualquier entorno que no sea `stage`; no hay una ruta de producción habilitada. Replit Secrets informa que existen `OPENAI_API_KEY` y `SHOTSTACK_API_KEY`, pero no se han leído ni mostrado sus valores. Tampoco se ha comprobado mediante una llamada que la clave de Shotstack corresponda a Stage, porque no se han realizado solicitudes externas.

La transcripción, la carga/render de Shotstack y la subida final siguen detrás de `--authorize-paid-apis`. La transcripción real puede tener coste; el MP4, los subtítulos y el logo opcional se envían a Shotstack Ingest y se solicita un render Stage. El render solo se sube a Drive cuando su estado es `done` y el archivo pasa la validación local. Los reintentos automáticos del SDK de OpenAI están desactivados y una nueva tentativa requiere `--retry-failed` y la autorización explícita.

Antes de contactar con proveedores, el proceso comprueba que existan `OPENAI_API_KEY`, `SHOTSTACK_API_KEY` y `GOOGLE_OAUTH_TOKEN`. El token de Drive debe poder crear archivos en Procesados. Si falta o Drive rechaza el destino, el proceso se detiene sin ampliar scopes.

## Pruebas sin llamadas externas

```bash
python -m unittest discover -s tests -v
python -m video_pipeline --dry-run
```

Las pruebas usan un MP4 sintético generado y comprobado localmente, mocks de las APIs y payloads construidos sin enviarlos. Validan los límites, estado/idempotencia, SRT, animación, logo opcional, creación reanudable de un nuevo archivo, rechazo de Production y calidad técnica de un MP4 sintético 1080 × 1920. **No son prueba de una transcripción ni un render real de Shotstack o Drive.**

## Primer render real (todavía no autorizado)

1. Coloca el MP4 local de prueba en `data/incoming/`.
2. Comprueba su preflight sin llamadas remotas:

   ```bash
   python -m video_pipeline --dry-run --input-dir data/incoming
   ```

3. Completa las tarifas vigentes de tu cuenta en `video_style.json` si quieres que el preflight calcule un importe USD. Sin esas tarifas informa que el importe es desconocido; no inventa precios.
4. Solo cuando autorices el coste y el envío del archivo, ejecuta una sola vez:

   ```bash
   python -m video_pipeline --process data/incoming/prueba.mp4 --authorize-paid-apis
   ```

El render validado se guarda en `outputs/` y se carga como archivo nuevo en Procesados. Si el vídeo ya está marcado como completado, el hash impide procesarlo de nuevo. Para un fallo, revisa primero el estado y usa `--retry-failed` solo si autorizas otra posible llamada de pago.

## Pendiente y limitaciones

- No hay vídeo real de usuario procesado y no se ha hecho ninguna llamada real a OpenAI o Shotstack. El secreto de Shotstack existe, pero no se ha verificado su correspondencia con Stage mediante API.
- Falta revisión visual y auditiva del primer resultado real; también hace falta comprobar la respuesta real de Shotstack y sus defaults de códec/fps. Los renders remotos no se simulan como verificados.
- La composición centra y conserva fuentes no 9:16 con letterboxing; no hace seguimiento facial ni reencuadre semántico.
- El logo se incorpora si se especifica un archivo local en `video_style.json`. El acceso al logo de Drive no está implementado.
- El importe USD es desconocido hasta que se configuren precios actuales por minuto para ambas APIs en `video_style.json`; revisa también si tu plan cobra conceptos adicionales de almacenamiento, transferencia o impuestos.
- Google Drive no lista ni descarga entradas y no amplía OAuth. Se necesita un token OAuth vigente con acceso de creación en Procesados; la autorización/selección de carpetas y archivos de entrada sigue pendiente por separado.

## Referencias consultadas

- OpenAI Transcriptions: https://platform.openai.com/docs/api-reference/audio/createTranscription
- Shotstack Rich Captions: https://shotstack.io/docs/guide/architecting-an-application/rich-captions/
- Shotstack Positioning: https://shotstack.io/docs/guide/architecting-an-application/positioning/
- Shotstack Ingest: https://shotstack.io/docs/guide/ingesting-footage/sources.md
