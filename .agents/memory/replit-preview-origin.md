---
name: Origen de Preview interno de Replit
description: Validación segura de Host, Origin y cabeceras proxy en el Preview incrustado.
---

El Preview de este proyecto envió un POST real a `/login` con `Origin: null`, `Host` igual al dominio configurado de la app, `X-Forwarded-Host` ausente y `X-Forwarded-Proto=https`. Este origen opaco solo se admite para `POST /login` en el host exacto configurado y sobre HTTPS; el desafío doble CSRF de cookie y formulario sigue siendo obligatorio. Las demás rutas y otros Origin inválidos siguen rechazándose.

El navegador también solicitaba `/favicon.ico`; al no estar tratado como ruta pública, recibía una redirección 303 a `/login`. Cada GET `/login` emitía un desafío nuevo y sobrescribía la cookie, mientras el campo oculto de la página abierta conservaba el token anterior. Eso causaba la respuesta de desafío caducado antes de comprobar la contraseña. La cookie del desafío es host-only, Secure, HttpOnly, SameSite=Strict y dura 300 segundos; el token de formulario y cookie se comparan en tiempo constante.

En una petición real a la URL externa, la respuesta observada exponía `Secure`, `HttpOnly`, `SameSite=None`, `Path=/` y `Max-Age=300`, sin atributo `Domain`, aunque el manejador local genera `SameSite=Strict`. Comprueba los atributos en la respuesta externa: la capa de proxy puede modificar el encabezado.

**Why:** además de `Origin: null`, la secuencia real de GET `/login` → GET `/favicon.ico` → GET `/login` explicaba el desacuerdo entre el token del formulario y la cookie, aunque ambos estuvieran dentro del plazo.

**How to apply:** trata `Origin: null` como excepción solo para el endpoint de login, con Host de aplicación exacto y HTTPS, y conserva la validación obligatoria del desafío CSRF. Responde a `/favicon.ico` sin redirigir a `/login`. No aceptes Origin opaco globalmente. Al registrar rechazos, conserva solo Host normalizado, estado/esquema/host de Origin, X-Forwarded-Host, X-Forwarded-Proto y el motivo; nunca registres cookies ni cuerpos.
