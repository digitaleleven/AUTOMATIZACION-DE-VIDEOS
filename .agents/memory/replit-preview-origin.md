---
name: Origen de Preview interno de Replit
description: Validación segura de Host, Origin y cabeceras proxy en el Preview incrustado.
---

El Preview interno puede presentar cabeceras distintas a la URL externa. En este proyecto, el registro real de un POST rechazado a `/login` mostró `Host` igual al dominio configurado de la app, `Origin` no analizable (registrado solo como `invalid`), `X-Forwarded-Host` ausente y `X-Forwarded-Proto=https`. El registro no conserva si el Origin literal era `null` u otro valor malformado.

**Why:** asumir que el Preview siempre envía `Origin: https://replit.com` junto a un `X-Forwarded-Host` válido puede llevar a excepciones de seguridad que no corresponden a la petición real.

**How to apply:** diagnostica la petición real registrada antes de cambiar la allowlist. No aceptes un Origin no analizable ni relajes CSRF por inferencia. Al registrar rechazos, conserva solo Host normalizado, esquema/host de Origin cuando parseable, X-Forwarded-Host, X-Forwarded-Proto y el motivo; nunca registres cookies ni cuerpos.
