#!/usr/bin/env python3
"""
Skyrim PC (.ess) to PS4 Apollo Save Converter
Converts Skyrim Special Edition PC save files into decrypted PS4 saves
compatible with Apollo Save Tool (CUSA05486).

Author: Hamza Djellal
"""

import os
import sys
import glob
import time
import shutil
import struct
import ctypes
import ctypes.util
import argparse
from datetime import datetime
from PIL import Image

# ---------------------------------------------------------------------------
# LZ4 Compression helper via system liblz4
# ---------------------------------------------------------------------------
def get_lz4():
    lib_path = ctypes.util.find_library('lz4') or 'liblz4.so.1'
    try:
        lz4 = ctypes.CDLL(lib_path)
    except Exception as e:
        raise RuntimeError(f"Failed to load LZ4 library ({lib_path}): {e}")

    lz4.LZ4_compressBound.argtypes = [ctypes.c_int]
    lz4.LZ4_compressBound.restype = ctypes.c_int

    lz4.LZ4_compress_default.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    lz4.LZ4_compress_default.restype = ctypes.c_int

    lz4.LZ4_decompress_safe.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    lz4.LZ4_decompress_safe.restype = ctypes.c_int

    return lz4

LZ4 = get_lz4()

def lz4_decompress(data: bytes, uncompressed_size: int) -> bytes:
    dst = ctypes.create_string_buffer(uncompressed_size)
    res = LZ4.LZ4_decompress_safe(data, dst, len(data), uncompressed_size)
    if res < 0:
        raise ValueError(f"LZ4 decompression failed with error code {res}")
    return dst.raw[:res]

def lz4_compress(data: bytes) -> bytes:
    src_len = len(data)
    max_dst = LZ4.LZ4_compressBound(src_len)
    dst = ctypes.create_string_buffer(max_dst)
    res = LZ4.LZ4_compress_default(data, dst, src_len, max_dst)
    if res <= 0:
        raise ValueError(f"LZ4 compression failed with code {res}")
    return dst.raw[:res]

# ---------------------------------------------------------------------------
# ParamSfo Parser & Patcher
# ---------------------------------------------------------------------------
class ParamSfo:
    def __init__(self, data: bytes):
        self.raw = bytearray(data)
        magic, version, key_off, data_off, num = struct.unpack('<4sIIII', self.raw[:20])
        if magic != b'\x00PSF':
            raise ValueError("Invalid SFO magic")
        self.version = version
        self.key_off = key_off
        self.data_off = data_off
        self.entries = []
        pos = 20
        for i in range(num):
            koff, pfmt, plen, pmax, doff = struct.unpack('<HHIII', self.raw[pos:pos+16])
            kend = self.raw.find(b'\x00', key_off + koff)
            key = self.raw[key_off + koff : kend].decode('ascii')
            val = bytes(self.raw[data_off + doff : data_off + doff + plen])
            self.entries.append({
                'key': key, 'fmt': pfmt, 'len': plen, 'max': pmax,
                'doff': doff, 'idx_pos': pos, 'val': val
            })
            pos += 16

    def get_entry(self, key: str):
        for e in self.entries:
            if e['key'] == key:
                return e
        return None

    def set_string(self, key: str, text: str):
        encoded = text.encode('utf-8') + b'\x00'
        e = self.get_entry(key)
        if not e:
            raise KeyError(f"Key {key} not found in SFO")
        if len(encoded) > e['max']:
            encoded = encoded[:e['max']-1] + b'\x00'
        struct.pack_into('<I', self.raw, e['idx_pos'] + 4, len(encoded))
        start = self.data_off + e['doff']
        self.raw[start : start + len(encoded)] = encoded
        if len(encoded) < e['max']:
            self.raw[start + len(encoded) : start + e['max']] = b'\x00' * (e['max'] - len(encoded))

    def set_blocks(self, num_blocks: int):
        e = self.get_entry('SAVEDATA_BLOCKS')
        if not e:
            raise KeyError("SAVEDATA_BLOCKS not found in SFO")
        start = self.data_off + e['doff']
        struct.pack_into('<Q', self.raw, start, num_blocks)

    def to_bytes(self) -> bytes:
        return bytes(self.raw)

