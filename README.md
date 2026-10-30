# MATRIX MOTION — VIDEO AUTOMATION STUDIO

«De vídeo original a contenido listo para publicar». El proyecto acepta vídeos locales `.MP4` y `.MOV`, prepara una edición vertical y ejecuta los renders desde el panel autenticado mediante el worker Modal/Remotion. FFmpeg normaliza los medios cuando hace falta. Ningún flujo modifica el original ni amplía permisos OAuth.

## Implementado

- Panel protegido con autenticación de una única propietaria, sesiones opacas almacenadas en SQLite, expiración por tiempo absoluto e inactividad, protección CSRF, cierre de sesión y límite de intentos de acceso.
- Panel web en español, adaptable y accesible desde Replit Preview, con biblioteca local, reproducción original/editado, estados y errores reales, estimaciones no inventadas y controles de estilo.
- Validación de MP4 y MOV, duración, tamaño, audio, H.264/HEVC, dimensiones, rotación y FPS mediante `ffprobe`.
- Compatibilidad iPhone: conserva sin conversión los MP4/MOV H.264 con AAC y orientación normal; convierte HEVC a H.264 de alta calidad cuando hace falta para edición, y convierte audio no AAC a AAC. Las conversiones por códec, audio u orientación son temporales; la copia original queda intacta.
- Extracción temporal de audio con FFmpeg en bloques de hasta 15 minutos; cada bloque se codifica como MP3 mono a 16 kHz y 64 kb/s, con desplazamiento temporal aplicado a las palabras para mantener los subtítulos sincronizados.
- Transcripción `whisper-1` en `verbose_json`, solicitando timestamps por palabra y segmento. La salida se normaliza; si faltan tiempos por palabra, se estiman dentro del segmento.
- Captions SRT de 2–4 palabras sincronizados con el audio.
- Render mediante el worker Modal/Remotion, con subtítulos dinámicos y validación FFprobe del resultado.
- Estilo configurable en `video_style.json`: fuente, colores, animación, posición, emojis asociados manualmente y logo local opcional.
- Perfiles verticales MP4 de 720 × 1280, 1080 × 1920 y 2160 × 3840. El perfil 4K se habilita solo para una fuente maestra de al menos 2160 × 3840; se admiten 25, 30, 50 o 60 FPS. La validación 4K está preparada en el código, pero aún no se ha comprobado con un render remoto.
- El resultado validado se guarda como archivo nuevo; el original no se modifica ni sobrescribe.
- Carga del render validado a Drive en fragmentos de 8 MB como archivo nuevo, con consulta del avance y reanudación tras cortes. El estado local de la sesión se conserva con permisos restrictivos y no incluye el token OAuth.
- SQLite local conserva el historial de trabajos y evita procesamientos duplicados. Los reintentos de render están bloqueados; para volver a procesar se requiere una propuesta nueva y autorización humana.
- Las salidas de Drive llevan una clave privada de ejecución en `appProperties`; antes de crear otro archivo, se busca el resultado anterior en `02_PROCESADOS`. Las cargas interrumpidas conservan su sesión reanudable y los originales no se modifican.
- Límites por defecto: un vídeo por ejecución, sin límite fijo de tamaño de archivo, 30 minutos de vídeo, dos bloques de transcripción y un nuevo render por intento. La duración y el máximo de llamadas se pueden ajustar en `video_style.json`.
- `requirements.txt` fija las versiones usadas. El panel y su API usan la biblioteca estándar de Python y no añaden dependencias web.

La animación activa destaca la palabra que se está pronunciando; el proyecto no realiza análisis semántico para decidir qué palabras son “importantes”. Los emojis son opcionales y solo se añaden si se configuran en `emoji_by_keyword`.

## Motor de render vigente

El panel usa el worker de Modal con Remotion. La ruta antigua de render por CLI se ha retirado y responde con un error explícito; el CLI solo mantiene la inspección en modo `--dry-run`. No se ha desplegado ni invocado el worker durante esta retirada.

Las llamadas de análisis, transcripción y cómputo requieren autorización humana y configuración disponible. Los resultados de importaciones de Drive pueden subirse como archivos nuevos después de validar el render; el original no se sobrescribe. Los reintentos de render están desactivados.

Antes de procesar, la aplicación valida las configuraciones necesarias para el flujo Modal/R2 y, si el origen procede de Drive, exige una sesión Drive válida. Si falla la subida a Drive, se puede reintentar únicamente esa subida sin repetir el render.

