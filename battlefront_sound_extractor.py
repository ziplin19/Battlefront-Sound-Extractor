#!/usr/bin/env python3
"""
Battlefront Sound Extractor
===========================

Extract audio from Star Wars: Battlefront (2004) and Star Wars: Battlefront II
(2005) PC .lvl and .bnk files, commonly found in
GameData\\data\\_LVL_PC\\Sound\\*.lvl and common.bnk.

Format notes (based on community documentation, including the BNK format
explanation by Dark_Phantom / SoundFMVextractor):

- Files are nested "ucfb" chunks from the Pandemic Zero Engine.
- Chunk names are FNV-1a hashes, with lowercase normalization.
- Sound banks contain SampleBank, Info, SampleInfo, and Data chunks.
- PC common.bnk files commonly contain PCM16 audio; .lvl sounds commonly use
  IMA ADPCM. File layouts can vary by game and build.
- Sound names may be hashed. An optional dictionary file can resolve known
  hashes to readable names.

The script uses only the Python standard library. It writes WAV output.
The interface and command-line messages are English-only.

Usage examples:
  python battlefront_sound_extractor.py                 # Open the GUI
  python battlefront_sound_extractor.py common.bnk      # Command line
  python battlefront_sound_extractor.py yav.lvl -o sounds_out --dict dictionary.txt
  python battlefront_sound_extractor.py "sound/*.lvl" --format adpcm --blocksize 2048
  python battlefront_sound_extractor.py mus1.lvl --raw  # Export raw payloads

Notes:
- --format auto selects PCM16 for .bnk and ADPCM for .lvl by heuristic.
- Stereo ADPCM channel data is interleaved during decoding.
- If decoding fails, --raw can preserve the source audio payload for analysis.
"""

import argparse
import fnmatch
import os
import struct
import sys
import wave
from typing import BinaryIO, Dict, List, Optional, Tuple


FNV_OFFSET = 2166136261
FNV_PRIME = 16777619
MASK32 = 0xFFFFFFFF

# ---------------------------------------------------------------- Hashes ---

def fnv1a(name: str) -> int:
    """Pandemic Munge hash: FNV-1a with '|= 0x20' (forces lowercase)."""
    h = FNV_OFFSET
    for ch in name:
        h = (h ^ (ord(ch) | 0x20)) & MASK32
        h = (h * FNV_PRIME) & MASK32
    return h

# Literal 4CC names (read as little-endian) and hashed names
CC_UCFB = struct.unpack("<I", b"ucfb")[0]          # 0x62666375
CC_LVL_ = struct.unpack("<I", b"lvl_")[0]         # 0x5F6C766C

KNOWN_CHUNKS = {
    CC_UCFB: "ucfb", CC_LVL_: "lvl_",
}
KNOWN_CHUNKS.update({
    fnv1a(n): n for n in (
        "SoundBankList", "SampleBank", "Info", "Name",
        "Format", "NumSamples", "Size", "Template", "SampleInfo", "Sample",
        "ID", "Frequency", "SizeSamples", "Padding", "Alias", "SampleEnd",
        "Data",
    )
})

H = {n: fnv1a(n) for n in (
    "SoundBankList", "SampleBank", "Info", "Name", "Format", "NumSamples",
    "Size", "Template", "SampleInfo", "Sample", "ID", "Frequency",
    "SizeSamples", "Padding", "Alias", "SampleEnd", "Data",
)}

# ------------------------------------------------------------ ADPCM ---------

IMA_STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41,
    45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190,
    209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724,
    796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272,
    2499, 2749, 3024, 3327, 3660, 4024, 4425, 4868, 5356, 5894, 6484, 7132,
    7847, 8632, 9495, 10444, 11487, 12635, 13899, 15289, 16818, 18500,
    20350, 22385, 24623, 27086, 29794, 32767,
)
IMA_INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)


