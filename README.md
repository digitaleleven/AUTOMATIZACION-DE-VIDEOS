# MATRIX MOTION — VIDEO AUTOMATION STUDIO

«De vídeo original a contenido listo para publicar». El proyecto acepta vídeos locales `.MP4` y `.MOV` y prepara una edición 9:16 con OpenAI y Shotstack. La interfaz web guarda el resultado únicamente en `outputs/`; el comando CLI conserva, por defecto, la opción de crear una copia nueva en Procesados de Google Drive. Ningún flujo modifica el original ni amplía permisos OAuth.

## Implementado

- Panel web en español, adaptable y accesible desde Replit Preview, con biblioteca local, reproducción original/editado, estados y errores reales, estimaciones no inventadas y controles de estilo.
- Validación de MP4 y MOV, duración, tamaño, audio, H.264/HEVC, dimensiones, rotación y FPS mediante `ffprobe`.
- Compatibilidad iPhone: conserva sin conversión los MP4/MOV H.264 con AAC y orientación normal; convierte HEVC a H.264 de alta calidad cuando hace falta para edición, y convierte audio no AAC a AAC. Las conversiones por códec, audio u orientación son temporales; la copia original queda intacta.
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
- `requirements.txt` fija las versiones usadas. El panel y su API usan la biblioteca estándar de Python y no añaden dependencias web.

La animación activa destaca la palabra que se está pronunciando; el proyecto no realiza análisis semántico para decidir qué palabras son “importantes”. Los emojis son opcionales y solo se añaden si se configuran en `emoji_by_keyword`.

## Shotstack: Stage exclusivamente

La configuración y el cliente rechazan cualquier entorno que no sea `stage`; no hay una ruta de producción habilitada. Replit Secrets informa que existen `OPENAI_API_KEY` y `SHOTSTACK_API_KEY`, pero no se han leído ni mostrado sus valores. Tampoco se ha comprobado mediante una llamada que la clave de Shotstack corresponda a Stage, porque no se han realizado solicitudes externas.

La transcripción y la carga/render de Shotstack siguen detrás de una autorización explícita. El panel nunca sube a Drive; el CLI sube solo si no se indica `--local-only`, y requiere `GOOGLE_OAUTH_TOKEN`. El render destinado a Drive se sube únicamente cuando su estado es `done` y el archivo pasa la validación local. Los reintentos automáticos del SDK de OpenAI están desactivados.

Antes de contactar con proveedores, el proceso comprueba que existan `OPENAI_API_KEY`, `SHOTSTACK_API_KEY` y `GOOGLE_OAUTH_TOKEN`. El token de Drive debe poder crear archivos en Procesados. Si falta o Drive rechaza el destino, el proceso se detiene sin ampliar scopes.

## Google Drive: estado y requisitos pendientes

No se ha hecho ninguna llamada real a Drive. Los IDs configurados son:

| Uso | ID |
| --- | --- |
| `01_ENTRADA` | `13xrMgSYEOkGANxrUYbmYw1t6TdvXWRYg` |
| `02_PROCESADOS` | `1J6Z0bsWb2dQNKY4hyq4xl7tigtm_pQKN` |
| `RECURSOS/LOGOS` | `1t46UE2Mog9JRDSe9TzoPxfs96nE9qDTW` |

El estado del código es más limitado que una automatización de carpetas:

- `video_pipeline/drive.py` implementa solo la creación reanudable de un archivo nuevo. El CLI la llama después de validar un render y usa `02_PROCESADOS`; no actualiza ni elimina el original.
- No hay flujo OAuth en el proyecto ni código que declare scopes, renueve tokens, enumere carpetas o descargue vídeos/logos. El CLI espera un token ya disponible como `GOOGLE_OAUTH_TOKEN`. El panel web sigue siendo local y no tiene conexión con Drive.
- La última comprobación `--dry-run` informó `google_drive: false`: `GOOGLE_OAUTH_TOKEN` no está disponible para el proceso en este entorno. Solo se comprobó la presencia de la variable, no se leyó ningún valor ni se intentó una conexión.
- La configuración de Google Cloud indicada para este proyecto usa `drive.file`; eso no concede al programa acceso general a todos los archivos ya existentes. La presencia o validez de `GOOGLE_OAUTH_TOKEN` por sí sola no demuestra que Drive esté conectado ni que el token tenga los permisos esperados.

