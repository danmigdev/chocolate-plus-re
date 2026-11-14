ENTRY(ch_boundary_glue)
native_output_callback = 0x194a0;
native_user_flash = 0x19100;
native_vm_flags = 0x1a25d;
native_usb_buffer = 0x1e11932;
native_task_delay = 0x2117d78;
native_irq_enable = 0x201c576;
native_settings_lock = 0x1e1506e;
native_settings_unlock = 0x1e15090;
native_flash_read = 0x1e0cd9c;
native_flash_write = 0x1e0cd8e;
native_config_read = 0x1e13b50;
native_config_write = 0x1e13b5c;
native_save_continue = 0x1e125d6;
native_packet_return = 0x1e1004c;
native_packet_continue = 0x1e0ff58;
SECTIONS {
  .patch 0x1e265f0 : ALIGN(4) { *(.text*) *(.rodata*) }
  .hook_boundary 0x1e0ff54 : { *(.hook_boundary) }
  .hook_after_send 0x1e11a16 : { *(.hook_after_send) }
  .hook_save 0x1e125d2 : { *(.hook_save) }
  .hook_read 0x1e18b44 : { *(.hook_read) }
  .hook_write 0x1e18b48 : { *(.hook_write) }
  ASSERT(SIZEOF(.hook_boundary) == 4, "Boundary hook size")
  ASSERT(SIZEOF(.hook_after_send) == 4, "After-send hook size")
  ASSERT(SIZEOF(.hook_save) == 4, "Save hook size")
  ASSERT(SIZEOF(.hook_read) == 4, "Read pointer size")
  ASSERT(SIZEOF(.hook_write) == 4, "Write pointer size")
  ASSERT(SIZEOF(.patch) <= 0x900, "Extension would move compact VM banks")
  .unexpected : { *(.data*) *(.bss*) *(COMMON) }
  ASSERT(SIZEOF(.unexpected) == 0, "No added static RAM is permitted")
  /DISCARD/ : { *(.comment) *(.note*) *(.debug*) *(.llvm*) }
}
