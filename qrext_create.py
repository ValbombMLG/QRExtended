# qrext_create.py
"""
QR+ creator - Optimized with streaming, multi-part support, and smart compression.
OPTIMIZED: Parallel checksum+compression, smart compression levels, cached QR, fast PNG saves.
"""

import numpy as np
from PIL import Image
import qrcode
import hashlib
import zlib
import os
import math
import gc
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import tempfile

# Constants

def _dprint(*args, **kwargs):
    """Print only when DEBUG mode is active."""
    try:
        from qrext_qt import DEBUG
        if DEBUG:
            import builtins
            builtins.print(*args, **kwargs)
    except ImportError:
        pass  # Running standalone without UI

STUB_TEXT = (
    "This is a QR+ code, you have to run it through the QR+ scanner. "
    "https://github.com/ValbombMLG/QRExtended"
)
GAP = 1
METADATA_LINES = 10
QR_VERSION = 2
CHUNK_SIZE = 10 * 1024 * 1024  # 10MB chunks for streaming

# Disable PIL decompression bomb protection
Image.MAX_IMAGE_PIXELS = None

# Multi-threading settings
MAX_WORKER_THREADS = min(8, (os.cpu_count() or 1))  # Use up to 8 threads

# Cached stub QR (generated once, reused) - OPTIMIZATION
_STUB_QR_CACHE = None


def generate_stub_qr():
    """Generate the stub QR code for header. OPTIMIZED: Cached after first generation."""
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
    return _STUB_QR_CACHE.copy()  # Return a copy to avoid mutations


SAMPLED_CHECKSUM_THRESHOLD = 250 * 1024 * 1024  # 250 MB
SAMPLED_CHECKSUM_EDGE     = 100 * 1024 * 1024  # 100 MB head + 100 MB tail
SAMPLED_CHECKSUM_INTERVAL = 500 * 1024 * 1024  # 1 MB sample every 500 MB
SAMPLED_CHECKSUM_CHUNK    =   1 * 1024 * 1024  # 1 MB per mid-sample


def compute_checksum(data):
    """Compute SHA256 of data (bytes)."""
    return hashlib.sha256(data).digest()


def _sampled_checksum_regions(file_size):
    """
    Return a list of (offset, length) byte regions to hash for a sampled checksum.
    Regions are returned in ascending offset order and never overlap.
    Layout:
      - First 100 MB
      - 1 MB chunks at every 500 MB interval through the middle
      - Last 100 MB
    """
    edge = SAMPLED_CHECKSUM_EDGE
    interval = SAMPLED_CHECKSUM_INTERVAL
    chunk = SAMPLED_CHECKSUM_CHUNK

    regions = []

    # Head
    head_end = min(edge, file_size)
    regions.append((0, head_end))

    # Mid samples: at 500 MB, 1000 MB, 1500 MB ... stopping before tail region starts
    tail_start = max(0, file_size - edge)
    pos = interval
    while pos < tail_start:
        sample_end = min(pos + chunk, tail_start)
        sample_len = sample_end - pos
        if sample_len > 0:
            regions.append((pos, sample_len))
        pos += interval

    # Tail (only if file is large enough to have a distinct tail)
    if tail_start > head_end:
        regions.append((tail_start, file_size - tail_start))

    return regions


def compute_checksum_stream(filepath, progress_callback=None):
    """
    Compute checksum of file.
    - Files < 250 MB: full SHA256
    - Files >= 250 MB: sampled SHA256 (head + mid samples + tail)
    """
    file_size = os.path.getsize(filepath)
    sha256 = hashlib.sha256()

    if file_size < SAMPLED_CHECKSUM_THRESHOLD:
        # Full checksum
        bytes_read = 0
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                sha256.update(chunk)
                bytes_read += len(chunk)
                if progress_callback and file_size > 0:
                    progress_callback("checksum", bytes_read, file_size,
                                      f"Computing checksum: {int(bytes_read/file_size*100)}%")
    else:
        # Sampled checksum
        regions = _sampled_checksum_regions(file_size)
        total_sample = sum(l for _, l in regions)
        bytes_read = 0
        with open(filepath, "rb") as f:
            for offset, length in regions:
                f.seek(offset)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    sha256.update(chunk)
                    bytes_read += len(chunk)
                    remaining -= len(chunk)
                    if progress_callback and total_sample > 0:
                        progress_callback("checksum", bytes_read, total_sample,
                                          f"Computing checksum (sampled): {int(bytes_read/total_sample*100)}%")

    return sha256.digest()


