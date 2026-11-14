.section .text.glue,"ax",@progbits
.global ch_boundary_glue
.global ch_after_send_glue
.global native_save

ch_boundary_glue:
    r0 = r8
    r1 = r7
    r8 += r7
    r5 += r7
    call ch_host_boundary
    if (r0 == 0) goto boundary_continue
    goto native_packet_return
boundary_continue:
    goto native_packet_continue

/* Replace only the native IRQ-enable call. Restore IRQs before entering C.
 * The unchanged following goto bypasses the empty-packet return path.
 */
ch_after_send_glue:
    [--sp] = rets
    call native_irq_enable
    r0 = r5
    r1 = r6
    call ch_host_after_send
    pc = [sp++]

/* Exact displaced prologue from 0x1e125d2; no relative instruction moved. */
native_save:
    [--sp] = {rets, r7-r4}
    sp += -48
    goto native_save_continue

.section .hook_boundary,"ax",@progbits
    goto ch_boundary_glue
.section .hook_after_send,"ax",@progbits
    call ch_after_send_glue
.section .hook_save,"ax",@progbits
    goto ch_config_save
.section .hook_read,"a",@progbits
    .long ch_config_read
.section .hook_write,"a",@progbits
    .long ch_config_write