# ---------------------------------------------------------------------------
# Skyrim Save (.ess) Parser
# ---------------------------------------------------------------------------
class SkyrimSave:
    def __init__(self, filepath: str):
        self.filepath = filepath
        with open(filepath, 'rb') as f:
            self.file_bytes = f.read()
        self._parse()

    def _parse(self):
        data = self.file_bytes
        self.magic = data[:13]
        if self.magic != b'TESV_SAVEGAME':
            raise ValueError(f"Invalid magic: {self.magic}")

        self.header_size = struct.unpack('<I', data[13:17])[0]
        header_end = 17 + self.header_size

        pos = 17
        self.version, self.save_num = struct.unpack('<II', data[pos:pos+8])
        pos += 8

        def read_wstr(p):
            length = struct.unpack('<H', data[p:p+2])[0]
            s = data[p+2:p+2+length].decode('utf-8', errors='replace')
            return s, p + 2 + length

        self.player_name, pos = read_wstr(pos)
        self.player_level = struct.unpack('<I', data[pos:pos+4])[0]
        pos += 4
        self.player_location, pos = read_wstr(pos)
        self.game_date, pos = read_wstr(pos)
        self.player_race, pos = read_wstr(pos)
        self.player_sex = struct.unpack('<H', data[pos:pos+2])[0]
        pos += 2
        self.cur_exp, self.lvl_up_exp = struct.unpack('<ff', data[pos:pos+8])
        pos += 8
        self.file_time = struct.unpack('<Q', data[pos:pos+8])[0]
        pos += 8
        self.shot_w, self.shot_h = struct.unpack('<II', data[pos:pos+8])
        pos += 8
        self.compression_type = struct.unpack('<H', data[pos:pos+2])[0]
        pos += 2

        self.header_bytes = data[17:header_end]

        shot_len = self.shot_w * self.shot_h * 4
        self.shot_data = data[header_end : header_end + shot_len]

        pos = header_end + shot_len
        self.uncompressed_size, self.compressed_size = struct.unpack('<II', data[pos:pos+8])
        compressed_payload = data[pos+8 : pos+8+self.compressed_size]

        self.uncompressed_data = lz4_decompress(compressed_payload, self.uncompressed_size)
        self._parse_uncompressed()

    def _parse_uncompressed(self):
        raw = self.uncompressed_data
        self.form_version = raw[0]
        self.plugin_info_size = struct.unpack('<I', raw[1:5])[0]

        pos = 5
        plugin_count = raw[pos]
        pos += 1
        self.plugins = []
        for _ in range(plugin_count):
            plen = struct.unpack('<H', raw[pos:pos+2])[0]
            pos += 2
            self.plugins.append(raw[pos:pos+plen].decode('utf-8', errors='replace'))
            pos += plen

        light_count = struct.unpack('<H', raw[pos:pos+2])[0]
        pos += 2
        self.light_plugins = []
        for _ in range(light_count):
            plen = struct.unpack('<H', raw[pos:pos+2])[0]
            pos += 2
            self.light_plugins.append(raw[pos:pos+plen].decode('utf-8', errors='replace'))
            pos += plen

        flt_pos = 5 + self.plugin_info_size
        self.flt_pos = flt_pos
        self.flt = list(struct.unpack('<25I', raw[flt_pos:flt_pos+100]))