def smart_compress_level(file_size):
    """Choose optimal compression level based on file size. OPTIMIZATION."""
    if file_size < 10 * 1024 * 1024:  # < 10MB
        return 9  # Best compression for small files
    elif file_size > 100 * 1024 * 1024:  # > 100MB
        return 3  # Fast compression for large files (often pre-compressed)
    else:
        return 6  # Balanced for medium files


def compute_checksum_and_compress_parallel(filepath, progress_callback=None):
    """
    Compute checksum AND compress in a single pass.
    For files >= 250 MB, uses sampled checksum to avoid a full second read.
    Returns: (checksum, compressed_file_or_original, data_size, compression_helped)
    """
    file_size = os.path.getsize(filepath)
    comp_level = smart_compress_level(file_size)
    compressor = zlib.compressobj(level=comp_level)
    _tmp_f = tempfile.NamedTemporaryFile(suffix=".tmp", delete=False)
    temp_compressed = _tmp_f.name
    _tmp_f.close()
    compressed_size = 0
    use_sampled = file_size >= SAMPLED_CHECKSUM_THRESHOLD

    if use_sampled:
        # Compress in one pass, then do sampled checksum on original file separately
        try:
            with open(filepath, 'rb') as f_in, open(temp_compressed, 'wb') as f_out:
                bytes_done = 0
                while True:
                    chunk = f_in.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    compressed_chunk = compressor.compress(chunk)
                    f_out.write(compressed_chunk)
                    compressed_size += len(compressed_chunk)
                    bytes_done += len(chunk)
                    if progress_callback and file_size > 0:
                        progress_callback("compress", bytes_done, file_size,
                                          f"Compressing: {int(bytes_done/file_size*100)}%")
                final_chunk = compressor.flush()
                f_out.write(final_chunk)
                compressed_size += len(final_chunk)

            # Sampled checksum on original file
            checksum = compute_checksum_stream(filepath, progress_callback)

            if compressed_size < file_size:
                return checksum, temp_compressed, compressed_size, True
            else:
                os.remove(temp_compressed)
                return checksum, filepath, file_size, False

        except Exception as e:
            if os.path.exists(temp_compressed):
                os.remove(temp_compressed)
            raise e
    else:
        # Small file: full checksum and compress in one pass
        sha256 = hashlib.sha256()
        try:
            with open(filepath, 'rb') as f_in, open(temp_compressed, 'wb') as f_out:
                while True:
                    chunk = f_in.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    sha256.update(chunk)
                    compressed_chunk = compressor.compress(chunk)
                    f_out.write(compressed_chunk)
                    compressed_size += len(compressed_chunk)
                final_chunk = compressor.flush()
                f_out.write(final_chunk)
                compressed_size += len(final_chunk)

            checksum = sha256.digest()

            if compressed_size < file_size:
                return checksum, temp_compressed, compressed_size, True
            else:
                os.remove(temp_compressed)
                return checksum, filepath, file_size, False

        except Exception as e:
            if os.path.exists(temp_compressed):
                os.remove(temp_compressed)
            raise e


def try_compress(data, file_size=None):
    """Try compressing data. OPTIMIZATION: Uses smart compression level."""
    try:
        original_size = len(data)
        level = smart_compress_level(file_size if file_size else original_size)
        compressed = zlib.compress(data, level=level)
        compressed_size = len(compressed)
        
        _dprint(f"DEBUG try_compress: original={original_size}, compressed={compressed_size}, level={level}")
        
        if compressed_size < original_size:
            _dprint(f"DEBUG try_compress: Compression HELPED ({original_size} -> {compressed_size})")
            return compressed, True
        _dprint(f"DEBUG try_compress: Compression did NOT help ({original_size} -> {compressed_size})")
        return data, False
    except Exception as e:
        _dprint(f"DEBUG try_compress: Exception {e}")
        return data, False


