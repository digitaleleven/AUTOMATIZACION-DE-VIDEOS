---
name: Origen de Preview interno de Replit
description: Validación segura de Host, Origin y cabeceras proxy en el Preview incrustado.
---

El Preview de este proyecto envió un POST real a `/login` con `Origin: null`, `Host` igual al dominio configurado de la app, `X-Forwarded-Host` ausente y `X-Forwarded-Proto=https`. Este origen opaco solo se admite para `POST /login` en el host exacto configurado y sobre HTTPS; el desafío doble CSRF de cookie y formulario sigue siendo obligatorio. Las demás rutas y otros Origin inválidos siguen rechazándose.

**Why:** la excepción anterior para `https://replit.com` no cubría el valor opaco `null` realmente enviado por el navegador y el login se rechazaba antes de validar el desafío CSRF.

**How to apply:** trata `Origin: null` como excepción solo para el endpoint de login, con Host de aplicación exacto y HTTPS, y conserva la validación obligatoria del desafío CSRF. No lo aceptes globalmente. Al registrar rechazos, conserva solo Host normalizado, estado/esquema/host de Origin, X-Forwarded-Host, X-Forwarded-Proto y el motivo; nunca registres cookies ni cuerpos.