## Google Drive: selección explícita, scope sin ampliar

Google Picker solicita únicamente `https://www.googleapis.com/auth/drive.file` para Drive, además de `openid` y `email` para verificar la cuenta OAuth del servicio. No se solicita `drive.readonly` ni otro scope de acceso general. La cuenta propietaria elige Entrada, Logos y Procesados en Picker, y después autoriza individualmente los MP4 y logos que la aplicación puede consultar y descargar. También se pueden seleccionar Google Docs de Entrada para instrucciones; se validan y leen solo los elegidos explícitamente. Seleccionar una carpeta no concede acceso recursivo: los archivos nuevos deben seleccionarse explícitamente.

El flujo de OAuth del panel está protegido con `state`, PKCE S256, cookie `HttpOnly; SameSite=Lax` y refresh token cifrado en SQLite. Sus rutas requieren sesión de propietaria; el callback exige estado de un solo uso y valida el correo permitido. El escaneo manual y periódico se limita a los archivos ya seleccionados en Picker, no inicia renders y conserva intactos los originales. El panel también ofrece una prueba Picker que crea un `.txt` nuevo en Procesados y una prueba avanzada que crea y elimina únicamente su MP4 temporal. Ninguna prueba real se ha ejecutado.

Para Google Picker, configura en Replit Secrets `GOOGLE_DRIVE_API_KEY` y `GOOGLE_DRIVE_APP_ID`. El ID de cliente se lee de `GOOGLE_OAUTH_CLIENT_ID` (o `GOOGLE_DRIVE_CLIENT_ID`). Restringe la API key al origen del panel y a las APIs de Google necesarias. El flujo OAuth de servidor usa `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`, `GOOGLE_OAUTH_ALLOWED_EMAIL` y el interruptor `GOOGLE_DRIVE_OAUTH_ENABLED`; no lo habilites hasta autorizar ese consentimiento. No pegues credenciales en el chat.

Los tokens de Picker se mantienen en la pestaña y se envían solo para la operación solicitada. El OAuth de servidor, cuando se habilita por separado, cifra el refresh token en SQLite. Se guardan localmente los IDs seleccionados y el manifiesto de descargas, nunca los tokens de Picker. Las escrituras crean archivos nuevos; no mueven, borran, renombran ni sobrescriben archivos originales. No se han cambiado permisos ni comparticiones de carpetas.

La sincronización descarga los MP4 seleccionados a `data/incoming/` y los logos seleccionados a `data/logos/`. La selección Picker puede crear un archivo nuevo de prueba en Procesados; la prueba no implica que el acceso de escritura de una cuenta real haya sido validado.

## Estado de verificación

- Probado localmente con respuestas HTTP simuladas: OAuth state/PKCE, verificación de cuenta, cifrado y renovación de tokens, selección de carpetas/archivos, descargas sin sobrescritura, importación idempotente y creación simulada de archivos nuevos.
- No probado con una cuenta Google real: OAuth, permisos efectivos, descarga real ni creación real en Procesados.
- En esta retirada no se han realizado llamadas externas, renders ni cambios de permisos.

`video_pipeline/drive.py` sube archivos nuevos en fragmentos acotados y recupera el punto confirmado por Drive tras un corte. El estado de sesión guardado permite que un reintento posterior retome la transferencia sin leer el render completo en memoria. El CLI puede seguir usando `GOOGLE_OAUTH_TOKEN` en su ruta anterior.

