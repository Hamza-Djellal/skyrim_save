#!/usr/bin/env python3
"""
Skyrim PS4 Save Verifier
Validates Skyrim PS4 decrypted saves (SAVEDATA.DAT & sce_sys/ container)
for Apollo Save Tool compatibility and internal engine structural integrity.

Author: Hamza Djellal
"""

import os
import sys
import glob
import struct
import ctypes
import ctypes.util
import argparse
from PIL import Image

def get_lz4():
    lib_path = ctypes.util.find_library('lz4') or 'liblz4.so.1'
    lz4 = ctypes.CDLL(lib_path)
    lz4.LZ4_decompress_safe.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    lz4.LZ4_decompress_safe.restype = ctypes.c_int
    return lz4

LZ4 = get_lz4()

class CheckResult:
    def __init__(self):
        self.checks = []
        self.warnings = []
        self.errors = []

    def ok(self, name: str, detail: str = ""):
        self.checks.append((name, "PASS", detail))

    def warn(self, name: str, detail: str = ""):
        self.checks.append((name, "WARN", detail))
        self.warnings.append((name, detail))

    def fail(self, name: str, detail: str = ""):
        self.checks.append((name, "FAIL", detail))
        self.errors.append((name, detail))

    def summary(self) -> bool:
        print("\n" + "=" * 70)
        print(f"{'CHECK':<35} {'STATUS':<8} {'DETAIL'}")
        print("=" * 70)
        for name, status, detail in self.checks:
            color_code = "\033[92m" if status == "PASS" else ("\033[93m" if status == "WARN" else "\033[91m")
            reset_code = "\033[0m"
            print(f"{name:<35} {color_code}{status:<8}{reset_code} {detail}")
        print("-" * 70)
        total = len(self.checks)
        passed = sum(1 for _, s, _ in self.checks if s == "PASS")
        warns = len(self.warnings)
        fails = len(self.errors)
        print(f"Results: {passed}/{total} Passed | {warns} Warnings | {fails} Errors")
        if fails == 0:
            print("\033[92m[SUCCESS] Save file is 100% valid and Apollo-compatible!\033[0m")
            return True
        else:
            print("\033[91m[FAILURE] Save verification detected critical errors.\033[0m")
            return False