def encode_header(filename, file_type, file_size, part_num=0, total_parts=1):
    """
    Encode file header with multi-part support.
    Format: fname_len(16) + fname + type(8) + size(64) + part_num(16) + total_parts(16)
    """
    fname_bytes = filename.encode("utf-8")
    fname_len = len(fname_bytes)
    if fname_len > 65535:
        raise ValueError("Filename too long (max 65535 bytes)")
    
    header = bytearray()
    header.extend(fname_len.to_bytes(2, 'big'))
    header.extend(fname_bytes)
    header.extend(file_type.to_bytes(1, 'big'))
    header.extend(file_size.to_bytes(8, 'big'))  # 64-bit: supports files up to 16 EB
    header.extend(part_num.to_bytes(2, 'big'))
    header.extend(total_parts.to_bytes(2, 'big'))
    return bytes(header)


def compute_canvas_size(total_bytes, stub_w, stub_h, metadata_lines=METADATA_LINES, gap=GAP):
    """Compute square canvas size N for grayscale encoding."""
    stub_area = (stub_w + 2 * gap) * (stub_h + 2 * gap)
    meta_area = (stub_w + 2 * gap) * metadata_lines
    reserved = stub_area + meta_area
    
    total_pixels = total_bytes + reserved
    N = math.ceil(math.sqrt(total_pixels))
    
    min_height = gap + stub_h + gap + metadata_lines
    if N < min_height:
        N = min_height
    
    min_width = stub_w + 2 * gap
    if N < min_width:
        N = min_width
    
    return N


def create_metadata_v2(canvas_size, data_byte_count, compression_flag, original_size, 
                       checksum_original, autorun_flag, part_num, total_parts, meta_width, 
                       metadata_lines=METADATA_LINES):
    """Create metadata block for v2 format with multi-part info."""
    meta_bits = ""
    meta_bits += f"{QR_VERSION:08b}"
    meta_bits += f"{canvas_size:032b}"
    meta_bits += f"{data_byte_count:064b}"   # 64-bit: supports >4 GB encoded blobs
    meta_bits += f"{compression_flag:08b}"
    meta_bits += f"{original_size:064b}"     # 64-bit: supports >4 GB original files
    
    for byte in checksum_original:
        meta_bits += f"{byte:08b}"
    
    meta_bits += f"{autorun_flag:08b}"
    meta_bits += f"{part_num:016b}"
    meta_bits += f"{total_parts:016b}"
    
    total_bits = meta_width * metadata_lines * 8
    if len(meta_bits) > total_bits:
        raise ValueError("Metadata too large for allocated area")
    meta_bits += "0" * (total_bits - len(meta_bits))
    
    meta_bytes = bytearray()
    for i in range(0, len(meta_bits), 8):
        meta_bytes.append(int(meta_bits[i:i+8], 2))
    
    meta_array = np.zeros((metadata_lines, meta_width), dtype=np.uint8)
    idx = 0
    for row in range(metadata_lines):
        row_data = meta_bytes[idx:idx + meta_width]
        if row % 2 == 1:
            row_data = row_data[::-1]
        meta_array[row, :len(row_data)] = list(row_data)
        idx += meta_width
    
    return meta_array


