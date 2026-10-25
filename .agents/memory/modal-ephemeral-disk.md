---
name: Disco efímero de Modal
description: Restricciones de tamaño y facturación del almacenamiento efímero en funciones Modal.
---

El servicio rechazó `ephemeral_disk=40*1024` (40 GiB) porque la petición explícita mínima es 512 GiB. La cuota predeterminada por contenedor ya es 512 GiB; no especifiques una cuota inferior. Modal indica que las cuotas de disco solicitadas elevan la memoria facturable en proporción 20:1.

**Why:** una petición explícita de 40 GiB falló durante el despliegue; quitar el parámetro permitió usar la cuota predeterminada y desplegar sin solicitar memoria facturable adicional por una cuota explícita.

**How to apply:** para renderizar proxies y fotogramas temporales, usa la cuota predeterminada. Antes de pedir más disco, consulta la documentación vigente y calcula el impacto de memoria/coste.
