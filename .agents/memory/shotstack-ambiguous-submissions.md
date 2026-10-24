---
name: Envíos inciertos a Shotstack
description: Respuestas de envío de render sin un identificador remoto recuperable.
---

La API oficial de Shotstack devuelve el identificador de envío bajo `response.id`; la consulta de estado requiere ese ID y la guía de inicio indica que no hay una lista de IDs de render recuperable por API. Si el POST devuelve JSON sin un ID reconocido, el resultado remoto es incierto: guardar el estado como `submission_unknown` y bloquear reintentos hasta reconciliarlo manualmente en Stage.

**Why:** Shotstack puede haber encolado un render aunque el cliente no reconozca o no guarde el ID. Reenviar a ciegas puede duplicar un render y su coste.

**How to apply:** aceptar el formato oficial `response.id` y los formatos legados necesarios; si falta el ID, conservar solo el código HTTP y rutas de campos saneadas, nunca el cuerpo completo, y no reintentar ni afirmar que el render fracasó hasta verificarlo.