def create_qr_plus(data, output_path, header_bytes, checksum_original, original_size, 
                   allow_autorun=True, part_num=0, total_parts=1, progress_callback=None, 
                   is_compressed=False, abort_check=None):
    """
    Create QR+ image with grayscale encoding and smart compression.
    
    Args:
        is_compressed: If True, data is already compressed (don't compress again).
                      If False, try to compress the data.
    """
    stub_img = generate_stub_qr()
    stub_w, stub_h = stub_img.size
    stub_array = np.array(stub_img, dtype=np.uint8)
    stub_array = np.where(stub_array == 0, 0, 255).astype(np.uint8)
    
    # is_compressed tells us if the data is ACTUALLY compressed
    if is_compressed:
        processed_data = data
        compressed = True
    else:
        processed_data, compressed = try_compress(data, len(data))
    
    full_data = header_bytes + processed_data
    data_byte_count = len(full_data)
    
    N = compute_canvas_size(data_byte_count, stub_w, stub_h)
    canvas = np.full((N, N), 255, dtype=np.uint8)
    
    canvas[GAP:GAP+stub_h, GAP:GAP+stub_w] = stub_array
    
    meta_width = stub_w + 2 * GAP
    meta_y0 = GAP + stub_h + GAP
    meta_array = create_metadata_v2(
        N, data_byte_count, 
        1 if compressed else 0,
        original_size,
        checksum_original,
        1 if allow_autorun else 0,
        part_num,
        total_parts,
        meta_width
    )
    canvas[meta_y0:meta_y0+METADATA_LINES, GAP:GAP+meta_width] = meta_array
    
    # Build snake traversal order as flat index arrays (vectorized, no Python loop)
    all_rows, all_cols = [], []
    for col_idx in range(N):
        col = N - 1 - col_idx
        rows = np.arange(N, dtype=np.int32) if (col_idx % 2 == 0) else np.arange(N-1, -1, -1, dtype=np.int32)
        all_rows.append(rows)
        all_cols.append(np.full(N, col, dtype=np.int32))
    rows_flat = np.concatenate(all_rows)
    cols_flat = np.concatenate(all_cols)

    # Build forbidden mask and filter snake order
    forbidden = np.zeros((N, N), dtype=bool)
    forbidden[GAP:GAP+stub_h+GAP, GAP:GAP+stub_w+GAP] = True
    forbidden[meta_y0:meta_y0+METADATA_LINES, GAP:GAP+meta_width] = True
    not_forbidden = ~forbidden[rows_flat, cols_flat]
    data_rows = rows_flat[not_forbidden]
    data_cols = cols_flat[not_forbidden]

    if abort_check and abort_check():
        raise InterruptedError("Operation aborted by user")

    # Write data in one vectorized assignment
    data_array = np.frombuffer(full_data, dtype=np.uint8)
    n_bytes = len(data_array)
    n_pixels = len(data_rows)

    canvas[data_rows[:n_bytes], data_cols[:n_bytes]] = data_array
    if n_pixels > n_bytes:
        canvas[data_rows[n_bytes:], data_cols[n_bytes:]] = np.random.randint(
            0, 256, size=n_pixels - n_bytes, dtype=np.uint8)

    if progress_callback:
        progress_callback("encode", N, N, "Saving image...")

    img = Image.fromarray(canvas, mode='L')
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    img.save(output_path, optimize=False, compress_level=1)
    
    return True