La opción mínima que conserva `drive.file` es integrar Google Picker: obtener un token de usuario con ese mismo scope, permitir que la persona seleccione explícitamente los MP4/MOV de Entrada y los logos, y seleccionar/autorizar la carpeta de destino antes de crear allí el archivo final. El backend tendría que descargar los archivos seleccionados por ID y conectar esa selección con el pipeline. Seleccionar un clip en Picker es una concesión por archivo; no equivale a enumerar automáticamente todos los hijos de una carpeta. La lectura automatizada de archivos no seleccionados requeriría un scope más amplio y una nueva autorización, que no se ha solicitado ni realizado.

Antes de implementar esa selección faltan la configuración web de Picker (OAuth client ID, clave de API, número del proyecto y orígenes permitidos), la adquisición/entrega segura del token al servidor, las rutas de descarga y el enlace de la selección con los IDs de las carpetas. Después hará falta una prueba con una cuenta real autorizada. No se han inventado credenciales ni se han cambiado permisos.

Referencias oficiales: [scopes de Google Drive](https://developers.google.com/drive/api/guides/about-auth) y [Google Picker para aplicaciones web](https://developers.google.com/workspace/drive/picker/guides/web-picker).

## Pruebas sin llamadas externas

```bash
python -m unittest discover -s tests -v
python -m video_pipeline --dry-run
bash scripts/post-merge.sh
```

Las pruebas usan MP4/MOV sintéticos, incluido un H.265 generado localmente, mocks de las APIs y payloads construidos sin enviarlos. También prueban la API local del panel, la validación y la reproducción por rangos. **No son prueba de una transcripción ni un render real de Shotstack o Drive.**

## Abrir el panel y cargar un vídeo local

1. En Replit, abre **Preview** (el servicio escucha en `0.0.0.0:5000`).
2. Pulsa **Cargar biblioteca** para consultar el servicio local o **Añadir vídeo** para subir `.MP4` o `.MOV`.
3. Espera la validación de FFprobe y revisa los metadatos y el coste estimado. Si no hay tarifas configuradas, se muestra “Desconocido”.
4. Para procesar, marca la autorización de llamadas de pago y confirma el coste desconocido si se solicita. El panel escribe el resultado en `outputs/` y no intenta conectar con Drive.

Para ejecutar el panel manualmente: `python main.py`. La biblioteca local se guarda en `data/incoming/`; los archivos originales subidos no se sobrescriben. El porcentaje de trabajo no se inventa: el panel muestra fases reales mientras el motor no disponga de una medición porcentual.

## Primer render real (todavía no autorizado)

1. Coloca el MP4 o MOV local de prueba en `data/incoming/`.
2. Comprueba su preflight sin llamadas remotas:

   ```bash
   python -m video_pipeline --dry-run --input-dir data/incoming
   ```

3. Completa las tarifas vigentes de tu cuenta en `video_style.json` si quieres que el preflight calcule un importe USD. Sin esas tarifas informa que el importe es desconocido; no inventa precios.
4. Solo cuando autorices el coste y el envío del archivo, ejecuta una sola vez:

   ```bash
   python -m video_pipeline --process data/incoming/prueba.mp4 --authorize-paid-apis --local-only
   ```

El render validado se guarda en `outputs/`. Con `--local-only` no se intenta subirlo a Drive. Sin esa opción, el CLI crea un archivo nuevo en Procesados y necesita un token autorizado previamente. Si el vídeo ya está marcado como completado, el hash impide procesarlo de nuevo.

## Pendiente y limitaciones

- No hay vídeo real de usuario procesado y no se ha hecho ninguna llamada real a OpenAI o Shotstack. El secreto de Shotstack existe, pero no se ha verificado su correspondencia con Stage mediante API.
- La compatibilidad se ha probado con vídeos sintéticos H.264/HEVC, no con un clip real de iPhone. La conversión de HDR/Dolby Vision y su resultado visual aún requieren validación con una muestra real.
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
