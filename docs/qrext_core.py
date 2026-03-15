"""
QRExtended Web Core
Browser-adapted port of qrext_create + qrext_scan.
Works entirely with bytes/bytearray buffers — no file I/O, no mmap, no tempfile.
Designed to run inside Pyodide (Python in WebAssembly).
"""

import hashlib
import zlib
import math
import io
import numpy as np
from PIL import Image
import qrcode

# ── Constants (must match desktop version exactly) ────────────────────────────
GAP             = 1
METADATA_LINES  = 10
QR_VERSION      = 2
CHUNK_SIZE      = 4 * 1024 * 1024   # 4 MB

SAMPLED_CHECKSUM_THRESHOLD = 250 * 1024 * 1024
SAMPLED_CHECKSUM_EDGE      = 100 * 1024 * 1024
SAMPLED_CHECKSUM_INTERVAL  = 500 * 1024 * 1024
SAMPLED_CHECKSUM_CHUNK     =   1 * 1024 * 1024

STUB_TEXT = (
    "This is a QR+ code, you have to run it through the QR+ scanner. "
    "https://github.com/ValbombMLG/QRExtended"
)

Image.MAX_IMAGE_PIXELS = None

_STUB_QR_CACHE = None


# ── Stub QR ───────────────────────────────────────────────────────────────────

def generate_stub_qr():
    global _STUB_QR_CACHE
    if _STUB_QR_CACHE is None:
        qr = qrcode.QRCode(
            version=3,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,
            border=0
        )
        qr.add_data(STUB_TEXT)
        qr.make(fit=True)
        _STUB_QR_CACHE = qr.make_image(fill_color="black", back_color="white").convert("1")
    return _STUB_QR_CACHE.copy()


# ── Checksum ──────────────────────────────────────────────────────────────────

def _sampled_regions(data_len):
    """Return (offset, length) pairs for sampled checksum."""
    edge     = SAMPLED_CHECKSUM_EDGE
    interval = SAMPLED_CHECKSUM_INTERVAL
    chunk    = SAMPLED_CHECKSUM_CHUNK
    regions  = []

    head_end   = min(edge, data_len)
    regions.append((0, head_end))

    tail_start = max(0, data_len - edge)
    pos = interval
    while pos < tail_start:
        sample_end = min(pos + chunk, tail_start)
        if sample_end > pos:
            regions.append((pos, sample_end - pos))
        pos += interval

    if tail_start > head_end:
        regions.append((tail_start, data_len - tail_start))

    return regions


def compute_checksum(data: bytes) -> bytes:
    """Full SHA256."""
    return hashlib.sha256(data).digest()


def compute_checksum_sampled(data: bytes, progress_cb=None) -> bytes:
    """
    Sampled SHA256 for large buffers.
    Files < 250 MB: full hash.
    Files >= 250 MB: head + mid samples + tail.
    """
    sha = hashlib.sha256()
    n   = len(data)

    if n < SAMPLED_CHECKSUM_THRESHOLD:
        sha.update(data)
        if progress_cb:
            progress_cb(1.0, "Checksum complete")
    else:
        regions     = _sampled_regions(n)
        total_bytes = sum(l for _, l in regions)
        done        = 0
        for offset, length in regions:
            sha.update(data[offset:offset + length])
            done += length
            if progress_cb:
                progress_cb(done / total_bytes, f"Checksumming… {int(done/total_bytes*100)}%")

    return sha.digest()


# ── Header encode / decode ────────────────────────────────────────────────────

def encode_header(filename: str, file_type: int, file_size: int,
                  part_num: int = 0, total_parts: int = 1) -> bytes:
    fname_bytes = filename.encode("utf-8")
    fname_len   = len(fname_bytes)
    if fname_len > 65535:
        raise ValueError("Filename too long")
    h = bytearray()
    h.extend(fname_len.to_bytes(2, 'big'))
    h.extend(fname_bytes)
    h.extend(file_type.to_bytes(1, 'big'))
    h.extend(file_size.to_bytes(8, 'big'))   # 64-bit
    h.extend(part_num.to_bytes(2, 'big'))
    h.extend(total_parts.to_bytes(2, 'big'))
    return bytes(h)