# ---------------------------------------------------------------------------
# Conversion Engine
# ---------------------------------------------------------------------------
def convert_skyrim_save(
    input_ess_path: str,
    template_dir: str,
    output_base_dir: str,
    save_dir_name: str = "SDU1",
    include_timestamp: bool = False
) -> str:
    """
    Converts a PC .ess save file to a PS4 Apollo save directory.
    Places SAVEDATA.DAT and sce_sys/ directly inside the container folder
    for 100% compatibility with Apollo's USB save scanner.
    """
    print(f"\n=======================================================")
    print(f"  Skyrim PC -> PS4 Save Converter (Apollo Compatible)  ")
    print(f"=======================================================")
    print(f"Input PC Save:   {input_ess_path}")
    print(f"Template PS4:    {template_dir}")
    print(f"Target Save Dir: {save_dir_name}")

    if not os.path.isfile(input_ess_path):
        raise FileNotFoundError(f"Input file not found: {input_ess_path}")

    # 1. Parse PC Save
    pc_save = SkyrimSave(input_ess_path)
    print(f"\n[1/5] Parsed PC Save:")
    print(f"  Character:     {pc_save.player_name} (Level {pc_save.player_level})")
    print(f"  Location:      {pc_save.player_location}")
    print(f"  Game Date:     {pc_save.game_date}")
    print(f"  Save Number:   {pc_save.save_num}")
    print(f"  Screenshot:    {pc_save.shot_w}x{pc_save.shot_h} ({len(pc_save.shot_data)} bytes)")
    print(f"  Uncompressed:  {pc_save.uncompressed_size:,} bytes")
    print(f"  Plugins:       {len(pc_save.plugins)} ESM/ESP, {len(pc_save.light_plugins)} ESL")

    # 2. Resize Screenshot to PS4 Console Specs (240x128)
    ps4_shot_w = 240
    ps4_shot_h = 128
    ps4_shot_len = ps4_shot_w * ps4_shot_h * 4

    pc_img = Image.frombytes('RGBA', (pc_save.shot_w, pc_save.shot_h), pc_save.shot_data)
    ps4_img = pc_img.resize((ps4_shot_w, ps4_shot_h), Image.Resampling.LANCZOS)
    ps4_shot_data = ps4_img.tobytes()

    icon0_img = pc_img.resize((228, 128), Image.Resampling.LANCZOS)

    shot_delta = ps4_shot_len - len(pc_save.shot_data)
    print(f"\n[2/5] Adjusted Screenshot:")
    print(f"  Resolution:    {ps4_shot_w}x{ps4_shot_h}")
    print(f"  Size Delta:    {shot_delta} bytes (-0x{-shot_delta:x})")

    # 3. Modify Uncompressed Stream
    raw = bytearray(pc_save.uncompressed_data)

    base_masters = [b'Skyrim.esm', b'Update.esm', b'Dawnguard.esm', b'HearthFires.esm', b'Dragonborn.esm']
    for master in base_masters:
        pos = raw.find(master, 5, 5 + pc_save.plugin_info_size)
        if pos != -1:
            raw[pos : pos + len(master)] = master.lower()
            print(f"  Normalized master plugin: {master.decode()} -> {master.lower().decode()}")

    flt_pos = pc_save.flt_pos
    flt_offsets = list(struct.unpack('<6I', raw[flt_pos : flt_pos + 24]))
    new_flt_offsets = [off + shot_delta for off in flt_offsets]

    for i, (old_off, new_off) in enumerate(zip(flt_offsets, new_flt_offsets)):
        struct.pack_into('<I', raw, flt_pos + (i * 4), new_off)

    print(f"\n[3/5] Recalculated File Location Table (FLT) Offsets:")
    offset_names = ['FormIDArray', 'UnknownTable3', 'GlobalData1', 'GlobalData2', 'ChangeForms', 'GlobalData3']
    for name, old_off, new_off in zip(offset_names, flt_offsets, new_flt_offsets):
        print(f"  {name:15s}: {old_off:8d} (0x{old_off:06x}) -> {new_off:8d} (0x{new_off:06x})")

    # 4. Recompress with LZ4
    print(f"\n[4/5] Recompressing payload with LZ4...")
    new_uncompressed_data = bytes(raw)
    new_compressed_data = lz4_compress(new_uncompressed_data)
    print(f"  Uncompressed:  {len(new_uncompressed_data):,} bytes")
    print(f"  Compressed:    {len(new_compressed_data):,} bytes")

    # 5. Build PS4 Header
    def pack_wstr(s: str) -> bytes:
        b = s.encode('utf-8')
        return struct.pack('<H', len(b)) + b

    header_payload = bytearray()
    header_payload += struct.pack('<II', pc_save.version, pc_save.save_num)
    header_payload += pack_wstr(pc_save.player_name)
    header_payload += struct.pack('<I', pc_save.player_level)
    header_payload += pack_wstr(pc_save.player_location)
    header_payload += pack_wstr(pc_save.game_date)
    header_payload += pack_wstr(pc_save.player_race)
    header_payload += struct.pack('<Hff', pc_save.player_sex, pc_save.cur_exp, pc_save.lvl_up_exp)
    header_payload += struct.pack('<Q', 0)
    header_payload += struct.pack('<IIH', ps4_shot_w, ps4_shot_h, pc_save.compression_type)

    header_size = len(header_payload)
    ps4_savedata = bytearray()
    ps4_savedata += b'TESV_SAVEGAME'
    ps4_savedata += struct.pack('<I', header_size)
    ps4_savedata += header_payload
    ps4_savedata += ps4_shot_data
    ps4_savedata += struct.pack('<II', len(new_uncompressed_data), len(new_compressed_data))
    ps4_savedata += new_compressed_data

    # 6. Prepare Apollo Output Directory
    title_id = "CUSA05486"
    if include_timestamp:
        timestamp_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        apollo_container_name = f"{title_id}-{save_dir_name}_{timestamp_str}"
    else:
        apollo_container_name = f"{title_id}-{save_dir_name}"

    output_container_dir = os.path.join(output_base_dir, apollo_container_name)
    output_sce_sys = os.path.join(output_container_dir, "sce_sys")
    os.makedirs(output_sce_sys, exist_ok=True)

    # Find template files
    template_sdu = os.path.join(template_dir, save_dir_name)
    if not os.path.isdir(template_sdu):
        subdirs = [d for d in glob.glob(os.path.join(template_dir, "*")) if os.path.isdir(d)]
        for d in subdirs:
            if os.path.isdir(os.path.join(d, "sce_sys")):
                template_sdu = d
                break
        if not os.path.isdir(template_sdu):
            template_sdu = template_dir

    template_sce_sys = os.path.join(template_sdu, "sce_sys")
    if not os.path.isdir(template_sce_sys):
        # Check if sce_sys is directly in template_dir
        if os.path.isdir(os.path.join(template_dir, "sce_sys")):
            template_sce_sys = os.path.join(template_dir, "sce_sys")
        else:
            raise FileNotFoundError(f"Template sce_sys directory not found at: {template_sce_sys}")

    # 7. Write SAVEDATA.DAT directly in container
    savedata_path = os.path.join(output_container_dir, "SAVEDATA.DAT")
    with open(savedata_path, 'wb') as f:
        f.write(ps4_savedata)
    print(f"\n[5/5] Generated PS4 SAVEDATA.DAT ({len(ps4_savedata):,} bytes)")

    # Also create nested save_dir_name folder for backward compatibility
    output_nested_sdu = os.path.join(output_container_dir, save_dir_name)
    output_nested_sce_sys = os.path.join(output_nested_sdu, "sce_sys")
    os.makedirs(output_nested_sce_sys, exist_ok=True)
    with open(os.path.join(output_nested_sdu, "SAVEDATA.DAT"), 'wb') as f:
        f.write(ps4_savedata)

    # 8. Copy hardware keystones and templates
    keystone_src = os.path.join(template_sce_sys, "keystone")
    if os.path.exists(keystone_src):
        shutil.copy2(keystone_src, os.path.join(output_sce_sys, "keystone"))
        shutil.copy2(keystone_src, os.path.join(output_nested_sce_sys, "keystone"))

    icon_buf_src = os.path.join(template_sce_sys, "sce_icon0png1")
    if os.path.exists(icon_buf_src):
        shutil.copy2(icon_buf_src, os.path.join(output_sce_sys, "sce_icon0png1"))
        shutil.copy2(icon_buf_src, os.path.join(output_nested_sce_sys, "sce_icon0png1"))
    else:
        with open(os.path.join(output_sce_sys, "sce_icon0png1"), 'wb') as f:
            f.write(b'\x00' * 116736)
        with open(os.path.join(output_nested_sce_sys, "sce_icon0png1"), 'wb') as f:
            f.write(b'\x00' * 116736)

    # Save generated icon0.png
    icon0_path = os.path.join(output_sce_sys, "icon0.png")
    icon0_img.save(icon0_path)
    icon0_img.save(os.path.join(output_nested_sce_sys, "icon0.png"))

    # 9. Patch param.sfo
    template_sfo_path = os.path.join(template_sce_sys, "param.sfo")
    with open(template_sfo_path, 'rb') as f:
        sfo = ParamSfo(f.read())

    filename = os.path.basename(input_ess_path)
    parts = filename.split('_')
    char_id = "D29E9CC8"
    if len(parts) >= 2 and len(parts[1]) == 8:
        char_id = parts[1]

    char_hex = pc_save.player_name.encode('utf-8').hex().upper()
    loc_hex = pc_save.player_location.encode('utf-8').hex().upper()

    # Determine title based on save type
    if filename.startswith("Autosave"):
        # e.g. Autosave1 -> Autosave 1
        num_str = filename[8:9]
        maintitle_str = f"Autosave {num_str} - {pc_save.player_name} - {pc_save.player_location}"
        detail_prefix = f"Auto{num_str}"
    elif filename.startswith("Quicksave"):
        maintitle_str = f"Quicksave - {pc_save.player_name} - {pc_save.player_location}"
        detail_prefix = "Quick"
    else:
        maintitle_str = f"Save {pc_save.save_num} - {pc_save.player_name} - {pc_save.player_location}"
        detail_prefix = f"Save{pc_save.save_num}"

    detail_str = f"{detail_prefix}_{char_id}_0_{char_hex}_{loc_hex}_{pc_save.game_date}_{pc_save.player_level}_2"
    subtitle_str = pc_save.game_date
    blocks = max(320, (len(ps4_savedata) + 32767) // 32768 + 64)

    sfo.set_string("SAVEDATA_DIRECTORY", save_dir_name)
    sfo.set_string("DETAIL", detail_str)
    sfo.set_string("MAINTITLE", maintitle_str)
    sfo.set_string("SUBTITLE", subtitle_str)
    sfo.set_blocks(blocks)

    sfo_bytes = sfo.to_bytes()
    with open(os.path.join(output_sce_sys, "param.sfo"), 'wb') as f:
        f.write(sfo_bytes)
    with open(os.path.join(output_nested_sce_sys, "param.sfo"), 'wb') as f:
        f.write(sfo_bytes)

    print(f"  Patched param.sfo:")
    print(f"    DIRECTORY: {save_dir_name}")
    print(f"    MAINTITLE: {maintitle_str}")
    print(f"    SUBTITLE:  {subtitle_str}")
    print(f"    DETAIL:    {detail_str}")
    print(f"    BLOCKS:    {blocks} (allocated ~{blocks * 32 // 1024} MB)")

    print(f"\n>>> Save conversion completed successfully!")
    print(f"Apollo Save Folder: {output_container_dir}")

    return output_container_dir

# ---------------------------------------------------------------------------
# Batch Conversion Helper
# ---------------------------------------------------------------------------
def convert_all_saves(
    save_dir: str = "Skyrim Special Edition/Saves",
    template_dir: str = "CUSA05486-SDU1_2026-09-11_191304",
    output_base_dir: str = "output",
    usb_path: str = None
):
    saves = sorted(glob.glob(os.path.join(save_dir, "*.ess")))
    if not saves:
        print(f"No .ess saves found in {save_dir}.")
        return []

    print(f"\nFound {len(saves)} saves in {save_dir}. Starting batch conversion...")

    converted_dirs = []
    # Slot mapping:
    # Save1..6 (hamza) -> SDU1..SDU6
    # Autosave1..3 -> SDA1..SDA3
    # Quicksave0 -> SDQ1
    # Prisoner -> SDU0
    for s in saves:
        fn = os.path.basename(s)
        if "Prisoner" in fn:
            slot = "SDU0"
        elif fn.startswith("Autosave"):
            num = fn[8:9]
            slot = f"SDA{num}"
        elif fn.startswith("Quicksave"):
            slot = "SDQ1"
        elif fn.startswith("Save"):
            # extract save num
            parts = fn.split('_')
            snum = parts[0].replace("Save", "")
            slot = f"SDU{snum}"
        else:
            slot = "SDU1"

        out_dir = convert_skyrim_save(
            input_ess_path=s,
            template_dir=template_dir,
            output_base_dir=output_base_dir,
            save_dir_name=slot,
            include_timestamp=False
        )
        converted_dirs.append((out_dir, slot))

    # Also if USB path is specified or mounted, copy all to USB
    if usb_path and os.path.isdir(usb_path):
        usb_apollo_dir = os.path.join(usb_path, "PS4", "APOLLO")
        os.makedirs(usb_apollo_dir, exist_ok=True)
        print(f"\nSyncing all converted saves directly to USB at: {usb_apollo_dir}...")
        for out_dir, slot in converted_dirs:
            folder_name = os.path.basename(out_dir)
            dst = os.path.join(usb_apollo_dir, folder_name)
            if os.path.exists(dst):
                shutil.rmtree(dst)
            # Copy container files directly into destination
            shutil.copytree(out_dir, dst)
            print(f"  Copied {folder_name} to USB [OK]")

        # Run sync
        try:
            os.sync()
            print("  Flushed filesystem buffers (sync) [OK]")
        except Exception:
            pass

    return converted_dirs

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def list_saves(save_dir: str):
    saves = sorted(glob.glob(os.path.join(save_dir, "*.ess")))
    print(f"\nAvailable PC Saves in '{save_dir}':")
    print(f"{'#':<3} {'Filename':<36} {'Character':<10} {'Lvl':<4} {'Date':<10} {'Location'}")
    print("-" * 80)
    for idx, s in enumerate(saves, 1):
        try:
            save = SkyrimSave(s)
            fn = os.path.basename(s)
            if len(fn) > 34:
                fn = fn[:31] + "..."
            print(f"{idx:<3} {fn:<36} {save.player_name:<10} {save.player_level:<4} {save.game_date:<10} {save.player_location}")
        except Exception as e:
            print(f"{idx:<3} {os.path.basename(s):<36} (Error parsing: {e})")
    print("-" * 80)
    return saves

def main():
    parser = argparse.ArgumentParser(description="Convert Skyrim PC .ess save to PS4 Apollo save format")
    parser.add_argument("-i", "--input", help="Path to input PC Skyrim .ess save file")
    parser.add_argument("-t", "--template", default="CUSA05486-SDU1_2026-09-11_191304",
                        help="Path to Apollo PS4 decrypted save dump directory (default: CUSA05486-SDU1_2026-09-11_191304)")
    parser.add_argument("-o", "--output", default="output", help="Base output directory (default: output)")
    parser.add_argument("-s", "--save-dir", default="SDU1", help="Target save directory slot name (default: SDU1)")
    parser.add_argument("-l", "--list", action="store_true", help="List available saves in PC saves directory and exit")
    parser.add_argument("-v", "--verify", action="store_true", help="Verify the converted save after conversion")
    parser.add_argument("--all", action="store_true", help="Convert all PC saves in 'Skyrim Special Edition/Saves'")
    parser.add_argument("--usb", nargs="?", const="/run/media/hamza/hamza",
                        help="Copy converted saves directly to USB drive (default: /run/media/hamza/hamza)")

    args = parser.parse_args()

    default_pc_dir = "Skyrim Special Edition/Saves"
    if args.list:
        list_saves(default_pc_dir)
        sys.exit(0)

    if args.all:
        convert_all_saves(
            save_dir=default_pc_dir,
            template_dir=args.template,
            output_base_dir=args.output,
            usb_path=args.usb
        )
        print("\nAll saves have been successfully converted!")
        sys.exit(0)

    input_save = args.input
    if not input_save:
        saves = sorted(glob.glob(os.path.join(default_pc_dir, "*.ess")))
        if not saves:
            print(f"Error: No .ess saves found in {default_pc_dir}. Please specify --input.")
            sys.exit(1)
        save6_candidates = [s for s in saves if "Save6" in s]
        if save6_candidates:
            input_save = save6_candidates[0]
        else:
            input_save = saves[-1]
        print(f"No input save specified. Auto-selected: {input_save}")

    out_folder = convert_skyrim_save(
        input_ess_path=input_save,
        template_dir=args.template,
        output_base_dir=args.output,
        save_dir_name=args.save_dir
    )

    if args.usb and os.path.isdir(args.usb):
        usb_apollo_dir = os.path.join(args.usb, "PS4", "APOLLO")
        os.makedirs(usb_apollo_dir, exist_ok=True)
        folder_name = os.path.basename(out_folder)
        dst = os.path.join(usb_apollo_dir, folder_name)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(out_folder, dst)
        os.sync()
        print(f"Directly copied to USB: {dst}")

    if args.verify:
        from verify_save import verify_apollo_save
        print("\n--- Running Automated Verification ---")
        verify_apollo_save(out_folder)

if __name__ == "__main__":
    main()