Referencias oficiales: [flujo OAuth web](https://developers.google.com/identity/protocols/oauth2/web-server), [scopes de Drive](https://developers.google.com/workspace/drive/api/guides/api-specific-auth), [listar archivos](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list) y [descargas/exportación](https://developers.google.com/workspace/drive/api/guides/manage-downloads).

## Pruebas sin llamadas externas

```bash
python -m unittest discover -s tests -v
python -m video_pipeline --dry-run
bash scripts/post-merge.sh
```

Las pruebas usan MP4/MOV sintéticos, incluido un H.265 generado localmente, mocks de las APIs y la API local del panel. También prueban la validación y la reproducción por rangos. **No verifican un render remoto real ni el acceso real a Drive.**

## Abrir el panel y cargar un vídeo local

1. En Replit Secrets, configura `PANEL_OWNER_PASSWORD` con una frase de paso única de al menos 16 caracteres. No la pongas en el código ni la compartas por chat. `SESSION_SECRET` debe existir y tener al menos 32 caracteres; no uses `PANEL_COOKIE_SECURE=false` en Preview o producción.
2. Abre **Preview** (el servicio escucha en `0.0.0.0:5000`) y entra con la contraseña configurada. Si el navegador bloquea cookies de una vista incrustada, abre Preview en una pestaña nueva.
3. Pulsa **Actualizar datos** para consultar la biblioteca local o **Añadir vídeo** para subir `.MP4` o `.MOV`.
4. Espera la validación de FFprobe y revisa los metadatos y el coste estimado. Si no hay tarifas configuradas, se muestra “Desconocido”.
5. Para procesar, marca la autorización de llamadas de pago y confirma el coste desconocido si se solicita. El panel escribe el resultado en `outputs/` y no intenta conectar con Drive.

Para ejecutar el panel manualmente: `python main.py`. La biblioteca local se guarda en `data/incoming/`; los archivos originales subidos no se sobrescriben. El porcentaje de trabajo no se inventa: el panel muestra fases reales mientras el motor no disponga de una medición porcentual.

## Primer render desde el panel

1. Coloca el MP4 o MOV local de prueba en `data/incoming/`.
2. Comprueba su preflight sin llamadas remotas:

   ```bash
   python -m video_pipeline --dry-run --input-dir data/incoming
   ```

3. Si el coste sigue apareciendo como desconocido, no inicies el procesamiento hasta revisar las tarifas de Modal y R2 fuera de la aplicación.
4. Para renderizar, vuelve al panel autenticado, revisa la propuesta y autoriza cada operación desde la interfaz. El comando CLI de render está retirado; `--dry-run` solo inspecciona archivos y nunca inicia trabajos.

## Pendiente y limitaciones

- La retirada de Shotstack se ha verificado localmente; el funcionamiento remoto de Modal/R2 no se ha verificado en esta tarea.
- Se validó localmente un segmento de 1 s de una captura real de iPhone 17 Pro: HEVC Main 10, HLG BT.2020 limitado, Dolby Vision 8.4 (RPU presente), 2160×3840 vertical y rotación 0. La copia temporal se convirtió a H.264 SDR BT.709, 1080×1920 y 30 FPS; los metadatos de color de salida se verificaron y el archivo de origen no se modificó. No se llamó a OpenAI, Shotstack, Drive ni Modal.
- Compatibilidad de color/orientación: el flujo local de Modal acepta SDR BT.709 y HLG BT.2020 limitado. HLG se convierte a SDR BT.709; no conserva el HDR. Dolby Vision 8.4 compatible con HLG se procesa usando su capa base HLG: el RPU dinámico no se conserva y el resultado no es un máster Dolby Vision. Otros perfiles Dolby Vision se rechazan con un error claro. La conversión H.264 de compatibilidad antigua también rechaza Dolby Vision para no descartar silenciosamente sus metadatos dinámicos. Perfiles de color desconocidos o HLG de rango completo no se convierten; requieren una ruta de color explícita.
- FFmpeg aplica la orientación de presentación al recodificar en el flujo de Modal; la prueba automatizada comprueba que la salida sigue vertical y BT.709 y que el hash del original no cambia. El clip real validado ya era vertical con rotación 0; las rotaciones 90°/270° se detectan en metadatos y se reflejan en las dimensiones mostradas. El resultado HLG→SDR todavía requiere revisión visual humana de color antes de aprobarlo como acabado final.
- Falta una revisión visual y auditiva de un resultado real del flujo Modal.
- La composición centra y conserva fuentes no 9:16 con letterboxing; no hace seguimiento facial ni reencuadre semántico.
- El logo se incorpora si se especifica un archivo local en `video_style.json`. El acceso al logo de Drive no está implementado.
- El coste final de Modal y R2 no se estima en este proyecto; revisa las tarifas configuradas en esas cuentas antes de autorizar un render.
- Google Drive no lista ni descarga entradas y no amplía OAuth. Se necesita un token OAuth vigente con acceso de creación en Procesados; la autorización/selección de carpetas y archivos de entrada sigue pendiente por separado.

## Referencias consultadas

- OpenAI Transcriptions: https://platform.openai.com/docs/api-reference/audio/createTranscription