def decode_header(data: bytes):
    """Returns (filename, file_type, file_size, part_num, total_parts, remaining)."""
    idx      = 0
    fname_len = int.from_bytes(data[idx:idx+2], 'big'); idx += 2
    filename  = data[idx:idx+fname_len].decode('utf-8', errors='replace'); idx += fname_len
    file_type = data[idx]; idx += 1
    file_size = int.from_bytes(data[idx:idx+8], 'big'); idx += 8
    part_num  = int.from_bytes(data[idx:idx+2], 'big'); idx += 2
    total_parts = int.from_bytes(data[idx:idx+2], 'big'); idx += 2
    return filename, file_type, file_size, part_num, total_parts, data[idx:]


def header_size_for(filename: str) -> int:
    return 2 + len(filename.encode('utf-8')) + 1 + 8 + 2 + 2


# ── Canvas / metadata ─────────────────────────────────────────────────────────

def compute_canvas_size(total_bytes, stub_w, stub_h):
    stub_area = (stub_w + 2*GAP) * (stub_h + 2*GAP)
    meta_area = (stub_w + 2*GAP) * METADATA_LINES
    reserved  = stub_area + meta_area
    N = math.ceil(math.sqrt(total_bytes + reserved))
    N = max(N, GAP + stub_h + GAP + METADATA_LINES)
    N = max(N, stub_w + 2*GAP)
    return N


def create_metadata_v2(canvas_size, data_byte_count, compression_flag, original_size,
                       checksum_original, autorun_flag, part_num, total_parts, meta_width):
    bits  = f"{QR_VERSION:08b}"
    bits += f"{canvas_size:032b}"
    bits += f"{data_byte_count:064b}"
    bits += f"{compression_flag:08b}"
    bits += f"{original_size:064b}"
    for b in checksum_original:
        bits += f"{b:08b}"
    bits += f"{autorun_flag:08b}"
    bits += f"{part_num:016b}"
    bits += f"{total_parts:016b}"

    total_bits = meta_width * METADATA_LINES * 8
    if len(bits) > total_bits:
        raise ValueError("Metadata too large")
    bits += "0" * (total_bits - len(bits))

    meta_bytes = bytearray(int(bits[i:i+8], 2) for i in range(0, len(bits), 8))
    arr = np.zeros((METADATA_LINES, meta_width), dtype=np.uint8)
    for row in range(METADATA_LINES):
        row_data = meta_bytes[row*meta_width:(row+1)*meta_width]
        if row % 2 == 1:
            row_data = row_data[::-1]
        arr[row, :len(row_data)] = list(row_data)
    return arr


def read_metadata_v2(img, stub_w, stub_h):
    meta_y0    = GAP + stub_h + GAP
    meta_width = stub_w + 2*GAP
    meta_img   = img.crop((GAP, meta_y0, GAP + meta_width, meta_y0 + METADATA_LINES))
    arr        = np.array(meta_img.convert('L'), dtype=np.uint8)
    meta_img.close()

    raw = bytearray()
    for row_idx in range(METADATA_LINES):
        row = arr[row_idx, :].tolist()
        if row_idx % 2 == 1:
            row = row[::-1]
        raw.extend(row)

    idx = 0
    _version        = raw[idx]; idx += 1
    canvas_size     = int.from_bytes(raw[idx:idx+4], 'big'); idx += 4
    data_byte_count = int.from_bytes(raw[idx:idx+8], 'big'); idx += 8
    compression_flag= raw[idx]; idx += 1
    original_size   = int.from_bytes(raw[idx:idx+8], 'big'); idx += 8
    checksum        = bytes(raw[idx:idx+32]); idx += 32
    autorun_flag    = raw[idx]; idx += 1
    part_num        = int.from_bytes(raw[idx:idx+2], 'big'); idx += 2
    total_parts     = int.from_bytes(raw[idx:idx+2], 'big'); idx += 2

    return canvas_size, data_byte_count, compression_flag, original_size, checksum, autorun_flag, part_num, total_parts


# ── Snake pattern (vectorized) ────────────────────────────────────────────────