def create_qr_plus_from_file(filepath, output_path, allow_autorun=True, progress_callback=None, abort_check=None):
    """Create single QR+ from a file with true streaming for large files."""
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"Input file not found: {filepath}")
    
    file_size = os.path.getsize(filepath)
    filename = os.path.basename(filepath)
    
    _dprint("=" * 60)
    _dprint(f"DEBUG: Creating QR+ from file")
    _dprint(f"DEBUG: Input: {filepath}")
    _dprint(f"DEBUG: Output: {output_path}")
    _dprint(f"DEBUG: File size: {file_size:,} bytes ({file_size / (1024*1024):.2f} MB)")
    _dprint(f"DEBUG: Autorun: {allow_autorun}")
    _dprint("=" * 60)
    
    # For small files (<100MB), use the fast in-memory method
    if file_size < 100 * 1024 * 1024:
        if abort_check and abort_check():
            raise InterruptedError("Operation aborted by user")
        
        if progress_callback:
            progress_callback("checksum", 1, 10, "Loading file...")
        
        with open(filepath, "rb") as f:
            data = f.read()
        
        if abort_check and abort_check():
            raise InterruptedError("Operation aborted by user")
        
        if progress_callback:
            progress_callback("checksum", 5, 10, "Computing checksum...")
        
        original_size = len(data)
        checksum = compute_checksum(data)
        
        if abort_check and abort_check():
            raise InterruptedError("Operation aborted by user")
        
        if progress_callback:
            progress_callback("compress", 5, 10, "Compressing...")
        
        header = encode_header(filename, 0, original_size, 0, 1)
        
        if progress_callback:
            progress_callback("encode", 5, 10, "Encoding to QR+ image...")
        
        result = create_qr_plus(data, output_path, header, checksum, original_size, 
                               allow_autorun, 0, 1, progress_callback, False, abort_check)
        
        if progress_callback:
            progress_callback("done", 10, 10, "Complete!")
        
        return result
    
    # For large files (>=100MB), use streaming WITHOUT pre-loading
    temp_compressed = None
    
    _dprint("DEBUG: Using streaming method (large file)")
    
    try:
        if abort_check and abort_check():
            raise InterruptedError("Operation aborted by user")
        
        if progress_callback:
            progress_callback("checksum", 0, 1, "Computing checksum and compressing...")
        
        _dprint("DEBUG: Computing checksum and attempting compression in parallel...")
        
        # OPTIMIZATION: Do checksum AND compression in ONE pass!
        checksum, data_file, data_size, compression_helped = compute_checksum_and_compress_parallel(filepath)
        
        compression_flag = 1 if compression_helped else 0
        
        if compression_helped:
            temp_compressed = data_file
            _dprint(f"DEBUG: Compression HELPED. {file_size:,} → {data_size:,} bytes (using compressed)")
        else:
            _dprint(f"DEBUG: Compression DID NOT HELP. Using original file")
        
        _dprint(f"DEBUG: Checksum computed: {checksum.hex()}")
        
        if abort_check and abort_check():
            raise InterruptedError("Operation aborted by user")
        
        if progress_callback:
            progress_callback("encode", 2, 10, "Encoding to QR+ image...")
        
        _dprint("DEBUG: Encoding to QR+ image with streaming...")
        
        # Encode header
        header = encode_header(filename, 0, file_size, 0, 1)
        
        # Create QR+ with streaming
        result = create_qr_plus_streaming_simple(
            data_file, output_path, header, checksum, 
            file_size, allow_autorun, compression_flag, 
            progress_callback, abort_check
        )
        
        _dprint(f"DEBUG: QR+ created successfully!")
        _dprint("=" * 60)
        
        return result
        
    finally:
        # Cleanup temp file
        if temp_compressed is not None and os.path.exists(temp_compressed):
            try:
                os.remove(temp_compressed)
            except:
                pass
        gc.collect()


