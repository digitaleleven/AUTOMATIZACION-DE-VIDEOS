---
name: Envíos de render inciertos
description: Tratamiento seguro de registros históricos y futuros envíos remotos sin estado confirmado.
---

La integración de Shotstack está retirada; no hay actualmente una ruta de render activa con ese proveedor. Se conservan los campos y estados históricos de la base de datos para no alterar el historial de trabajos existentes. No se debe interpretar un `render_id` o `source_url` legado como prueba de una integración activa.

Para cualquier futuro render remoto, un POST cuyo resultado no incluya un identificador recuperable deja el estado remoto incierto. Mantener `submission_unknown` y bloquear reintentos hasta reconciliarlo con el proveedor.

**Why:** el proveedor puede haber encolado un render aunque el cliente no haya guardado el ID; un reenvío a ciegas puede duplicar trabajo y coste.

**How to apply:** persistir el estado incierto antes de la llamada remota y cambiarlo solo al guardar un ID válido. Sin un ID verificable, bloquear reintentos ciegos y no afirmar que el render falló hasta reconciliarlo.
