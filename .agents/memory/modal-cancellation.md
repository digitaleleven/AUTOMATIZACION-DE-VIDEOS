---
name: Cancelación de Modal
description: Interpretación de cancelaciones de funciones Modal y diseño de ejecución reanudable.
---

Los mensajes del runtime `Received a cancellation signal` y `Successfully canceled input` significan que Modal entregó al contenedor un evento de cancelación y que la entrada terminó cancelada. Por sí solos no identifican quién originó la orden ni prueban si una salida ya se había guardado.

**Why:** El SDK inspeccionado registra esos mensajes al procesar un evento de cancelación del control plane. La cancelación de Modal es `BaseException`, así que un `except Exception` del worker no basta para persistir un diagnóstico terminal.

**How to apply:** Para trabajos que deben sobrevivir a una sesión SSH, enviar con `Function.spawn()`, guardar inmediatamente el ID de `FunctionCall` junto con el hash de origen, perfil y job id, y permitir reconexión con `FunctionCall.from_id()`. Ejecutar el supervisor bajo un servicio persistente, no en la terminal SSH; no llamar `cancel()` al desconectarse. Registrar fases en almacenamiento persistente y verificar la caché por hash/perfil antes de considerar un reintento. Tratar el motivo de la cancelación y la existencia de un resultado como desconocidos hasta consultar los logs y el almacenamiento remoto.