def decode_ima_adpcm(data: bytes, blocksize: int = 0x800) -> List[int]:
    """Decode IMA ADPCM (block: int16le predictor, uint8 index, 1 reserved
    byte, followed by 4-bit nibbles) into PCM16 samples."""
    samples: List[int] = []
    pos = 0
    while pos + 4 <= len(data):
        predictor = struct.unpack_from("<h", data, pos)[0]
        index = data[pos + 2]
        if index > 88:
            index = 88
        pos += 4
        samples.append(predictor)  # Blockkopf-Sample zaehlt mit
        end = min(pos + blocksize - 4, len(data))
        cur = predictor
        while pos < end:
            b = data[pos]
            pos += 1
            for nib in (b & 0x0F, (b >> 4) & 0x0F):
                step = IMA_STEP_TABLE[index]
                diff = (step >> 3)
                if nib & 1: diff += step >> 2
                if nib & 2: diff += step >> 1
                if nib & 4: diff += step
                cur += -diff if nib & 8 else diff
                cur = max(-32768, min(32767, cur))
                index = max(0, min(88, index + IMA_INDEX_TABLE[nib]))
                samples.append(cur)
        # Align to the block boundary, then move to the next block header
        pos = end
        if blocksize > 4 and (pos % blocksize) != 0:
            pos += blocksize - (pos % blocksize)
        if pos > len(data):
            pos = len(data)
    return samples


def decode_ima_adpcm_channels(data: bytes, channels: int,
                              blocksize: int = 0x800) -> List[List[int]]:
    """Decode standard IMA WAV ADPCM blocks.

    SWBF PC samples are stored in blocks: 36 bytes for mono or
    72 bytes for stereo. For stereo, the two 4-byte channel headers
    appear at the start of each block, followed by alternating 4-byte groups per channel.
    """
    channels = 2 if channels == 2 else 1
    block_align = 36 * channels
    header_size = 4 * channels
    output: List[List[int]] = [[] for _ in range(channels)]

    def decode_nibble(nibble: int, predictor: int, index: int):
        step = IMA_STEP_TABLE[index]
        diff = step >> 3
        if nibble & 1:
            diff += step >> 2
        if nibble & 2:
            diff += step >> 1
        if nibble & 4:
            diff += step
        predictor += -diff if nibble & 8 else diff
        predictor = max(-32768, min(32767, predictor))
        index = max(0, min(88, index + IMA_INDEX_TABLE[nibble]))
        return predictor, index

    for block_start in range(0, len(data) - header_size + 1, block_align):
        block = data[block_start:block_start + block_align]
        if len(block) < header_size:
            break

        predictors = []
        indices = []
        for ch in range(channels):
            off = ch * 4
            predictor = struct.unpack_from("<h", block, off)[0]
            index = min(88, block[off + 2])
            predictors.append(predictor)
            indices.append(index)
            output[ch].append(predictor)

        payload_pos = header_size
        while payload_pos < len(block):
            for ch in range(channels):
                group = block[payload_pos:payload_pos + 4]
                payload_pos += 4
                for byte in group:
                    for nibble in (byte & 0x0F, (byte >> 4) & 0x0F):
                        predictors[ch], indices[ch] = decode_nibble(
                            nibble, predictors[ch], indices[ch])
                        output[ch].append(predictors[ch])
                if channels == 1:
                    break
    return output

# ------------------------------------------------------------ WAV ----------

def write_wav(path: str, channels: List[List[int]], rate: int) -> None:
    ch_count = len(channels)
    length = min(len(c) for c in channels)
    with wave.open(path, "wb") as w:
        w.setnchannels(ch_count)
        w.setsampwidth(2)
        w.setframerate(rate if rate > 0 else 22050)
        frames = bytearray()
        for i in range(length):
            for c in channels:
                frames += struct.pack("<h", c[i])
        w.writeframes(bytes(frames))

# ------------------------------------------------------------ Chunks -------

class Chunk:
    __slots__ = ("name_hash", "name", "size", "start", "data_start")

    def __init__(self, name_hash: int, size: int, start: int, data_start: int):
        self.name_hash = name_hash
        self.name = KNOWN_CHUNKS.get(name_hash, "hash_0x%08X" % name_hash)
        self.size = size
        self.start = start
        self.data_start = data_start


def read_chunk_header(f: BinaryIO) -> Optional[Chunk]:
    hdr = f.read(8)
    if len(hdr) < 8:
        return None
    name_hash, size = struct.unpack("<Ii", hdr)
    if size < 0:
        size = 0  # Handle malformed sizes defensively.
    return Chunk(name_hash, size, f.tell() - 8, f.tell())


