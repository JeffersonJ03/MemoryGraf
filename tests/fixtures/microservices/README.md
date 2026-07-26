# Fixture: sistema de microservicios multi-lenguaje

Sistema sintético para **testear MemoryGraf contra cada lenguaje soportado**. Cada
servicio es un "repo" independiente (proyecto) escrito en un lenguaje distinto y se
comunica con los demás **por HTTP** (literales de endpoint), no por imports.

Verifica dos cosas:
1. **Extracción por lenguaje**: símbolos/defines + `calls` intra-archivo.
2. **Comunicación entre servicios**: `cross_link` une los servicios que comparten un
   endpoint HTTP (mecanismo language-agnostic).

## Topología (quién llama a quién por HTTP)

| Endpoint        | Sirve (declara)        | Consume (llama)                  |
|-----------------|------------------------|----------------------------------|
| `/api/orders`   | orders (Python)        | gateway (TypeScript)             |
| `/api/inventory`| inventory (Rust)       | gateway (TypeScript)             |
| `/api/payments` | payments (Go)          | orders (Python)                  |
| `/api/notify`   | notifications (Java)   | orders (Python)                  |
| `/api/pricing`  | pricing (C++)          | orders (Python)                  |
| `/api/billing`  | billing (C#)           | admintool (Visual Basic)         |
| `/api/analytics`| analytics (PHP)        | reporting (R), deviceagent (C)   |
| `/api/health`   | bootstrap (Assembly)   | gateway (TypeScript)             |

No está pensado para ejecutarse — MemoryGraf solo parsea el código fuente.
