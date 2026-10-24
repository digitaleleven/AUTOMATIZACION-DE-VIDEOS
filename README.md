# MATRIX MOTION — VIDEO AUTOMATION STUDIO

«De vídeo original a contenido listo para publicar». El proyecto acepta vídeos locales `.MP4` y `.MOV` y prepara una edición 9:16 con OpenAI y Shotstack. La interfaz web guarda el resultado únicamente en `outputs/`; el comando CLI conserva, por defecto, la opción de crear una copia nueva en Procesados de Google Drive. Ningún flujo modifica el original ni amplía permisos OAuth.

## Implementado

- Panel protegido con autenticación de una única propietaria, sesiones opacas almacenadas en SQLite, expiración por tiempo absoluto e inactividad, protección CSRF, cierre de sesión y límite de intentos de acceso.
- Panel web en español, adaptable y accesible desde Replit Preview, con biblioteca local, reproducción original/editado, estados y errores reales, estimaciones no inventadas y controles de estilo.
- Validación de MP4 y MOV, duración, tamaño, audio, H.264/HEVC, dimensiones, rotación y FPS mediante `ffprobe`.
- Compatibilidad iPhone: conserva sin conversión los MP4/MOV H.264 con AAC y orientación normal; convierte HEVC a H.264 de alta calidad cuando hace falta para edición, y convierte audio no AAC a AAC. Las conversiones por códec, audio u orientación son temporales; la copia original queda intacta.
- Extracción temporal de audio con FFmpeg en bloques de hasta 15 minutos; cada bloque se codifica como MP3 mono a 16 kHz y 64 kb/s, con desplazamiento temporal aplicado a las palabras para mantener los subtítulos sincronizados.
- Transcripción `whisper-1` en `verbose_json`, solicitando timestamps por palabra y segmento. La salida se normaliza; si faltan tiempos por palabra, se estiman dentro del segmento.
- Captions SRT de 2–4 palabras sincronizados con el audio.
- Edición Shotstack con `rich-caption`, animación/highlight por palabra, trazo legible y subtítulos en zona inferior segura.
- Estilo configurable en `video_style.json`: fuente, colores, animación, posición, emojis asociados manualmente y logo local opcional.
- Salida MP4 1080 × 1920 a 30 fps. Las fuentes con otra relación de aspecto usan `contain` para no cortar caras; solo se usa crop si el formato ya es prácticamente 9:16.
- Ingest de MP4, SRT y, opcionalmente, logo a Shotstack. Resultado descargado como archivo nuevo; el original no se modifica ni sobrescribe.
- Carga del render validado a Drive en fragmentos de 8 MB como archivo nuevo, con consulta del avance y reanudación tras cortes. El estado local de la sesión se conserva con permisos restrictivos y no incluye el token OAuth.
- SQLite local (`data/processing.sqlite3`) para detectar vídeos ya completados, reanudar un render existente y permitir como máximo un reintento manual. Cada ejecución conserva una clave estable que combina el hash del original y la configuración exacta.
- Antes de solicitar un render, el estado persistente pasa a “envío incierto”; si el proceso se interrumpe antes de guardar el ID remoto, el sistema bloquea el reenvío ciego. Los reintentos consultan el ID guardado, y reutilizan el MP4 local ya validado si existe.
- Las salidas de Drive llevan una clave privada de ejecución en `appProperties`; antes de crear otro archivo, se busca el resultado anterior en `02_PROCESADOS`. Las cargas interrumpidas conservan su sesión reanudable y los originales no se modifican.
- Límites por defecto: un vídeo por ejecución, sin límite fijo de tamaño de archivo, 30 minutos de vídeo, dos bloques de transcripción y un nuevo render por intento. La duración y el máximo de llamadas se pueden ajustar en `video_style.json`.
- `requirements.txt` fija las versiones usadas. El panel y su API usan la biblioteca estándar de Python y no añaden dependencias web.

La animación activa destaca la palabra que se está pronunciando; el proyecto no realiza análisis semántico para decidir qué palabras son “importantes”. Los emojis son opcionales y solo se añaden si se configuran en `emoji_by_keyword`.

## Shotstack: Stage exclusivamente

La configuración y el cliente rechazan cualquier entorno que no sea `stage`; no hay una ruta de producción habilitada. Replit Secrets informa que existen `OPENAI_API_KEY` y `SHOTSTACK_API_KEY`, pero no se han leído ni mostrado sus valores. Tampoco se ha comprobado mediante una llamada que la clave de Shotstack corresponda a Stage, porque no se han realizado solicitudes externas.

La transcripción y el render de Shotstack siguen detrás de una autorización explícita. El panel solo sube a Drive resultados de vídeos importados desde Drive, después de validar el render y con una autorización OAuth guardada; los vídeos locales se mantienen locales. El CLI conserva su ruta anterior, que requiere `GOOGLE_OAUTH_TOKEN` y no se ejecuta desde el panel. Los reintentos automáticos del SDK de OpenAI están desactivados.

Antes de procesar, la aplicación comprueba la presencia de las claves de OpenAI y Shotstack y bloquea Shotstack a Stage. Para una entrada de Drive necesita además una autorización Drive válida. Si Drive rechaza el destino, el render local se conserva y puede reintentarse solo la subida, sin volver a renderizar.

