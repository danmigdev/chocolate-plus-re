"""NOR flash fault/extent tests for migration and restoration; no hardware I/O."""
import binascii
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tools.install_142 import FlashGate, transfer, verify_preserved, replace_journal
from tools.prepare_142 import (FULL_SIZE, SECTOR_SIZE, PAGE_SIZE,
                               vm_geometry, load_plan, sha, construct_target, COMPACT_END)
from tools.br25_usb import command

SECTOR_ORDER = tuple(range(0x2000, 0x28000, SECTOR_SIZE)) + (0x1000,)
REAL_PLAN = Path(__file__).resolve().parents[1] / "artifacts/migration-142/plan"


class NorDevice:
    def __init__(self, original, target, failure=None, order=SECTOR_ORDER):
        self.memory = bytearray(original)
        self.flash_gate = FlashGate(target, order)
        self.failure = failure
        self.erases, self.programs = [], []

    def execute(self,cdb,outgoing=None,incoming=0):
        self.flash_gate.authorize(cdb,outgoing,incoming)
        code = int.from_bytes(cdb[:2],"big")
        address = int.from_bytes(cdb[2:6],"big")
        if code == 0xFD05:
            return bytes(self.memory[address:address+incoming])
        if code == 0xFB01:
            self.erases.append(address)
            self.memory[address:address+SECTOR_SIZE] = b"\xff" * SECTOR_SIZE
            if self.failure == "bad_blank": self.memory[address] = 0
            if self.failure == "lost_erase": raise OSError("Lost erase reply")
            return cdb[:2] + bytes(14)
        if code == 0xFB04:
            self.programs.append(address)
            for index,value in enumerate(outgoing): self.memory[address+index] &= value
            if self.failure == "lost_program": raise OSError("Lost program reply")
            if self.failure == "bad_program": self.memory[address+4] ^= 1
            return b""
        raise AssertionError("Unexpected command")

    def reply(self,code,args=b""):
        return self.execute(command(code,args),incoming=16)[2:]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.original = bytes(range(256)) * (FULL_SIZE // 256)
        target = bytearray(self.original)
        for address in SECTOR_ORDER: target[address+7] ^= 255
        self.target = bytes(target)
        self.events = []

    def record(self,status,**fields):
        self.events.append((status,fields))

    def test_application_sectors_only_and_complete_readback(self):
        dev = NorDevice(self.original,self.target)
        pair = transfer(dev,self.original,self.target,False,self.record)
        self.assertEqual(pair,(self.target,self.target))
        self.assertEqual(dev.erases,list(SECTOR_ORDER))
        self.assertEqual(len(dev.programs),16 * len(SECTOR_ORDER))
        self.assertEqual(len(set(dev.programs)),16 * len(SECTOR_ORDER))
        verify_preserved(bytes(dev.memory),self.original,SECTOR_ORDER)
        self.assertEqual(self.events[-1][0],"physically_verified_restart_required")

    def test_already_complete_is_read_only(self):
        dev = NorDevice(self.target,self.target)
        transfer(dev,self.original,self.target,False,self.record)
        self.assertEqual(dev.erases+dev.programs,[])

    def test_wrong_device_or_modified_configuration_stops_before_erase(self):
        for address in (0,0x28000,0x37000,0x38000,0x39000,0x3f000):
            current = bytearray(self.original)
            current[address] ^= 1
            dev = NorDevice(current,self.target)
            with self.subTest(address=address),self.assertRaisesRegex(ValueError,"outside patch sectors"):
                transfer(dev,self.original,self.target,False,self.record)
            self.assertEqual(dev.erases+dev.programs,[])

    def test_partial_install_requires_explicit_restore(self):
        current = bytearray(self.original)
        current[SECTOR_ORDER[0]:SECTOR_ORDER[0]+SECTOR_SIZE] = b"\xff" * SECTOR_SIZE
        dev = NorDevice(current,self.target)
        with self.assertRaisesRegex(ValueError,"use restore"):
            transfer(dev,self.original,self.target,False,self.record)
        self.assertEqual(dev.erases+dev.programs,[])
        dev = NorDevice(current,self.original)
        transfer(dev,self.original,self.original,True,self.record)
        self.assertEqual(bytes(dev.memory),self.original)
        self.assertEqual(dev.erases,[SECTOR_ORDER[0]])

    def test_lost_reply_never_retries_and_never_reports_complete(self):
        for failure in ("lost_erase","lost_program","bad_blank","bad_program"):
            dev = NorDevice(self.original,self.target,failure)
            with self.subTest(failure=failure),self.assertRaises((ValueError,OSError)):
                transfer(dev,self.original,self.target,False,self.record)
            self.assertEqual(len(dev.erases),1)
            self.assertEqual(len(dev.programs),int(failure in ("lost_program","bad_program")))
            self.assertNotEqual(self.events[-1][0],"physically_verified_restart_required")

    def test_no_reset_key_whole_chip_or_ram_commands(self):
        gate = FlashGate(self.target,SECTOR_ORDER)
        for code in (0xFB00,0xFB02,0xFB06,0xFB08,0xFC09,0xFC0C,0xFC0D,0xFC12):
            with self.subTest(code=code),self.assertRaises(ValueError):
                gate.authorize(command(code),None,16)

    def test_erase_must_be_armed_exactly_and_cannot_be_repeated(self):
        gate = FlashGate(self.target,SECTOR_ORDER)
        address = SECTOR_ORDER[0]
        cdb = command(0xFB01,address.to_bytes(4,"big"))
        with self.assertRaises(ValueError): gate.authorize(cdb,None,16)
        for other in (0,0x39000,0x3f000,address+1):
            with self.assertRaises(ValueError): gate.arm(other)
        gate.arm(address)
        with self.assertRaises(ValueError): gate.authorize(command(0xFB01,bytes(4)),None,16)
        gate.authorize(cdb,None,16)
        with self.assertRaises(ValueError): gate.authorize(cdb,None,16)
        with self.assertRaises(ValueError): gate.arm(SECTOR_ORDER[1])

    def test_program_requires_two_blank_checks_and_exact_page_and_crc(self):
        gate = FlashGate(self.target,SECTOR_ORDER)
        address = SECTOR_ORDER[0]
        data = self.target[address:address+PAGE_SIZE]
        fields = address.to_bytes(4,"big")+b"\x01\x00\x00"+binascii.crc_hqx(data,0).to_bytes(2,"little")
        cdb = command(0xFB04,fields)
        gate.arm(address)
        gate.authorize(command(0xFB01,address.to_bytes(4,"big")),None,16)
        with self.assertRaises(ValueError): gate.authorize(cdb,data,0)
        with self.assertRaises(ValueError): gate.blank_verified(b"\xff"*SECTOR_SIZE,bytes(SECTOR_SIZE))
        gate.blank_verified(b"\xff"*SECTOR_SIZE,b"\xff"*SECTOR_SIZE)
        with self.assertRaises(ValueError): gate.authorize(cdb,bytes(PAGE_SIZE),0)
        with self.assertRaises(ValueError): gate.authorize(cdb[:-1]+b"\0",data,0)
        gate.authorize(cdb,data,0)
        with self.assertRaises(ValueError): gate.authorize(cdb,data,0)

    def test_native_vm_bank_geometry_is_unchanged_by_one_page_extension(self):
        expected = dict(start=0x28000,bank_bytes=0x1000,second_bank=0x29000,end=0x2a000)
        for start in (0x27600,0x27700):
            self.assertEqual(vm_geometry(start,0x37000-start),expected)
        self.assertNotEqual(vm_geometry(0x28000,0xf000),expected)
        with self.assertRaises(ValueError): vm_geometry(0x27600,1024)

    @unittest.skipUnless((REAL_PLAN / "plan.json").is_file(),
                         "Optional private device backup is not distributed")
    def test_real_plan_installs_and_restores_every_original_byte_in_simulation(self):
        plan,original,target = load_plan(REAL_PLAN)
        order = plan["sector_order"]
        verify_preserved(target,original,order)
        self.assertEqual(plan["vm_geometry"]["start"],0x28000)
        self.assertEqual(original[0x39000:0x3f000],target[0x39000:0x3f000])
        self.assertEqual(original[plan["preserved_from"]:],target[plan["preserved_from"]:])
        self.assertEqual(order[-1],0x1000)
        dev = NorDevice(original,target,order=order)
        transfer(dev,original,target,False,self.record)
        self.assertEqual(bytes(dev.memory),target)
        self.assertEqual(dev.erases[-1],0x1000)
        dev = NorDevice(dev.memory,original,order=order)
        transfer(dev,original,original,True,self.record)
        self.assertEqual(bytes(dev.memory),original)

    def test_no_scope_can_include_boot_vm_or_configuration(self):
        for address in (0,0x28000,0x37000,0x38000,0x39000,0x3f000,0x1001):
            with self.subTest(address=address),self.assertRaises(ValueError):
                FlashGate(self.target,[address])

    def test_transient_windows_journal_lock_retries_only_local_rename(self):
        error=PermissionError('Reader denied delete sharing')
        error.winerror=5
        with patch('tools.install_142.os.replace',side_effect=[error,None]) as rename, \
                patch('tools.install_142.time.sleep') as sleep:
            replace_journal('report.tmp','report.json')
        self.assertEqual(rename.call_count,2)
        sleep.assert_called_once_with(0.025)

    def test_permanent_journal_errors_stop_and_retry_limit_is_bounded(self):
        error=PermissionError('Permanent sharing violation')
        error.winerror=32
        with patch('tools.install_142.os.replace',side_effect=error) as rename, \
                patch('tools.install_142.time.sleep'), self.assertRaises(PermissionError):
            replace_journal('report.tmp','report.json')
        self.assertEqual(rename.call_count,200)
        with patch('tools.install_142.os.replace',side_effect=OSError('Disk full')) as rename, \
                patch('tools.install_142.time.sleep') as sleep, self.assertRaises(OSError):
            replace_journal('report.tmp','report.json')
        self.assertEqual(rename.call_count,1)
        sleep.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing semantics')
    def test_real_windows_reader_can_release_journal_without_losing_record(self):
        with tempfile.TemporaryDirectory() as name:
            destination=Path(name)/'report.json'
            temporary=Path(name)/'report.tmp'
            destination.write_bytes(b'old')
            temporary.write_bytes(b'new durable record')
            held=destination.open('rb')
            timer=threading.Timer(0.15,held.close)
            timer.start()
            try:
                replace_journal(temporary,destination)
                self.assertEqual(destination.read_bytes(),b'new durable record')
            finally:
                timer.join()
                held.close()

    @unittest.skipUnless((REAL_PLAN / "plan.json").is_file(), "Private backup is not distributed")
    def test_real_plan_rejects_tampering_even_with_updated_manifest_hashes(self):
        plan,original,target = load_plan(REAL_PLAN)
        for address in (0,0x1000,COMPACT_END,0x28000,0x39000):
            changed=bytearray(target)
            changed[address] ^= 1
            forged=dict(plan,target_sha256=sha(changed))
            with self.subTest(address=address),tempfile.TemporaryDirectory() as name:
                directory=Path(name)
                (directory/'original.bin').write_bytes(original)
                (directory/'target.bin').write_bytes(changed)
                (directory/'plan.json').write_text(json.dumps(forged),encoding='utf-8')
                with self.assertRaises(ValueError): load_plan(directory)

    @unittest.skipUnless((REAL_PLAN / "plan.json").is_file(), "Private backup is not distributed")
    def test_unknown_installed_build_is_rejected(self):
        _,original,target=load_plan(REAL_PLAN)
        changed=bytearray(original)
        changed[0x1120] ^= 1
        with self.assertRaisesRegex(ValueError,'both audited'):
            construct_target(changed,target[:COMPACT_END])


if __name__ == "__main__":
    unittest.main(verbosity=2)