def walk_chunks(f: BinaryIO, start: int, end: int,
                depth: int = 0, max_depth: int = 16) -> List[Chunk]:
    """Read chunks in [start, end), descending into ucfb/lvl_/
    and SoundBankList containers."""
    chunks: List[Chunk] = []
    pos = start
    f.seek(pos)
    while pos + 8 <= end:
        c = read_chunk_header(f)
        if c is None:
            break
        data_end = min(c.data_start + c.size, end)
        chunks.append(c)
        is_container = c.size > 8 and (
            c.name_hash in (H["SoundBankList"], CC_LVL_))
        if is_container and depth < max_depth:
            chunks.extend(
                walk_chunks(f, c.data_start, data_end, depth + 1, max_depth))
        elif c.name_hash == CC_UCFB and depth < max_depth:
            # Nested ucfb containers: their data consists of chunks.
            chunks.extend(
                walk_chunks(f, c.data_start, data_end, depth + 1, max_depth))
        pos = data_end
        # Align to 4 bytes.
        if pos % 4:
            pos += 4 - (pos % 4)
        f.seek(pos)
    return chunks


# ------------------------------------------------------------ Scan-Modus ---
# Approach follows the established SoundRipper (Dark_Phantom/BAD-AL): actual
# .lvl/.bnk files are read using hash scanning rather than structural parsing:
#   - "Size" tag (0x23A0D95C): first hit per bank = bank header
#     (sound count at i-4, channel count ushort at i-20); later hits =
#     sample entries (sample rate at i-4, size at i+4, name hash at i-12,
#     skip if u32 at i+0x14 == 0 or u32 at i+0x18 equals the alias hash).
#   - "Data" tag (0xD872E2A5): audio data consumed sequentially by samples.
#   - PC: common.bnk = PCM16; all others = IMA ADPCM with block sizes
#     of 36 (mono) or 72 (stereo) in the WAV header (as in the original).

ALIAS_HASH = 0x7D268157
COMMON_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)


def round_sample_rate(raw: int) -> int:
    if raw <= 0:
        return 22050
    return min(COMMON_RATES, key=lambda r: abs(r - raw))


def scan_all(data: bytes, needle: int) -> List[int]:
    """Return offsets of all occurrences of a 4-byte hash (tag positions)."""
    out: List[int] = []
    n = struct.pack("<I", needle)
    off = 0
    while True:
        i = data.find(n, off)
        if i < 0:
            return out
        out.append(i)
        off = i + 4


def write_wav_ima_header(path: str, payload: bytes, rate: int,
                         channels: int) -> None:
    """Write ADPCM payload data with an IMA ADPCM WAV header (fmt 17), as in
    the original ripper: blockAlign 36 (mono) / 72 (stereo), samplesPerBlock 64.
    Playable in VLC/ffmpeg without decoding the payload here."""
    channels = 2 if channels == 2 else 1
    block_align = 36 * channels
    samples_per_block = 65
    avg_bytes_per_sec = (rate * block_align) // samples_per_block
    fmt = struct.pack("<HHIIHHHH",
                      17, channels, rate, avg_bytes_per_sec,
                      block_align, 4, 2, samples_per_block)
    data_chunk = b"data" + struct.pack("<I", len(payload))
    riff_size = 4 + 8 + len(fmt) + len(data_chunk)
    with open(path, "wb") as o:
        o.write(b"RIFF" + struct.pack("<I", riff_size) + b"WAVE")
        o.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        o.write(data_chunk)
        o.write(payload)


