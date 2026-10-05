"""Plantillas de los docs: la estructura de cada tipo (ver la convención en ~/projects/CLAUDE.md).

Los comentarios HTML son las indicaciones para quien completa el doc; `search`, `weekly` y
`promote` los ignoran.
"""

DIAG = """
# {titulo}

## TL;DR

<!-- 2-4 líneas: problema, causa raíz, solución. La 1ª dice QUÉ se hizo (sale en weekly). -->

---

## Contexto / topología

<!-- Máquinas, rutas, repos, servicios involucrados. Incluir las trampas (cosas confusas). -->

---

## Síntomas observados

<!-- Qué se veía, con evidencia (salidas de comandos, logs). -->

```
# pegar salida relevante acá
```

---

## Discoveries (en orden)

<!-- Recorrido del diagnóstico paso a paso. Resaltar la causa raíz. -->

1.

**Causa raíz:**

---

## Correcciones aplicadas

<!-- Qué se cambió y por qué. -->

---

## Pendientes / a monitorear

- [ ]

---

## Comandos útiles de referencia

```bash
# para retomar o re-diagnosticar
```
"""

PLAN = """
# {titulo}

## TL;DR

<!-- Objetivo en 2-4 líneas: qué se construye y por qué. -->

---

## Contexto / problema

<!-- Qué disparó la necesidad, qué hay hoy que no alcanza. -->

---

## Alcance

<!-- Qué incluye y qué explícitamente no (para no scope-creepear). -->

---

## Diseño / propuesta

<!-- La solución, con fases si el trabajo es incremental. -->

---

## Decisiones tomadas

<!-- Alternativas consideradas y por qué se descartaron, si hubo. -->

---

## Pendientes / checklist

- [ ]
"""

REF = """
# {titulo}

## TL;DR

<!-- Qué es esta referencia y cuándo consultarla, en 1-2 líneas. -->
"""

REF_CONTENIDO = """
---

## Contenido

<!-- Fichas, mapeos, tablas, diagramas, reviews: lo que se consulta, no lo que se cierra.
     Al tocarlo, `actualizado:` se pone solo — una referencia vieja miente sin avisar. -->
"""

HANDOFF = """
# {titulo}

## TL;DR

<!-- 1-2 líneas: qué tiene que hacer la próxima conversación. -->

---

## Leer primero

<!-- Qué plan leer y qué sección. Apuntar, no copiar: si el plan se corrige después, la nota no
     queda vieja. Con `--plan <doc>`, `take` avisa si el plan cambió después de escrita la nota. -->

---

## Orden de trabajo

1.

---

## Preguntar antes de arrancar

-

---

## Forma de trabajar

<!-- Opcional: lo que no está ni en el plan ni en CLAUDE.md. Nada destructivo: la nota se archiva
     sola al tomarla, no pide que la borren. -->
"""
