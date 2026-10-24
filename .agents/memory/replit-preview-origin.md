---
name: Origen de Preview interno de Replit
description: Validación segura de Host, Origin y cabeceras proxy en el Preview incrustado.
---

El Preview interno puede ser cross-origin respecto al editor. La petición de la aplicación puede llevar `Origin: https://replit.com`, mientras `Host` es el dominio temporal de la app o un host local del proxy. Para aceptar ese origen del editor, exige además que `X-Forwarded-Host` coincida exactamente con `REPLIT_DEV_DOMAIN` o un elemento de `REPLIT_DOMAINS` y que `X-Forwarded-Proto` sea `https`. Mantén una allowlist de hosts exacta; nunca permitas todos los subdominios ni confíes en un `X-Forwarded-Host` arbitrario.

**Why:** el Preview incrustado y la URL externa pueden presentar pares de Host/Origin distintos; comparar solo el Host bruto con el Origin puede bloquear el formulario interno.

**How to apply:** prueba GET y POST del login con las combinaciones internas de proxy y la URL externa. Al registrar rechazos, limita los datos a Host, host de Origin, X-Forwarded-Host, X-Forwarded-Proto y un motivo; nunca registres cookies ni cuerpos.