def scan_extract(data: bytes, dictionary: Dict[int, str], out_dir: str,
                 raw: bool, fmt: str, is_bnk: bool) -> Tuple[int, int]:
    """Scan mode for real .lvl/.bnk files.
    fmt: 'auto' (.bnk=pcm16, .lvl=IMA ADPCM payload with WAV header),
         'pcm16', 'adpcm' (Dekodierung zu PCM16), raw=True erzwingt .raw.
    Returns (written files, detected channels)."""
    size_hits = scan_all(data, H["Size"])
    data_hits = scan_all(data, H["Data"])
    if not size_hits or not data_hits:
        return 0, 1

    # Collect data banks with sizes (tag, size, data offset)
    banks = []
    for pos in data_hits:
        if pos + 8 > len(data):
            continue
        dsize = struct.unpack_from("<I", data, pos + 4)[0]
        if 0 < dsize <= len(data) - pos - 8:
            banks.append((pos + 8, dsize))
    if not banks:
        return 0, 1

    # Collect samples via Size tags (state machine: bank header vs. sample)
    entries = []  # (name, rate, size)
    channels = 1
    remaining = 0
    for i in size_hits:
        if i + 0x1C > len(data):
            continue
        if remaining == 0:
            # Bank header: sound count at i-4, channel count ushort at i-20
            n = struct.unpack_from("<I", data, i - 4)[0] if i >= 4 else 0
            ch = struct.unpack_from("<H", data, i - 20)[0] if i >= 20 else 1
            if 0 < n < 10000:
                remaining = n
                channels = ch if ch in (1, 2) else 1
            continue
        remaining -= 1
        rate_raw = struct.unpack_from("<I", data, i - 4)[0]
        size = struct.unpack_from("<I", data, i + 4)[0]
        name_hash = struct.unpack_from("<I", data, i - 12)[0]
        padding = struct.unpack_from("<I", data, i + 0x14)[0]
        next_tag = struct.unpack_from("<I", data, i + 0x18)[0]
        # Padding is part of bank offsets and must not be used as an exclusion
        # criterion. Skip alias entries.
        if next_tag == ALIAS_HASH:
            continue
        if not (0 < size <= len(data)):
            continue
        entries.append((name_hash, round_sample_rate(rate_raw), size,
                        padding, channels))

    if not entries:
        return 0, channels

    os.makedirs(out_dir, exist_ok=True)
    written = 0
    bank_idx = 0
    bank_pos = 0
    for name_hash, rate, size, padding, sample_channels in entries:
        if bank_idx >= len(banks):
            break
        start, total = banks[bank_idx]
        if bank_pos + size > total:
            # Next data bank
            bank_idx += 1
            bank_pos = 0
            if bank_idx >= len(banks):
                break
            start, total = banks[bank_idx]
        payload = data[start + bank_pos:start + bank_pos + size]
        bank_pos += size + padding
        if len(payload) < size:
            break
        name = dictionary.get(name_hash) or ("0x%08X" % name_hash)
        base = os.path.join(out_dir, name)
        try:
            if raw:
                with open(base + ".raw", "wb") as o:
                    o.write(payload)
            elif fmt == "pcm16" or (fmt == "auto" and is_bnk):
                chans = []
                if sample_channels <= 1:
                    seg = payload
                    n = len(seg) // 2
                    chans = [list(struct.unpack("<%dh" % n,
                                                seg[:n * 2]))]
                else:
                    per = (len(payload) // 2 // sample_channels) * 2
                    for c in range(sample_channels):
                        seg = payload[c * per:(c + 1) * per]
                        n = len(seg) // 2
                        chans.append(list(struct.unpack(
                            "<%dh" % n, seg[:n * 2])))
                write_wav(base + ".wav", chans, rate)
            elif fmt == "adpcm":
                chans = decode_ima_adpcm_channels(
                    payload, sample_channels, 36 * sample_channels)
                write_wav(base + ".wav", chans, rate)
            else:
                # auto + .lvl: payload data with IMA ADPCM header (as in the original)
                write_wav_ima_header(base + ".wav", payload, rate,
                                     sample_channels)
            written += 1
        except (struct.error, wave.Error, OSError) as e:
            print("  " + '!! Error processing %s: %s' % (name, e))
    return written, channels


def find_sample_banks(f: BinaryIO, size: int) -> List[Chunk]:
    """Find all SampleBank chunks: try structured parsing first, then scan."""
    f.seek(0)
    magic = f.read(8)
    banks: List[Chunk] = []
    if len(magic) == 8 and magic[:4] == b"ucfb":
        try:
            for c in walk_chunks(f, 8, size):
                if c.name_hash == H["SampleBank"]:
                    banks.append(c)
        except (struct.error, OSError):
            banks = []
    if banks:
        return banks
    # Fallback: raw hash search (level files with nested structures)
    f.seek(0)
    data = f.read()
    needle = struct.pack("<I", H["SampleBank"])
    off = 0
    while True:
        idx = data.find(needle, off)
        if idx < 0:
            break
        size_val = struct.unpack_from("<i", data, idx + 4)[0]
        if 0 < size_val <= len(data) - idx - 8:
            banks.append(Chunk(H["SampleBank"], size_val, idx, idx + 8))
        off = idx + 4
    return banks

# ------------------------------------------------------------ Bank-Parsing -

class Sample:
    __slots__ = ("name_hash", "name", "frequency", "size", "size_samples",
                 "padding", "alias")

    def __init__(self):
        self.name_hash = 0
        self.name = None
        self.frequency = 0
        self.size = 0
        self.size_samples = 0
        self.padding = 0
        self.alias = 0


class SoundBank:
    def __init__(self):
        self.name_hash = 0
        self.name = None
        self.channels = 1
        self.num_samples = 0
        self.data_size = 0
        self.samples: List[Sample] = []
        self.data_offset = 0


def read_info(buf: bytes, bank: SoundBank, dictionary: Dict[int, str]) -> None:
    """Parse the Info chunk: field chunks with 4-byte payloads, SampleInfo
    as a container with sample subchunks (recursive; current state is
    carried through recursive calls)."""

    def apply_tag(tag: int, val: int, cur: Optional[Sample],
                  bank: SoundBank) -> Optional[Sample]:
        if tag == H["Name"]:
            bank.name_hash = val
            bank.name = dictionary.get(val)
        elif tag == H["Format"]:
            bank.channels = val if val in (1, 2) else 1
        elif tag == H["NumSamples"]:
            bank.num_samples = val
        elif tag == H["Size"]:
            # The tag occurs at both bank and sample level.
            if cur is not None:
                cur.size = val
            else:
                bank.data_size = val
        elif tag == H["Sample"]:
            cur = Sample()
            bank.samples.append(cur)
        elif tag == H["ID"]:
            if cur is not None:
                cur.name_hash = val
                cur.name = dictionary.get(val)
        elif tag == H["Frequency"]:
            if cur is not None:
                cur.frequency = val
        elif tag == H["SizeSamples"]:
            if cur is not None:
                cur.size_samples = val
        elif tag == H["Padding"]:
            if cur is not None:
                cur.padding = val
        elif tag == H["Alias"]:
            if cur is not None:
                cur.alias = val
        elif tag == H["SampleEnd"]:
            cur = None
        return cur

    def parse(buf: bytes, cur: Optional[Sample]) -> Optional[Sample]:
        pos = 0
        while pos + 8 <= len(buf):
            tag, size = struct.unpack_from("<Ii", buf, pos)
            pos += 8
            if tag == H["SampleInfo"] and size > 0:
                cur = parse(buf[pos:pos + size], cur)
            elif tag == H["Sample"]:
                cur = apply_tag(tag, 0, cur, bank)
            elif size >= 4:
                cur = apply_tag(tag, struct.unpack_from("<I", buf, pos)[0],
                                cur, bank)
            pos += size
            if pos % 4:
                pos += 4 - (pos % 4)
        return cur

    parse(buf, None)


def parse_bank(f: BinaryIO, chunk: Chunk, dictionary: Dict[int, str],
               base: int) -> Optional[SoundBank]:
    """Parse a SampleBank chunk; base is the chunk file offset."""
    f.seek(chunk.data_start)
    body = f.read(chunk.size)
    bank = SoundBank()
    bank.name_hash = 0
    pos = 0
    data_found = False
    while pos + 8 <= len(body):
        tag, size = struct.unpack_from("<Ii", body, pos)
        pos += 8
        payload_start = pos
        if tag == H["Info"]:
            read_info(body[payload_start:payload_start + size], bank,
                      dictionary)
        elif tag == H["Data"]:
            bank.data_offset = chunk.data_start + payload_start
            data_found = True
        pos = payload_start + size
        if pos % 4:
            pos += 4 - (pos % 4)
    if not data_found or not bank.samples:
        return None
    return bank

# ------------------------------------------------------------ Extraction ---

def extract_bank(f: BinaryIO, bank: SoundBank, out_dir: str,
                 audio_format: str, blocksize: int, raw: bool,
                 prefix: str) -> List[str]:
    written: List[str] = []
    offset = bank.data_offset
    for idx, s in enumerate(bank.samples):
        size = s.size or s.size_samples
        if size <= 0 or offset + size > 10**12:
            continue
        f.seek(offset)
        data = f.read(size)
        if len(data) < size:
            break
        name = s.name or ("0x%08X" % s.name_hash if s.name_hash
                          else "sample_%03d" % idx)
        base_name = "%s%s" % (prefix, name)
        path = os.path.join(out_dir, base_name)
        fmt = audio_format
        if fmt == "auto":
            fmt = "pcm16"
        try:
            if raw or (fmt not in ("pcm16", "adpcm")):
                with open(path + ".raw", "wb") as o:
                    o.write(data)
                written.append(base_name + ".raw")
            elif fmt == "pcm16":
                ch = bank.channels
                per = len(data) // ch // 2 * 2
                chans = []
                for i in range(ch):
                    seg = data[i * per:(i + 1) * per] if ch > 1 else data
                    vals = struct.unpack("<%dh" % (len(seg) // 2), seg)
                    chans.append(list(vals))
                write_wav(path + ".wav", chans,
                          s.frequency or 22050)
                written.append(base_name + ".wav")
            else:  # adpcm
                chans = decode_ima_adpcm_channels(data, bank.channels,
                                                  blocksize)
                write_wav(path + ".wav", chans, s.frequency or 22050)
                written.append(base_name + ".wav")
        except (struct.error, wave.Error, OSError) as e:
            print("  " + '!! Error processing %s: %s' % (base_name, e))
        offset += size + s.padding
    return written


def load_dictionary(path: str) -> Dict[int, str]:
    d: Dict[int, str] = {}
    with open(path, "r", errors="ignore") as f:
        for line in f:
            name = line.strip()
            if name and not name.startswith("#"):
                d.setdefault(fnv1a(name), name)
    return d

# ------------------------------------------------------------ Main ---------

def process_file(path: str, args, dictionary: Dict[int, str]) -> int:
    print("== %s" % os.path.basename(path))
    try:
        with open(path, "rb") as f:
            f.seek(0)
            data = f.read()
        file_size = len(data)
        # Create a folder named after the source file by default.
        # (without the .lvl/.bnk extension).
        out_dir = args.outdir or os.path.join(
            os.path.dirname(os.path.abspath(path)),
            os.path.splitext(os.path.basename(path))[0])

        # --- Path 1: scan mode (tested in real .lvl/.bnk files) ---
        written, channels = scan_extract(
            data, dictionary, out_dir, args.raw, args.format,
            path.lower().endswith(".bnk"))
        if written:
            print("  " + 'Scan mode: %d sound files in %s'
                  % (written, out_dir))
            return written

        # --- Path 2: structured SampleBank parsing ---
        import io
        with io.BytesIO(data) as f:
            banks = find_sample_banks(f, file_size)
        total = 0
        if banks:
            fmt = args.format
            if fmt == "auto":
                fmt = "pcm16" if path.lower().endswith(".bnk") else "adpcm"
            os.makedirs(out_dir, exist_ok=True)
            with open(path, "rb") as f:
                for bi, chunk in enumerate(banks):
                    bank = parse_bank(f, chunk, dictionary, chunk.data_start)
                    if bank is None:
                        continue
                    bank_name = bank.name or ("bank_%d" % bi)
                    prefix = "%s__" % bank_name if len(banks) > 1 else ""
                    res = extract_bank(f, bank, out_dir, fmt,
                                       args.blocksize, args.raw, prefix)
                    total += len(res)
                    print("  " + "Bank '%s' (%s, %d channel(s)): %d sounds"
                          % (bank_name, fmt, bank.channels, len(res)))
            if total:
                print("  " + '-> %d files in %s' % (total, out_dir))
                return total

        # --- Nothing found: diagnostics ---
        print("  " + 'No sounds found')
        print("  Magic: %r" % data[:4])
        for name, h in (
            ("SampleBank", H["SampleBank"]),
            ("SoundBankList", H["SoundBankList"]),
            ("Size", H["Size"]),
            ("Data", H["Data"]),
        ):
            print("    %-14s: %dx" % (name, len(scan_all(data, h))))
        return 0
    except OSError as e:
        print("  " + '!! %s' % e)
        return 0


# ------------------------------------------------------------ GUI ---------

def run_gui() -> int:
    """Open the Tkinter GUI to select input, output, and extraction options."""
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title('Battlefront Sound Extractor (.lvl / .bnk)')
    root.resizable(True, True)

    # Optional project banner, stored beside the script.
    banner_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "Soundtrooper01.png")
    banner_image = None
    if os.path.isfile(banner_path):
        try:
            from PIL import Image, ImageTk
            image = Image.open(banner_path).convert("RGB")
            max_width = 1100
            if image.width > max_width:
                new_height = round(image.height * max_width / image.width)
                image = image.resize((max_width, new_height), Image.Resampling.LANCZOS)
            banner_image = ImageTk.PhotoImage(image)
        except ImportError:
            try:
                banner_image = tk.PhotoImage(file=banner_path)
                factor = max(1, (banner_image.width() + 1099) // 1100)
                if factor > 1:
                    banner_image = banner_image.subsample(factor, factor)
            except tk.TclError:
                banner_image = None
        except Exception:
            banner_image = None
    if banner_image is not None:
        banner = ttk.Label(root, image=banner_image, anchor="center")
        banner.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 0))
        root.banner_image = banner_image

    vars = {
        "input": tk.StringVar(),
        "outdir": tk.StringVar(),
        "format": tk.StringVar(value="auto"),
        "blocksize": tk.StringVar(value="2048"),
        "raw": tk.BooleanVar(value=False),
        "dict": tk.StringVar(),
    }

    frm = ttk.Frame(root, padding=10)
    frm.grid(row=1, column=0, sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)
    for i in range(3):
        frm.columnconfigure(1, weight=1)

    def browse_input():
        path = filedialog.askopenfilename(
            title='Choose .lvl/.bnk file',
            filetypes=[('Battlefront sound files', "*.lvl *.bnk"), ('All files', "*.*")])
        if path:
            vars["input"].set(path)
            if not vars["outdir"].get():
                vars["outdir"].set(
                    os.path.join(os.path.dirname(os.path.abspath(path)),
                                 os.path.splitext(os.path.basename(path))[0]))

    def browse_outdir():
        path = filedialog.askdirectory(title='Choose output folder')
        if path:
            vars["outdir"].set(path)

    def browse_dict():
        path = filedialog.askopenfilename(
            title='Choose dictionary',
            filetypes=[('Text files', "*.txt"), ('All files', "*.*")])
        if path:
            vars["dict"].set(path)

    ttk.Label(frm, text='Input file (.lvl / .bnk):').grid(
        row=0, column=0, sticky="w", pady=2)
    ttk.Entry(frm, textvariable=vars["input"]).grid(
        row=0, column=1, sticky="ew", padx=4)
    ttk.Button(frm, text='Browse...', command=browse_input).grid(
        row=0, column=2, padx=2)

    ttk.Label(frm, text='Output folder:').grid(
        row=1, column=0, sticky="w", pady=2)
    ttk.Entry(frm, textvariable=vars["outdir"]).grid(
        row=1, column=1, sticky="ew", padx=4)
    ttk.Button(frm, text='Browse...', command=browse_outdir).grid(
        row=1, column=2, padx=2)

    ttk.Label(frm, text='Audio format:').grid(
        row=2, column=0, sticky="w", pady=2)
    ttk.Combobox(frm, textvariable=vars["format"], state="readonly",
                 values=["auto", "pcm16", "adpcm"], width=12).grid(
        row=2, column=1, sticky="w", padx=4)

    ttk.Label(frm, text='ADPCM block size:').grid(
        row=3, column=0, sticky="w", pady=2)
    ttk.Spinbox(frm, from_=64, to=65536, increment=64, width=8,
                textvariable=vars["blocksize"]).grid(
        row=3, column=1, sticky="w", padx=4)
    ttk.Checkbutton(
        frm, text='Raw data (.raw, no decoding)',
        variable=vars["raw"]).grid(row=4, column=0, columnspan=2,
                                   sticky="w", pady=2)

    ttk.Label(frm, text='Dictionary (optional, readable names):').grid(
        row=5, column=0, sticky="w", pady=2)
    ttk.Entry(frm, textvariable=vars["dict"]).grid(
        row=5, column=1, sticky="ew", padx=4)
    ttk.Button(frm, text='Browse...', command=browse_dict).grid(
        row=5, column=2, padx=2)

    log = tk.Text(frm, height=12, width=70, state="disabled")
    log.grid(row=7, column=0, columnspan=3, sticky="nsew", pady=(8, 4))
    frm.rowconfigure(7, weight=1)
    scroll = ttk.Scrollbar(frm, command=log.yview)
    scroll.grid(row=7, column=3, sticky="ns")
    log.configure(yscrollcommand=scroll.set)
    frm.columnconfigure(3, weight=0)

    def log_write(msg: str) -> None:
        log.configure(state="normal")
        log.insert("end", msg + "\n")
        log.see("end")
        log.configure(state="disabled")

    busy = {"running": False}

    def do_extract():
        if busy["running"]:
            return
        path = vars["input"].get().strip()
        outdir = vars["outdir"].get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror(
                'Error', 'Please select a valid .lvl/.bnk file.')
            return
        try:
            blocksize = int(vars["blocksize"].get())
        except ValueError:
            blocksize = 2048
        dictionary: Dict[int, str] = {}
        dict_path = vars["dict"].get().strip()
        if dict_path and os.path.isfile(dict_path):
            try:
                dictionary = load_dictionary(dict_path)
                log_write('Dictionary: loaded %d names' % len(dictionary))
            except OSError as e:
                log_write('!! Could not read dictionary: %s' % e)

        class _Args:
            pass
        a = _Args()
        a.outdir = outdir or None
        a.format = vars["format"].get()
        a.blocksize = blocksize
        a.raw = vars["raw"].get()

        busy["running"] = True
        btn.configure(state="disabled")
        log_write("== " + 'Starting extraction: %s' % os.path.basename(path))

        def worker():
            try:
                total = process_file(path, a, dictionary)
                root.after(0, lambda: log_write(
                    'Done: %d sound files.' % total))
                root.after(0, lambda: messagebox.showinfo(
                    'Done', 'Extracted %d sound files.' % total))
            except Exception as e:  # noqa: BLE001 - keep the GUI running.
                root.after(0, lambda e=e: log_write('!! Error: %r' % e))
            finally:
                root.after(0, lambda: busy.update(running=False))
                root.after(0, lambda: btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    btn = ttk.Button(frm, text='Start extraction',
                     command=do_extract)
    btn.grid(row=6, column=0, columnspan=3, pady=(8, 0), sticky="ew")

    # Redirect GUI print output to the log window
    import io

    class _LogStream(io.StringIO):
        def write(self, s):  # type: ignore[override]
            if s and s.strip():
                root.after(0, lambda s=s: log_write(s.rstrip()))
            return len(s)

        def flush(self):  # type: ignore[override]
            pass

    sys.stdout = _LogStream()

    root.mainloop()
    return 0


def main(argv=None) -> int:
    # With no arguments, open the graphical interface
    if argv is None and len(sys.argv) == 1:
        return run_gui()
    ap = argparse.ArgumentParser(
        description='Extract sounds from Star Wars: Battlefront (2004) and Battlefront II (2005) .lvl and .bnk files.')
    ap.add_argument("input", nargs="+",
                    help='.lvl or .bnk file(s); wildcards are supported')
    ap.add_argument("-o", "--outdir", default=None,
                    help='Output folder (default: <filename>/)')
    ap.add_argument("--format", choices=("auto", "pcm16", "adpcm"),
                    default="auto",
                    help='auto: .bnk=pcm16, .lvl=adpcm (default)')
    ap.add_argument("--blocksize", type=int, default=0x800,
                    help='ADPCM block size in bytes (default: 2048)')
    ap.add_argument("--raw", action="store_true",
                    help='Write raw data without decoding (.raw)')
    ap.add_argument("--dict", dest="dictionary", default=None,
                    help='Dictionary file (one name per line) to resolve readable names')
    args = ap.parse_args(argv)
    if args.blocksize < 8:
        ap.error("The ADPCM block size must be at least 8 bytes.")

    dictionary: Dict[int, str] = {}
    if args.dictionary:
        try:
            dictionary = load_dictionary(args.dictionary)
            print('Dictionary: loaded %d names' % len(dictionary))
        except OSError as e:
            print('!! Could not read dictionary: %s' % e)

    files: List[str] = []
    for pattern in args.input:
        matched = fnmatch.filter([pattern], pattern) if os.path.exists(pattern) else []
        if matched:
            files.append(pattern)
        else:
            import glob
            files.extend(sorted(glob.glob(pattern)))
    if not files:
        print('No input files found.')
        return 1

    grand = 0
    for path in files:
        grand += process_file(path, args, dictionary)
    print("\n" + 'Finished: extracted %d sound files.' % grand)
    return 0


if __name__ == "__main__":
    sys.exit(main())