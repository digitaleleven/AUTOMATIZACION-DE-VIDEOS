---
name: Carpetas compartidas de Drive
description: Interpretación de la jerarquía de carpetas compartidas al consultar Drive desde la cuenta secundaria.
---

La propietaria confirmó que `AUTOMATIZACION_VIDEOS/01_ENTRADA` aparece anidada en su «Mi unidad», pero directamente en «Compartido conmigo» en la cuenta secundaria, con el mismo ID. Al consultar la carpeta compartida desde la cuenta secundaria, Drive puede devolver `parents` ausente; esto no invalida por sí solo el ID ni la ruta confirmados por la propietaria.

**Why:** la vista de una carpeta compartida difiere entre la cuenta propietaria y la secundaria, así que exigir la misma cadena de `parents` puede bloquear una carpeta válida.

**How to apply:** cuando la propietaria confirme el ID exacto, consultar solo ese ID y verificar que los archivos devueltos lo incluyen en `parents`. No ampliar la búsqueda al Drive completo ni inferir otro ID por la ausencia del padre.