## Google Drive: selección explícita, scope sin ampliar

Google Picker solicita únicamente `https://www.googleapis.com/auth/drive.file` para Drive, además de `openid` y `email` para verificar la cuenta OAuth del servicio. No se solicita `drive.readonly` ni otro scope de acceso general. La cuenta propietaria elige Entrada, Logos y Procesados en Picker, y después autoriza individualmente los MP4 y logos que la aplicación puede consultar y descargar. También se pueden seleccionar Google Docs de Entrada para instrucciones; se validan y leen solo los elegidos explícitamente. Seleccionar una carpeta no concede acceso recursivo: los archivos nuevos deben seleccionarse explícitamente.

El flujo de OAuth del panel está protegido con `state`, PKCE S256, cookie `HttpOnly; SameSite=Lax` y refresh token cifrado en SQLite. Sus rutas requieren sesión de propietaria; el callback exige estado de un solo uso y valida el correo permitido. El escaneo manual y periódico se limita a los archivos ya seleccionados en Picker, no inicia renders y conserva intactos los originales. El panel también ofrece una prueba Picker que crea un `.txt` nuevo en Procesados y una prueba avanzada que crea y elimina únicamente su MP4 temporal. Ninguna prueba real se ha ejecutado.

Para Google Picker, configura en Replit Secrets `GOOGLE_DRIVE_API_KEY` y `GOOGLE_DRIVE_APP_ID`. El ID de cliente se lee de `GOOGLE_OAUTH_CLIENT_ID` (o `GOOGLE_DRIVE_CLIENT_ID`). Restringe la API key al origen del panel y a las APIs de Google necesarias. El flujo OAuth de servidor usa `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`, `GOOGLE_OAUTH_ALLOWED_EMAIL` y el interruptor `GOOGLE_DRIVE_OAUTH_ENABLED`; no lo habilites hasta autorizar ese consentimiento. No pegues credenciales en el chat.

Los tokens de Picker se mantienen en la pestaña y se envían solo para la operación solicitada. El OAuth de servidor, cuando se habilita por separado, cifra el refresh token en SQLite. Se guardan localmente los IDs seleccionados y el manifiesto de descargas, nunca los tokens de Picker. Las escrituras crean archivos nuevos; no mueven, borran, renombran ni sobrescriben archivos originales. No se han cambiado permisos ni comparticiones de carpetas.

La sincronización descarga los MP4 seleccionados a `data/incoming/` y los logos seleccionados a `data/logos/`. La selección Picker puede crear un archivo nuevo de prueba en Procesados; la prueba no implica que el acceso de escritura de una cuenta real haya sido validado.

## Estado de verificación

- Probado localmente con respuestas HTTP simuladas: OAuth state/PKCE, verificación de cuenta, cifrado y renovación de tokens, selección de carpetas/archivos, descargas sin sobrescritura, importación idempotente y creación simulada de archivos nuevos.
- No probado con una cuenta Google real: OAuth, permisos efectivos, descarga real ni creación real en Procesados.
- No se han realizado llamadas reales a Google, OpenAI o Shotstack ni se han cambiado permisos.

`video_pipeline/drive.py` sube archivos nuevos en fragmentos acotados y recupera el punto confirmado por Drive tras un corte. El estado de sesión guardado permite que un reintento posterior retome la transferencia sin leer el render completo en memoria. El CLI puede seguir usando `GOOGLE_OAUTH_TOKEN` en su ruta anterior.

Referencias oficiales: [flujo OAuth web](https://developers.google.com/identity/protocols/oauth2/web-server), [scopes de Drive](https://developers.google.com/workspace/drive/api/guides/api-specific-auth), [listar archivos](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list) y [descargas/exportación](https://developers.google.com/workspace/drive/api/guides/manage-downloads).

## Pruebas sin llamadas externas

```bash
python -m unittest discover -s tests -v
python -m video_pipeline --dry-run
bash scripts/post-merge.sh
```

Las pruebas usan MP4/MOV sintéticos, incluido un H.265 generado localmente, mocks de las APIs y payloads construidos sin enviarlos. También prueban la API local del panel, la validación y la reproducción por rangos. **No son prueba de una transcripción ni un render real de Shotstack o Drive.**

## Abrir el panel y cargar un vídeo local

1. En Replit Secrets, configura `PANEL_OWNER_PASSWORD` con una frase de paso única de al menos 16 caracteres. No la pongas en el código ni la compartas por chat. `SESSION_SECRET` debe existir y tener al menos 32 caracteres; no uses `PANEL_COOKIE_SECURE=false` en Preview o producción.
2. Abre **Preview** (el servicio escucha en `0.0.0.0:5000`) y entra con la contraseña configurada. Si el navegador bloquea cookies de una vista incrustada, abre Preview en una pestaña nueva.
3. Pulsa **Actualizar datos** para consultar la biblioteca local o **Añadir vídeo** para subir `.MP4` o `.MOV`.
4. Espera la validación de FFprobe y revisa los metadatos y el coste estimado. Si no hay tarifas configuradas, se muestra “Desconocido”.
5. Para procesar, marca la autorización de llamadas de pago y confirma el coste desconocido si se solicita. El panel escribe el resultado en `outputs/` y no intenta conectar con Drive.

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
