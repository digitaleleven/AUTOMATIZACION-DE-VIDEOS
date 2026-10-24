---
name: Workspace pnpm anidado
description: Cómo instalar dependencias en un workspace Node aislado dentro del repositorio Python de MATRIX MOTION.
---

En este Replit, pnpm 10.26.1 mostró el paquete único de un workspace pnpm anidado en `pnpm list`, pero `pnpm --filter <nombre> add` y `pnpm --filter .` no lo seleccionaron. Ejecutar `pnpm add --workspace-root` desde esa raíz anidada sí instaló las dependencias y mantuvo el manifiesto y el lockfile fuera de la raíz Python.

**Why:** el instalador global de Replit opera en la raíz del repl y habría mezclado las dependencias Node con el proyecto Python; el filtro estándar no funcionó para el workspace anidado de un solo paquete.

**How to apply:** al crear un paquete pnpm independiente bajo la raíz Python, ejecutar la instalación desde la raíz de ese paquete con `--workspace-root`, mantener su propio lockfile y verificar con `pnpm install --frozen-lockfile`.