def create_qr_plus_streaming_simple(data_filepath, output_path, header_bytes, checksum_original,
                                    original_size, allow_autorun, compression_flag,
                                    progress_callback=None, abort_check=None):
    """
    Vectorized large-file encoder using mmap.
    Processes the canvas in column bands to keep RAM bounded regardless of file size.
    No strip temp files, no stitch step.
    """
    import mmap

    stub_img = generate_stub_qr()
    stub_w, stub_h = stub_img.size
    stub_array = np.array(stub_img, dtype=np.uint8)
    stub_array = np.where(stub_array == 0, 0, 255).astype(np.uint8)
    stub_img.close()

    header_size = len(header_bytes)
    data_size = os.path.getsize(data_filepath)
    full_data_size = header_size + data_size
    N = compute_canvas_size(full_data_size, stub_w, stub_h)

    meta_width = stub_w + 2 * GAP
    meta_y0 = GAP + stub_h + GAP

    meta_array = create_metadata_v2(
        N, full_data_size, compression_flag, original_size,
        checksum_original, 1 if allow_autorun else 0, 0, 1, meta_width
    )

    # Build forbidden mask once
    forbidden = np.zeros((N, N), dtype=bool)
    forbidden[GAP:GAP+stub_h+GAP, GAP:GAP+stub_w+GAP] = True
    forbidden[meta_y0:meta_y0+METADATA_LINES, GAP:GAP+meta_width] = True

    canvas = np.full((N, N), 255, dtype=np.uint8)

    # Place stub and metadata
    canvas[GAP:GAP+stub_h, GAP:GAP+stub_w] = stub_array
    canvas[meta_y0:meta_y0+METADATA_LINES, GAP:GAP+meta_width] = meta_array

    if abort_check and abort_check():
        raise InterruptedError("Operation aborted by user")

    # Memory-map the data file for efficient large-file access
    # Read via mm[start:end] (returns bytes copy) to avoid held numpy references
    header_arr = np.frombuffer(header_bytes, dtype=np.uint8).copy()

    with open(data_filepath, 'rb') as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        mm_len = len(mm)

        # Process in bands of columns to limit peak RAM usage
        BAND_SIZE = 500
        byte_cursor = 0
        total_bands = math.ceil(N / BAND_SIZE)

        for band_num, band_col_idx_start in enumerate(range(0, N, BAND_SIZE)):
            if abort_check and abort_check():
                mm.close()
                raise InterruptedError("Operation aborted by user")

            band_col_idx_end = min(band_col_idx_start + BAND_SIZE, N)

            # Build snake traversal for this band
            band_rows_list, band_cols_list = [], []
            for col_idx in range(band_col_idx_start, band_col_idx_end):
                col = N - 1 - col_idx
                rows = (np.arange(N, dtype=np.int32) if (col_idx % 2 == 0)
                        else np.arange(N-1, -1, -1, dtype=np.int32))
                band_rows_list.append(rows)
                band_cols_list.append(np.full(N, col, dtype=np.int32))

            rows_flat = np.concatenate(band_rows_list)
            cols_flat = np.concatenate(band_cols_list)
            not_forb = ~forbidden[rows_flat, cols_flat]
            dr = rows_flat[not_forb]
            dc = cols_flat[not_forb]
            n_pixels = len(dr)

            # Assemble data bytes for this band from header then mmap
            # Use np.frombuffer(...).copy() on mm slices to avoid held references
            if byte_cursor + n_pixels <= header_size:
                band_data = header_arr[byte_cursor:byte_cursor+n_pixels]
            elif byte_cursor >= header_size:
                file_start = byte_cursor - header_size
                file_end = min(file_start + n_pixels, mm_len)
                band_data = np.frombuffer(mm[file_start:file_end], dtype=np.uint8).copy()
                if len(band_data) < n_pixels:
                    pad = np.random.randint(0, 256, size=n_pixels - len(band_data), dtype=np.uint8)
                    band_data = np.concatenate([band_data, pad])
            else:
                h_part = header_arr[byte_cursor:].copy()
                file_bytes_needed = n_pixels - len(h_part)
                file_end = min(file_bytes_needed, mm_len)
                f_part = np.frombuffer(mm[:file_end], dtype=np.uint8).copy()
                if file_end < file_bytes_needed:
                    pad = np.random.randint(0, 256, size=file_bytes_needed - file_end, dtype=np.uint8)
                    f_part = np.concatenate([f_part, pad])
                band_data = np.concatenate([h_part, f_part])

            canvas[dr, dc] = band_data[:n_pixels]
            byte_cursor += n_pixels
            del band_data, rows_flat, cols_flat, not_forb, dr, dc

            if progress_callback:
                progress_callback("encode", band_num + 1, total_bands,
                                  f"Encoding... {int((band_num+1)/total_bands*100)}%")

        mm.close()

    del forbidden

    if abort_check and abort_check():
        raise InterruptedError("Operation aborted by user")

    if progress_callback:
        progress_callback("encode", total_bands, total_bands, "Saving image...")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    img = Image.fromarray(canvas, mode='L')
    img.save(output_path, optimize=False, compress_level=1)
    img.close()
    del canvas
    gc.collect()

    return True