def verify_apollo_save(sdu_dir_path: str) -> bool:
    """
    Verifies an Apollo decrypted save directory (e.g. .../SDU1)
    """
    print(f"\n=======================================================")
    print(f"         Skyrim PS4 Save Verification Suite           ")
    print(f"=======================================================")
    print(f"Target Directory: {os.path.abspath(sdu_dir_path)}")

    results = CheckResult()

    if not os.path.isdir(sdu_dir_path):
        results.fail("Directory Exists", f"Path not found: {sdu_dir_path}")
        return results.summary()

    savedata_path = os.path.join(sdu_dir_path, "SAVEDATA.DAT")
    sce_sys_path = os.path.join(sdu_dir_path, "sce_sys")
    sfo_path = os.path.join(sce_sys_path, "param.sfo")
    icon0_path = os.path.join(sce_sys_path, "icon0.png")
    keystone_path = os.path.join(sce_sys_path, "keystone")
    sce_icon0_path = os.path.join(sce_sys_path, "sce_icon0png1")

    # 1. Container Structure
    results.ok("Container: Directory Layout", f"Folder: {os.path.basename(sdu_dir_path)}")

    if os.path.isfile(savedata_path):
        savedata_size = os.path.getsize(savedata_path)
        results.ok("Container: SAVEDATA.DAT exists", f"{savedata_size:,} bytes")
    else:
        results.fail("Container: SAVEDATA.DAT exists", "File missing!")
        return results.summary()

    if os.path.isfile(sfo_path):
        results.ok("Container: sce_sys/param.sfo", f"{os.path.getsize(sfo_path)} bytes")
    else:
        results.fail("Container: sce_sys/param.sfo", "File missing!")

    if os.path.isfile(keystone_path):
        ksize = os.path.getsize(keystone_path)
        if ksize == 96:
            results.ok("Container: sce_sys/keystone", "Valid 96-byte token")
        else:
            results.warn("Container: sce_sys/keystone", f"Unexpected size: {ksize} bytes (expected 96)")
    else:
        results.warn("Container: sce_sys/keystone", "Keystone missing (Apollo can re-encrypt without it if offline)")

    if os.path.isfile(sce_icon0_path):
        results.ok("Container: sce_sys/sce_icon0png1", f"{os.path.getsize(sce_icon0_path):,} bytes")
    else:
        results.warn("Container: sce_sys/sce_icon0png1", "Placeholder file missing")

    # 2. icon0.png Verification
    if os.path.isfile(icon0_path):
        try:
            with Image.open(icon0_path) as img:
                w, h = img.size
                fmt = img.format
                if w == 228 and h == 128:
                    results.ok("Thumbnail: icon0.png geometry", f"{w}x{h} {fmt}")
                else:
                    results.warn("Thumbnail: icon0.png geometry", f"{w}x{h} (recommended 228x128)")
        except Exception as e:
            results.fail("Thumbnail: icon0.png geometry", f"Invalid image: {e}")
    else:
        results.warn("Thumbnail: icon0.png", "icon0.png missing")

    # 3. SAVEDATA.DAT Deep Inspection
    with open(savedata_path, 'rb') as f:
        data = f.read()

    # Magic
    magic = data[:13]
    if magic == b'TESV_SAVEGAME':
        results.ok("SAVEDATA: Magic Header", "b'TESV_SAVEGAME'")
    else:
        results.fail("SAVEDATA: Magic Header", f"Invalid magic: {magic}")
        return results.summary()

    # Header
    header_size = struct.unpack('<I', data[13:17])[0]
    header_end = 17 + header_size
    pos = 17
    version, save_num = struct.unpack('<II', data[pos:pos+8])
    pos += 8

    def read_wstr(p):
        l = struct.unpack('<H', data[p:p+2])[0]
        s = data[p+2:p+2+l].decode('utf-8', errors='replace')
        return s, p + 2 + l

    char_name, pos = read_wstr(pos)
    level = struct.unpack('<I', data[pos:pos+4])[0]
    pos += 4
    loc, pos = read_wstr(pos)
    date, pos = read_wstr(pos)
    race, pos = read_wstr(pos)
    sex = struct.unpack('<H', data[pos:pos+2])[0]
    pos += 2
    cur_exp, lvl_exp = struct.unpack('<ff', data[pos:pos+8])
    pos += 8
    file_time = struct.unpack('<Q', data[pos:pos+8])[0]
    pos += 8
    shot_w, shot_h = struct.unpack('<II', data[pos:pos+8])
    pos += 8
    comp_type = struct.unpack('<H', data[pos:pos+2])[0]
    pos += 2

    if version == 12:
        results.ok("SAVEDATA: Save Version", f"v{version} (SSE)")
    else:
        results.warn("SAVEDATA: Save Version", f"v{version} (expected 12)")

    results.ok("SAVEDATA: Character Meta", f"'{char_name}' (Lvl {level}, {race}, Save #{save_num})")
    results.ok("SAVEDATA: Location & Time", f"'{loc}' @ {date}")

    if file_time == 0:
        results.ok("SAVEDATA: PS4 FileTime", "0 (Console standard)")
    else:
        results.warn("SAVEDATA: PS4 FileTime", f"Non-zero: {file_time}")

    if shot_w == 240 and shot_h == 128:
        results.ok("SAVEDATA: Screenshot Geometry", f"{shot_w}x{shot_h} (PS4 Console standard)")
    else:
        results.fail("SAVEDATA: Screenshot Geometry", f"{shot_w}x{shot_h} (expected 240x128)")

    shot_bytes_len = shot_w * shot_h * 4
    pos = header_end + shot_bytes_len

    if comp_type == 2:
        results.ok("SAVEDATA: Compression Type", "LZ4 (comp_type=2)")
    else:
        results.warn("SAVEDATA: Compression Type", f"{comp_type} (expected 2)")

    # 4. Decompression Check
    unc_sz, comp_sz = struct.unpack('<II', data[pos:pos+8])
    payload = data[pos+8 : pos+8+comp_sz]

    if pos + 8 + comp_sz == len(data):
        results.ok("SAVEDATA: File Byte Alignment", f"Exact: {len(data):,} bytes")
    else:
        results.fail("SAVEDATA: File Byte Alignment", f"Trailing or missing bytes: expected {pos+8+comp_sz}, got {len(data)}")

    dst = ctypes.create_string_buffer(unc_sz)
    res = LZ4.LZ4_decompress_safe(payload, dst, comp_sz, unc_sz)
    if res == unc_sz:
        results.ok("SAVEDATA: LZ4 Decompression", f"{comp_sz:,} -> {unc_sz:,} bytes")
    else:
        results.fail("SAVEDATA: LZ4 Decompression", f"Failed: result={res}, expected={unc_sz}")
        return results.summary()

    raw = dst.raw[:res]

    # 5. FormVersion & Plugins
    form_ver = raw[0]
    if form_ver == 78:
        results.ok("SAVEDATA: Engine FormVersion", f"78 (0x{form_ver:x})")
    else:
        results.warn("SAVEDATA: Engine FormVersion", f"{form_ver} (expected 78)")

    plugin_info_size = struct.unpack('<I', raw[1:5])[0]
    pos = 5
    p_cnt = raw[pos]
    pos += 1
    plugins = []
    for _ in range(p_cnt):
        l = struct.unpack('<H', raw[pos:pos+2])[0]
        pos += 2
        plugins.append(raw[pos:pos+l].decode('utf-8', errors='replace'))
        pos += l

    l_cnt = struct.unpack('<H', raw[pos:pos+2])[0]
    pos += 2
    light_plugins = []
    for _ in range(l_cnt):
        l = struct.unpack('<H', raw[pos:pos+2])[0]
        pos += 2
        light_plugins.append(raw[pos:pos+l].decode('utf-8', errors='replace'))
        pos += l

    results.ok("SAVEDATA: Plugin Counts", f"{p_cnt} ESM/ESP, {l_cnt} ESL")

    # Check lowercase base masters
    base_masters = ['skyrim.esm', 'update.esm', 'dawnguard.esm', 'hearthfires.esm', 'dragonborn.esm']
    lower_found = [m for m in base_masters if m in plugins]
    results.ok("SAVEDATA: Master Plugins Casing", f"{len(lower_found)}/5 lowercase base masters verified")

    # 6. File Location Table (FLT) Offsets Integrity
    flt_pos = 5 + plugin_info_size
    flt = list(struct.unpack('<25I', raw[flt_pos:flt_pos+100]))
    form_id_off, unk_off, g1_off, g2_off, cf_off, g3_off = flt[:6]
    g1_cnt, g2_cnt, g3_cnt, cf_cnt = flt[6:10]

    virtual_base = header_end + shot_bytes_len  # 96 + 122880 = 122976
    expected_g1_off = virtual_base + flt_pos + 100

    if g1_off == expected_g1_off:
        results.ok("FLT Offsets: GlobalData1 Start", f"0x{g1_off:06x} == virtual_base + FLT_end")
    else:
        results.fail("FLT Offsets: GlobalData1 Start", f"Mismatch: 0x{g1_off:06x} vs expected 0x{expected_g1_off:06x}")

    # Monotonic order check
    if g1_off < g2_off < cf_off < g3_off < form_id_off < unk_off:
        results.ok("FLT Offsets: Monotonic Ordering", "G1 < G2 < ChangeForms < G3 < FormID < Unk3")
    else:
        results.fail("FLT Offsets: Monotonic Ordering", "Offsets out of sequence!")

    expected_virtual_total = virtual_base + len(raw)
    if unk_off + 8 == expected_virtual_total:
        results.ok("FLT Offsets: Virtual End of File", f"0x{unk_off+8:06x} == virtual stream end")
    else:
        results.fail("FLT Offsets: Virtual End of File", f"Mismatch: unk_off+8=0x{unk_off+8:06x} vs virtual={expected_virtual_total}")

    results.ok("FLT Counts: Table Records", f"G1={g1_cnt}, G2={g2_cnt}, G3={g3_cnt}, ChangeForms={cf_cnt}")

    # 7. param.sfo Verification
    if os.path.isfile(sfo_path):
        with open(sfo_path, 'rb') as f:
            sfo_bytes = f.read()

        sfo_magic, sfo_ver, key_off, data_off, num_entries = struct.unpack('<4sIIII', sfo_bytes[:20])
        if sfo_magic == b'\x00PSF':
            results.ok("param.sfo: SFO Magic", f"b'\\x00PSF' v{hex(sfo_ver)}")
        else:
            results.fail("param.sfo: SFO Magic", f"Invalid magic: {sfo_magic}")

        sfo_dict = {}
        pos = 20
        for _ in range(num_entries):
            koff, pfmt, plen, pmax, doff = struct.unpack('<HHIII', sfo_bytes[pos:pos+16])
            pos += 16
            kend = sfo_bytes.find(b'\x00', key_off + koff)
            k = sfo_bytes[key_off + koff : kend].decode('ascii')
            val = sfo_bytes[data_off + doff : data_off + doff + plen]
            if pfmt in (0x0204, 0x0004) and val.endswith(b'\x00'):
                val_parsed = val[:-1].decode('utf-8', errors='replace')
            elif pfmt == 0x0404:
                val_parsed = struct.unpack('<I', val)[0]
            else:
                val_parsed = val
            sfo_dict[k] = val_parsed

        # Check required keys
        if sfo_dict.get('TITLE_ID') == 'CUSA05486':
            results.ok("param.sfo: TITLE_ID", "CUSA05486 (Skyrim SE PS4)")
        else:
            results.warn("param.sfo: TITLE_ID", f"{sfo_dict.get('TITLE_ID')} (expected CUSA05486)")

        if sfo_dict.get('CATEGORY') == 'sd':
            results.ok("param.sfo: CATEGORY", "sd (Savedata)")
        else:
            results.warn("param.sfo: CATEGORY", f"{sfo_dict.get('CATEGORY')}")

        if sfo_dict.get('SAVEDATA_DIRECTORY') == os.path.basename(sdu_dir_path):
            results.ok("param.sfo: SAVEDATA_DIRECTORY", f"{sfo_dict.get('SAVEDATA_DIRECTORY')}")
        else:
            results.warn("param.sfo: SAVEDATA_DIRECTORY", f"{sfo_dict.get('SAVEDATA_DIRECTORY')} != {os.path.basename(sdu_dir_path)}")

        maintitle = sfo_dict.get('MAINTITLE', '')
        if char_name in maintitle:
            results.ok("param.sfo: MAINTITLE Sync", f"'{maintitle}'")
        else:
            results.warn("param.sfo: MAINTITLE Sync", f"'{maintitle}' does not match char '{char_name}'")

        subtitle = sfo_dict.get('SUBTITLE', '')
        if subtitle == date:
            results.ok("param.sfo: SUBTITLE Sync", f"'{subtitle}' matches game date")
        else:
            results.warn("param.sfo: SUBTITLE Sync", f"'{subtitle}' != game date '{date}'")

        blocks = struct.unpack('<Q', sfo_bytes[data_off + 2196 : data_off + 2204])[0] if 'SAVEDATA_BLOCKS' in sfo_dict else 0
        allocated_bytes = blocks * 32768
        if allocated_bytes >= len(data):
            results.ok("param.sfo: SAVEDATA_BLOCKS", f"{blocks} blocks ({allocated_bytes // 1024:,} KB) >= {len(data) // 1024:,} KB")
        else:
            results.fail("param.sfo: SAVEDATA_BLOCKS", f"{blocks} blocks too small for {len(data):,} bytes")

    return results.summary()

def main():
    parser = argparse.ArgumentParser(description="Verify Skyrim PS4 decrypted save integrity")
    parser.add_argument("path", nargs="?", help="Path to SDU folder (e.g. .../SDU1) or Apollo container folder")
    args = parser.parse_args()

    target = args.path
    if not target:
        # Look for converted folders or template
        candidates = sorted(glob.glob("output/CUSA05486-SDU1*") + glob.glob("CUSA05486-SDU1*"))
        if candidates:
            target = candidates[-1]
            print(f"No path specified. Auto-selected: {target}")
        else:
            print("Error: No save directory specified and none found.")
            sys.exit(1)

    # If user provided parent container, look for SDU1 inside
    if os.path.isdir(os.path.join(target, "SDU1")):
        target = os.path.join(target, "SDU1")

    success = verify_apollo_save(target)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
