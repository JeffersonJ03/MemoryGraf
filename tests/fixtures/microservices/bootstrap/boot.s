/* Bootstrap health probe (Assembly) — expone /api/health. */
.section .rodata
health_path:
    .asciz "/api/health"

.text
.globl _start

check:
    ret

_start:
    call check
    ret