def create_qr_plus_multipart(filepath, output_folder, split_size_mb, allow_autorun=True, 
                             progress_callback=None, abort_check=None, use_multithreading=True):
    """
    Create multi-part QR+ - REWRITTEN from scratch.
    
    Each part is a complete, independent QR+ image (just like single-part).
    This avoids all the complex compression/streaming bugs.
    """
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"Input file not found: {filepath}")
    
    file_size = os.path.getsize(filepath)
    filename = os.path.basename(filepath)
    split_size = split_size_mb * 1024 * 1024
    total_parts = math.ceil(file_size / split_size)
    
    _dprint("=" * 60)
    _dprint(f"DEBUG: Creating multi-part QR+ (v2 - clean rewrite)")
    _dprint(f"DEBUG: Input: {filepath}")
    _dprint(f"DEBUG: Output folder: {output_folder}")
    _dprint(f"DEBUG: File size: {file_size:,} bytes ({file_size / (1024*1024):.2f} MB)")
    _dprint(f"DEBUG: Split size: {split_size_mb} MB per part")
    _dprint(f"DEBUG: Total parts: {total_parts}")
    _dprint("=" * 60)
    
    try:
        # Step 1: Compute checksum
        if progress_callback:
            progress_callback("checksum", 0, total_parts, "Computing checksum...")
        
        _dprint("DEBUG: Computing checksum (streaming)...")
        checksum = compute_checksum_stream(filepath)
        _dprint(f"DEBUG: Checksum computed: {checksum.hex()}")
        
        if abort_check and abort_check():
            raise InterruptedError("Operation aborted by user")
        
        # Step 2: Create each part independently
        output_paths = []
        base, ext = os.path.splitext(filename)
        
        with open(filepath, 'rb') as f:
            for part_num in range(total_parts):
                if abort_check and abort_check():
                    for p in output_paths:
                        if os.path.exists(p):
                            os.remove(p)
                    raise InterruptedError("Operation aborted by user")
                
                # Read chunk for this part
                chunk = f.read(split_size)
                _dprint(f"DEBUG: Part {part_num}: Read {len(chunk):,} bytes from file")
                
                # Create header
                # file_type: 0 for first part, 1 for continuation parts
                file_type = 0 if part_num == 0 else 1
                header = encode_header(filename, file_type, file_size, part_num, total_parts)
                _dprint(f"DEBUG: Part {part_num}: Created header ({len(header)} bytes, type={file_type})")
                
                # Create output path
                part_output = os.path.join(output_folder, f"{base}_part{part_num+1:03d}{ext}.png")
                
                # Create QR+ for this part
                # Each part is a complete standalone QR+ image!
                # BUGFIX: Disable compression for multi-part to avoid snake pattern bug
                _dprint(f"DEBUG: Part {part_num}: Creating QR+ image (compression disabled for multi-part)...")
                
                # Temporarily disable compression by passing already-compressed data
                # This forces create_qr_plus to NOT compress
                create_qr_plus(
                    data=chunk,
                    output_path=part_output,
                    header_bytes=header,
                    checksum_original=checksum,
                    original_size=file_size,
                    allow_autorun=allow_autorun,
                    part_num=part_num,
                    total_parts=total_parts,
                    progress_callback=None,
                    is_compressed=True  # Tell it data is "already compressed" so it won't compress
                )
                
                output_paths.append(part_output)
                _dprint(f"DEBUG: Part {part_num}: Complete!")
                
                if progress_callback:
                    progress_callback("done", part_num + 1, total_parts,
                                    f"Completed part {part_num + 1}/{total_parts}")
                
                gc.collect()
        
        _dprint("=" * 60)
        _dprint(f"DEBUG: All {total_parts} parts created successfully!")
        _dprint("=" * 60)
        
        return output_paths
        
    finally:
        gc.collect()


def create_qr_plus_from_text(text, output_path, allow_autorun=True):
    """Create QR+ from text input."""
    if not text.strip():
        raise ValueError("Text input is empty")
    
    _dprint("=" * 60)
    _dprint(f"DEBUG: Creating QR+ from text")
    _dprint(f"DEBUG: Output: {output_path}")
    _dprint(f"DEBUG: Text length: {len(text)} characters")
    _dprint(f"DEBUG: Autorun: {allow_autorun}")
    _dprint("=" * 60)
    
    data = text.encode("utf-8")
    filename = "text_input.txt"
    original_size = len(data)
    checksum = compute_checksum(data)
    
    _dprint(f"DEBUG: Data size: {original_size} bytes")
    _dprint(f"DEBUG: Checksum: {checksum.hex()}")
    _dprint(f"DEBUG: Creating QR+ image...")
    
    header = encode_header(filename, 1, original_size, 0, 1)
    
    result = create_qr_plus(data, output_path, header, checksum, original_size, allow_autorun, 0, 1)
    
    _dprint(f"DEBUG: QR+ created successfully!")
    _dprint("=" * 60)
    
    return result