def _build_snake_indices(N, forbidden):
    """Return (rows, cols) flat arrays of writable pixel positions in snake order."""
    all_rows, all_cols = [], []
    for col_idx in range(N):
        col  = N - 1 - col_idx
        rows = (np.arange(N, dtype=np.int32) if col_idx % 2 == 0
                else np.arange(N-1, -1, -1, dtype=np.int32))
        all_rows.append(rows)
        all_cols.append(np.full(N, col, dtype=np.int32))
    rows_flat = np.concatenate(all_rows)
    cols_flat = np.concatenate(all_cols)
    mask = ~forbidden[rows_flat, cols_flat]
    return rows_flat[mask], cols_flat[mask]


# ── Encode ────────────────────────────────────────────────────────────────────

def encode(file_bytes: bytes, filename: str, allow_autorun: bool = False,
           progress_cb=None) -> bytes:
    """
    Encode file_bytes into a QR+ PNG and return the PNG as bytes.
    progress_cb(fraction: float, message: str)
    """
    if progress_cb:
        progress_cb(0.0, "Computing checksum…")

    checksum     = compute_checksum_sampled(file_bytes, progress_cb)
    original_size = len(file_bytes)

    if progress_cb:
        progress_cb(0.1, "Compressing…")

    compressed = zlib.compress(file_bytes, level=6)
    if len(compressed) < len(file_bytes):
        payload          = compressed
        compression_flag = 1
    else:
        payload          = file_bytes
        compression_flag = 0

    header   = encode_header(filename, 0, original_size, 0, 1)
    full_data = header + payload

    if progress_cb:
        progress_cb(0.2, "Building canvas…")

    stub_img  = generate_stub_qr()
    stub_w, stub_h = stub_img.size
    stub_array = np.where(np.array(stub_img, dtype=np.uint8) == 0, 0, 255).astype(np.uint8)
    stub_img.close()

    N          = compute_canvas_size(len(full_data), stub_w, stub_h)
    meta_width = stub_w + 2*GAP
    meta_y0    = GAP + stub_h + GAP

    meta_array = create_metadata_v2(
        N, len(full_data), compression_flag, original_size,
        checksum, 1 if allow_autorun else 0, 0, 1, meta_width
    )

    canvas = np.full((N, N), 255, dtype=np.uint8)
    canvas[GAP:GAP+stub_h, GAP:GAP+stub_w] = stub_array
    canvas[meta_y0:meta_y0+METADATA_LINES, GAP:GAP+meta_width] = meta_array

    forbidden = np.zeros((N, N), dtype=bool)
    forbidden[GAP:GAP+stub_h+GAP, GAP:GAP+stub_w+GAP] = True
    forbidden[meta_y0:meta_y0+METADATA_LINES, GAP:GAP+meta_width] = True

    if progress_cb:
        progress_cb(0.3, "Encoding pixels…")

    data_arr = np.frombuffer(full_data, dtype=np.uint8)
    dr, dc   = _build_snake_indices(N, forbidden)
    n_bytes  = len(data_arr)
    n_pixels = len(dr)

    canvas[dr[:n_bytes], dc[:n_bytes]] = data_arr
    if n_pixels > n_bytes:
        canvas[dr[n_bytes:], dc[n_bytes:]] = np.random.randint(
            0, 256, size=n_pixels - n_bytes, dtype=np.uint8)

    del forbidden, data_arr, dr, dc

    if progress_cb:
        progress_cb(0.9, "Saving PNG…")

    img = Image.fromarray(canvas, mode='L')
    del canvas
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=False, compress_level=1)
    img.close()
    buf.seek(0)

    if progress_cb:
        progress_cb(1.0, "Done!")

    return buf.read()


# ── Decode ────────────────────────────────────────────────────────────────────

