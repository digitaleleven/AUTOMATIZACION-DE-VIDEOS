---
name: Modal como motor único de render
description: Alcance de backend de render y normalización previa en MATRIX MOTION.
---

MATRIX MOTION usa Modal como único motor de render. No exponer Shotstack ni habilitar reintentos de renders antiguos. Mantener intacto el original: convertir entradas compatibles a una copia de trabajo validada antes del envío, y detenerse si el color o las pistas no se pueden interpretar con seguridad.

**Why:** la responsable eligió Modal como motor único y autorizó preparar salidas verticales de hasta 2160×3840 y 60 FPS para másteres compatibles; los renders siguen sujetos a aprobación humana.

**How to apply:** seleccionar 2160×3840 solo si el máster tiene al menos esas dimensiones; aceptar únicamente 25, 30, 50 o 60 FPS. El cambio local de código no demuestra que el worker esté desplegado ni que un render remoto sea viable. No desplegar ni renderizar sin autorización expresa para esa operación.

El nombre devuelto por `modal profile current` no demuestra que exista autenticación válida: puede devolver `default` sin ningún token.

**Why:** el CLI devolvió ese perfil, pero el despliegue falló con `Token missing. Could not authenticate client.`

**How to apply:** comprobar la presencia de credenciales sin mostrar valores y la respuesta real del despliegue antes de afirmar que la cuenta está conectada. Si falta autenticación, detener el pipeline antes de generar análisis de pago.

Los errores de subprocesos capturados deben conservarse antes de limpiar el contenedor; no confiar en recuperarlos después desde los logs generales de Modal.

**Why:** los logs generales conservaron solo `CalledProcessError`, sin el `stderr` capturado; consultar posteriormente el resultado de la llamada síncrona consumida devolvió `NotFoundError`.

**How to apply:** persistir un diagnóstico acotado y redactado antes de propagar el error. Si no se conserva el detalle original, distinguir claramente la traza disponible de una reproducción local.

La validación del contrato Python → Remotion debe cubrir el JSON completo, no solo los argumentos de la CLI o una lista de palabras aislada.

**Why:** una invocación real superó el parser, pero fue rechazada porque faltaba el texto canónico exigido por la comprobación de cobertura. También había marcadores de duración cero que deben conservarse con ventanas compartidas, no descartarse.

**How to apply:** mantener una prueba local entre ambos lenguajes que use el mismo constructor de transcripción del pipeline y el validador real de Remotion; conservar palabras y tiempos originales, sin atribuir precisión individual a ventanas compartidas.

### Motion blur nativo en cortes de escena

La responsable autorizó cambiar el compositor final para que Remotion procese también el vídeo y pueda aplicar motion blur en los límites entre escenas. La conversión de color y el montaje de audio deben conservar sus etapas validadas; no introducir solapamientos que cambien la duración sin autorización aparte.

**Why:** el compositor anterior mezclaba el vídeo en FFmpeg después de renderizar una capa gráfica transparente en Remotion, así que Remotion no podía muestrear fotogramas de la escena.

**How to apply:** procesar el vídeo base dentro de la composición Remotion con una copia de trabajo BT.709 y mantener la mezcla final del audio original. La autorización de arquitectura no autoriza por sí sola desplegar el worker ni ejecutar un render remoto.

### Coordinación de la edición premium

Las sugerencias de corte de Gemini son candidatas: solo se aplican donde el detector local confirma silencio y el intervalo no elimina palabras transcritas. Audio, subtítulos, efectos y ventanas de zoom deben mapearse sobre los mismos segmentos conservados. La conversión FFmpeg resuelve color/escala; Remotion aplica el zoom una sola vez.

**Why:** los cortes basados solo en texto pueden truncar voz, y aplicar zoom tanto en FFmpeg como en Remotion duplica el movimiento.

**How to apply:** al ampliar el plan Python→Modal→Remotion, mantener la corroboración local de pausas, el límite de cortes y el mapeo de tiempos fuente a salida. No afirmar que el montaje de un vídeo real está validado sin render y revisión.
