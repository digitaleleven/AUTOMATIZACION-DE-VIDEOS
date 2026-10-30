---
name: Cancelación de Modal
description: Interpretación de cancelaciones de funciones Modal y diseño de ejecución reanudable.
---

Los mensajes del runtime `Received a cancellation signal` y `Successfully canceled input` significan que Modal entregó al contenedor un evento de cancelación y que la entrada terminó cancelada. Por sí solos no identifican quién originó la orden ni prueban si una salida ya se había guardado.

Con Modal Python SDK 1.6.2, la consulta de solo lectura `FunctionCall.from_id(id).get(timeout=0)` puede reconectar una llamada existente: si sigue activa, el timeout no acredita cancelación; una cancelación confirmada se presenta como `modal.exception.InputCancellation`. `AuthError` y fallos de consulta deben quedar como estado desconocido/fallido, nunca convertirse en cancelación. Persiste el ID consultado y un evento con el tipo exacto de excepción y hash de la observación, sin reescribir el ledger previo.

**Why:** El SDK inspeccionado registra esos mensajes al procesar un evento de cancelación del control plane, y su `InputCancellation` es `BaseException`; `FunctionCall.from_id().get(timeout=0)` proporciona una consulta sin volver a enviar el trabajo. Un `except Exception` no basta y un `AuthError` no demuestra estado terminal.

**How to apply:** Para trabajos que deben sobrevivir a una sesión SSH, enviar con `Function.spawn()`, guardar inmediatamente el ID de `FunctionCall` junto con el hash de origen, perfil y job id, y permitir reconexión con `FunctionCall.from_id()`. Ejecutar el supervisor bajo un servicio persistente, no en la terminal SSH; no llamar `cancel()` al desconectarse. Registrar fases en almacenamiento persistente y verificar la caché por hash/perfil antes de considerar un reintento. Tratar el motivo de la cancelación y la existencia de un resultado como desconocidos hasta consultar los logs y el almacenamiento remoto.