def decode(png_bytes: bytes, skip_checksum: bool = False,
           progress_cb=None) -> tuple:
    """
    Decode a QR+ PNG from bytes.
    Returns (filename, file_bytes).
    Raises ValueError on corrupt/mismatched data.
    """
    if progress_cb:
        progress_cb(0.0, "Opening image…")

    img = Image.open(io.BytesIO(png_bytes))

    # Verify stub
    stub_img  = generate_stub_qr()
    stub_w, stub_h = stub_img.size
    stub_img.close()

    (canvas_size, data_byte_count, compression_flag, original_size,
     checksum_original, autorun_flag, part_num, total_parts) = read_metadata_v2(img, stub_w, stub_h)

    if total_parts > 1:
        raise ValueError("Multi-part QR+ images are not supported in the web version. Use the desktop app.")

    if progress_cb:
        progress_cb(0.1, "Reading pixel data…")

    N = canvas_size
    arr = np.array(img.convert('L'), dtype=np.uint8)
    img.close()

    forbidden = np.zeros((N, N), dtype=bool)
    meta_y0   = GAP + stub_h + GAP
    meta_width = stub_w + 2*GAP
    forbidden[GAP:GAP+stub_h+GAP, GAP:GAP+stub_w+GAP] = True
    forbidden[meta_y0:meta_y0+METADATA_LINES, GAP:GAP+meta_width] = True

    dr, dc   = _build_snake_indices(N, forbidden)
    raw_data = arr[dr[:data_byte_count], dc[:data_byte_count]].tobytes()
    del arr, forbidden, dr, dc

    if progress_cb:
        progress_cb(0.5, "Decoding header…")

    # Try to decode header
    try:
        filename, file_type, _, part_num, total_parts, _ = decode_header(raw_data)
        hsize = header_size_for(filename)
        payload = raw_data[hsize:]
        header_compressed = False
    except Exception:
        # Entire blob may be compressed
        if not compression_flag:
            raise ValueError("Cannot decode header and compression_flag=0. File may be corrupted.")
        decompressed = zlib.decompress(raw_data)
        filename, file_type, _, part_num, total_parts, _ = decode_header(decompressed)
        hsize   = header_size_for(filename)
        payload = decompressed[hsize:]
        header_compressed = True
        compression_flag  = 0  # already decompressed

    if progress_cb:
        progress_cb(0.6, "Decompressing…")

    if compression_flag and not header_compressed:
        payload = zlib.decompress(payload)

    if progress_cb:
        progress_cb(0.8, "Verifying checksum…")

    if not skip_checksum:
        actual = compute_checksum_sampled(payload, progress_cb)
        if actual != checksum_original:
            raise ValueError("Checksum mismatch — file may be corrupted.")

    if progress_cb:
        progress_cb(1.0, "Done!")

    return filename, payload


# ── Quick metadata peek (for preview screen) ─────────────────────────────────

def peek_metadata(png_bytes: bytes) -> dict:
    """
    Read just the metadata and filename from a QR+ PNG without decoding the full image.
    Returns a dict with keys: filename, original_size, compression_flag,
    autorun_flag, part_num, total_parts.
    """
    img = Image.open(io.BytesIO(png_bytes))
    stub_img = generate_stub_qr()
    stub_w, stub_h = stub_img.size
    stub_img.close()

    (canvas_size, data_byte_count, compression_flag, original_size,
     checksum_original, autorun_flag, part_num, total_parts) = read_metadata_v2(img, stub_w, stub_h)

    # Read just enough pixels to extract the header
    N   = canvas_size
    arr = np.array(img.convert('L'), dtype=np.uint8)
    img.close()

    forbidden  = np.zeros((N, N), dtype=bool)
    meta_y0    = GAP + stub_h + GAP
    meta_width = stub_w + 2*GAP
    forbidden[GAP:GAP+stub_h+GAP, GAP:GAP+stub_w+GAP] = True
    forbidden[meta_y0:meta_y0+METADATA_LINES, GAP:GAP+meta_width] = True

    dr, dc    = _build_snake_indices(N, forbidden)
    peek_size = min(1024, data_byte_count)
    raw_peek  = arr[dr[:peek_size], dc[:peek_size]].tobytes()
    del arr, forbidden, dr, dc

    filename = "Unknown"
    try:
        if compression_flag:
            try:
                raw_peek = zlib.decompress(raw_peek)
            except Exception:
                pass
        filename, *_ = decode_header(raw_peek)
    except Exception:
        pass

    return {
        "filename":         filename,
        "original_size":    original_size,
        "compression_flag": compression_flag,
        "autorun_flag":     autorun_flag,
        "part_num":         part_num,
        "total_parts":      total_parts,
    }
