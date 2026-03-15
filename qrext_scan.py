# qrext_scan.py
"""
QR+ scanner - Optimized with streaming, multi-part support, and low memory usage.
OPTIMIZED: Dynamic chunk sizing, incremental progress, cached QR generation, fixed decompression.
"""

import numpy as np
from PIL import Image
import qrcode
import hashlib
import zlib
import os
import sys
import subprocess
import webbrowser
import glob
import tempfile
import gc
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

# Disable PIL decompression bomb protection
Image.MAX_IMAGE_PIXELS = None

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
CHUNK_SIZE = 10 * 1024 * 1024  # 10MB chunks

# Multi-threading settings
MAX_WORKER_THREADS = min(8, (os.cpu_count() or 1))

# Cached stub QR (generated once, reused) - OPTIMIZATION
_STUB_QR_CACHE = None


def generate_stub_qr():
    """Generate stub QR for verification. OPTIMIZED: Cached after first generation."""
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


def verify_stub(img):
    """Verify stub QR code is present. Returns stub dimensions."""
    stub_img = None
    img_stub_region = None
    
    try:
        stub_img = generate_stub_qr()
        stub_w, stub_h = stub_img.size
        stub_array = np.array(stub_img, dtype=np.uint8)
        stub_binary = np.where(stub_array == 0, 0, 255).astype(np.uint8)
        
        img_stub_region = img.crop((GAP, GAP, GAP + stub_w, GAP + stub_h))
        img_stub_array = np.array(img_stub_region.convert('L'), dtype=np.uint8)
        img_stub_binary = np.where(img_stub_array < 128, 0, 255).astype(np.uint8)
        
        if not np.array_equal(img_stub_binary, stub_binary):
            raise ValueError("Stub QR code mismatch - not a valid QR+ image")
        
        return stub_w, stub_h
        
    finally:
        if stub_img is not None:
            stub_img.close()
        if img_stub_region is not None:
            img_stub_region.close()
        del stub_array, stub_binary, img_stub_array, img_stub_binary
        gc.collect()


def detect_version(img, stub_w, stub_h):
    """Detect QR+ version. Returns version number (1 or 2)."""
    meta_sample = None
    
    try:
        meta_y0 = GAP + stub_h + GAP
        meta_x0 = GAP
        
        meta_sample = img.crop((meta_x0, meta_y0, meta_x0 + 10, meta_y0 + 2))
        sample_array = np.array(meta_sample.convert('L'), dtype=np.uint8).flatten()
        unique_values = np.unique(sample_array)
        
        if len(unique_values) > 2:
            return 2
        elif set(unique_values).issubset({0, 255}):
            return 1
        else:
            return 2
            
    finally:
        if meta_sample is not None:
            meta_sample.close()
        del sample_array, unique_values
        gc.collect()


def read_metadata_v2(img, stub_w, stub_h):
    """Read metadata from v2 QR+ with multi-part support."""
    meta_img = None
    
    try:
        meta_y0 = GAP + stub_h + GAP
        meta_x0 = GAP
        meta_width = stub_w + 2 * GAP
        
        meta_img = img.crop((meta_x0, meta_y0, meta_x0 + meta_width, meta_y0 + METADATA_LINES))
        meta_array = np.array(meta_img.convert('L'), dtype=np.uint8)
        
        meta_bytes = bytearray()
        for row_idx in range(METADATA_LINES):
            row_data = meta_array[row_idx, :].tolist()
            if row_idx % 2 == 1:
                row_data = row_data[::-1]
            meta_bytes.extend(row_data)
        
        idx = 0
        version = meta_bytes[idx]
        idx += 1
        
        canvas_size = int.from_bytes(meta_bytes[idx:idx+4], 'big')
        idx += 4
        
        data_byte_count = int.from_bytes(meta_bytes[idx:idx+8], 'big')  # 64-bit
        idx += 8
        
        compression_flag = meta_bytes[idx]
        idx += 1
        
        original_size = int.from_bytes(meta_bytes[idx:idx+8], 'big')    # 64-bit
        idx += 8
        
        checksum_original = bytes(meta_bytes[idx:idx+32])
        idx += 32
        
        autorun_flag = meta_bytes[idx]
        idx += 1
        
        if idx + 4 <= len(meta_bytes):
            part_num = int.from_bytes(meta_bytes[idx:idx+2], 'big')
            idx += 2
            total_parts = int.from_bytes(meta_bytes[idx:idx+2], 'big')
            
            if total_parts == 0:
                part_num = 0
                total_parts = 1
        else:
            part_num = 0
            total_parts = 1
        
        return (canvas_size, data_byte_count, compression_flag, 
                original_size, checksum_original, autorun_flag, part_num, total_parts)
                
    finally:
        if meta_img is not None:
            meta_img.close()
        del meta_array, meta_bytes
        gc.collect()


def calculate_optimal_chunk_size(canvas_size, available_ram_mb=200):
    """Calculate optimal column chunk size. OPTIMIZATION: Dynamic sizing."""
    bytes_per_column = canvas_size
    max_chunk_bytes = (available_ram_mb * 1024 * 1024) // 2
    optimal_columns = max_chunk_bytes // bytes_per_column
    return max(50, min(500, optimal_columns))


def read_data_snake_v2_streaming(img, canvas_size, stub_w, stub_h, data_byte_count, output_file, 
                                 progress_callback=None, abort_check=None):
    """Read data from v2 QR+. FIXED: Process chunks right-to-left to match writing order."""
    N = canvas_size
    
    forbidden = np.zeros((N, N), dtype=bool)
    forbidden[GAP:GAP+stub_h+GAP, GAP:GAP+stub_w+GAP] = True
    meta_y0 = GAP + stub_h + GAP
    meta_width = stub_w + 2 * GAP
    forbidden[meta_y0:meta_y0+METADATA_LINES, GAP:GAP+meta_width] = True
    
    bytes_read = 0
    chunk_img = None
    
    # OPTIMIZATION: Calculate optimal chunk size
    COLUMN_CHUNK = calculate_optimal_chunk_size(N)
    
    # FIX: Process chunks RIGHT-TO-LEFT to match writing order!
    chunk_starts = list(range(0, N, COLUMN_CHUNK))
    chunk_starts.reverse()  # Process from right to left!
    total_chunks = len(chunk_starts)
    chunk_idx = 0
    
    try:
        with open(output_file, 'wb') as f_out:
            for col_start in chunk_starts:
                if abort_check and abort_check():
                    raise InterruptedError("Scan aborted by user")
                
                col_end = min(col_start + COLUMN_CHUNK, N)
                
                chunk_img = img.crop((col_start, 0, col_end, N))
                chunk_array = np.array(chunk_img.convert('L'), dtype=np.uint8)
                
                # Process columns within chunk RIGHT-TO-LEFT as well
                local_cols = list(range(col_end - col_start))
                local_cols.reverse()  # Right to left within chunk!
                
                # Build snake order for this chunk vectorized
                chunk_rows_list, chunk_cols_list = [], []
                for local_col_idx in local_cols:
                    global_col = col_start + local_col_idx
                    col_idx_from_right = N - 1 - global_col
                    down = (col_idx_from_right % 2 == 0)
                    rows = np.arange(N, dtype=np.int32) if down else np.arange(N-1, -1, -1, dtype=np.int32)
                    chunk_rows_list.append(rows)
                    chunk_cols_list.append(np.full(N, local_col_idx, dtype=np.int32))

                chunk_rows_flat = np.concatenate(chunk_rows_list)
                chunk_cols_flat = np.concatenate(chunk_cols_list)
                chunk_global_cols = np.concatenate([
                    np.full(N, col_start + lc, dtype=np.int32) for lc in local_cols
                ])

                # Filter forbidden pixels
                not_forb = ~forbidden[chunk_rows_flat, chunk_global_cols]
                valid_rows = chunk_rows_flat[not_forb]
                valid_local_cols = chunk_cols_flat[not_forb]

                # Extract pixel values in one shot
                pixel_vals = chunk_array[valid_rows, valid_local_cols]

                # Write only as many bytes as needed
                remaining = data_byte_count - bytes_read
                to_write = pixel_vals[:remaining]
                f_out.write(to_write.tobytes())
                bytes_read += len(to_write)

                if bytes_read >= data_byte_count:
                    return
                
                chunk_img.close()
                chunk_img = None
                del chunk_array
                gc.collect()
                
                chunk_idx += 1
                if progress_callback:
                    progress_pct = int((chunk_idx / total_chunks) * 100)
                    progress_callback("reading", chunk_idx, total_chunks, 
                                    f"Reading data: {progress_pct}%")
        
        return
        
    finally:
        if chunk_img is not None:
            chunk_img.close()
        del forbidden
        gc.collect()


def decode_header(data_bytes):
    """Decode header with multi-part support."""
    idx = 0
    fname_len = int.from_bytes(data_bytes[idx:idx+2], 'big')
    idx += 2
    
    fname_bytes = data_bytes[idx:idx+fname_len]
    filename = fname_bytes.decode("utf-8", errors="replace")
    idx += fname_len
    
    file_type = data_bytes[idx]
    idx += 1
    
    file_size = int.from_bytes(data_bytes[idx:idx+8], 'big')  # 64-bit
    idx += 8
    
    if idx + 4 <= len(data_bytes):
        part_num = int.from_bytes(data_bytes[idx:idx+2], 'big')
        total_parts = int.from_bytes(data_bytes[idx+2:idx+4], 'big')
        
        if total_parts > 1000 or part_num > 1000:
            part_num = 0
            total_parts = 1
            remaining_data = data_bytes[idx:]
        else:
            idx += 4
            remaining_data = data_bytes[idx:]
    else:
        part_num = 0
        total_parts = 1
        remaining_data = data_bytes[idx:]
    
    return filename, file_type, part_num, total_parts, remaining_data


def compute_checksum(data):
    """Compute SHA256 checksum."""
    return hashlib.sha256(data).digest()


SAMPLED_CHECKSUM_THRESHOLD = 250 * 1024 * 1024  # 250 MB
SAMPLED_CHECKSUM_EDGE     = 100 * 1024 * 1024  # 100 MB head + tail
SAMPLED_CHECKSUM_INTERVAL = 500 * 1024 * 1024  # sample every 500 MB
SAMPLED_CHECKSUM_CHUNK    =   1 * 1024 * 1024  # 1 MB per mid-sample


def _sampled_checksum_regions(file_size):
    """Mirror of create-side: same regions in same order."""
    edge = SAMPLED_CHECKSUM_EDGE
    interval = SAMPLED_CHECKSUM_INTERVAL
    chunk = SAMPLED_CHECKSUM_CHUNK

    regions = []
    head_end = min(edge, file_size)
    regions.append((0, head_end))

    tail_start = max(0, file_size - edge)
    pos = interval
    while pos < tail_start:
        sample_end = min(pos + chunk, tail_start)
        sample_len = sample_end - pos
        if sample_len > 0:
            regions.append((pos, sample_len))
        pos += interval

    if tail_start > head_end:
        regions.append((tail_start, file_size - tail_start))

    return regions


def compute_checksum_stream(filepath, progress_callback=None):
    """
    Compute checksum of file, matching create-side sampling logic exactly.
    - Files < 250 MB: full SHA256
    - Files >= 250 MB: sampled SHA256 (head + mid samples + tail)
    """
    sha256 = hashlib.sha256()
    file_size = os.path.getsize(filepath)

    if file_size < SAMPLED_CHECKSUM_THRESHOLD:
        bytes_read = 0
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                sha256.update(chunk)
                bytes_read += len(chunk)
                if progress_callback and file_size > 0:
                    progress_callback("reading", bytes_read, file_size,
                                      f"Verifying checksum: {int(bytes_read/file_size*100)}%")
    else:
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
                        progress_callback("reading", bytes_read, total_sample,
                                          f"Verifying checksum (sampled): {int(bytes_read/total_sample*100)}%")

    return sha256.digest()


def is_url(text):
    """Check if text is a URL."""
    text = text.strip()
    return (text.startswith("http://") or text.startswith("https://") or 
            text.startswith("www."))


def open_with_default_app(path):
    """Open file with system default application."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", path], check=True)
        elif sys.platform.startswith("darwin"):
            subprocess.run(["open", path], check=True)
        else:
            webbrowser.open(path)
        return True, None
    except Exception as e:
        return False, str(e)


def find_multipart_qr_images(input_path):
    """Find all parts of a multi-part QR+ in the same folder."""
    folder = os.path.dirname(input_path)
    all_images = glob.glob(os.path.join(folder, "*.png"))
    
    qr_parts = []
    for img_path in all_images:
        try:
            img = Image.open(img_path)
            stub_w, stub_h = verify_stub(img)
            version = detect_version(img, stub_w, stub_h)
            
            if version == 2:
                metadata = read_metadata_v2(img, stub_w, stub_h)
                part_num = metadata[6]
                total_parts = metadata[7]
                
                if total_parts > 1:
                    qr_parts.append((img_path, part_num, total_parts))
            
            img.close()
        except:
            continue
    
    return qr_parts


def scan_qr_plus(input_path, output_folder, run_requested=False, skip_checksum=False,
                 delete_after=False, progress_callback=None, abort_check=None,
                 confirm_cb=None, error_cb=None):
    """
    Scan and decode QR+ image.
    confirm_cb(title, message) -> bool  — used for executable run warning
    error_cb(title, message)            — used for open-failed notification
    Both fall back to console output if not provided.
    """
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")
    
    img = None
    temp_data = None
    temp_decompressed = None
    content_file = None
    
    try:
        img = Image.open(input_path)
        stub_w, stub_h = verify_stub(img)
        version = detect_version(img, stub_w, stub_h)
        
        if version != 2:
            raise ValueError("V1 format no longer supported. Please use V2.")
        
        (canvas_size, data_byte_count, compression_flag, 
         original_size, checksum_original, autorun_flag, part_num, total_parts) = read_metadata_v2(img, stub_w, stub_h)
        
        _dprint("=" * 60)
        _dprint("DEBUG: Metadata read:")
        _dprint(f"  Canvas size: {canvas_size}")
        _dprint(f"  Data byte count: {data_byte_count}")
        _dprint(f"  Compression flag: {compression_flag}")
        _dprint(f"  Original size: {original_size}")
        _dprint(f"  Part {part_num}/{total_parts}")
        _dprint("=" * 60)
        
        if total_parts > 1:
            return scan_qr_plus_multipart(input_path, output_folder, run_requested,
                                         skip_checksum, delete_after, progress_callback,
                                         abort_check, confirm_cb, error_cb)
        
        if progress_callback:
            progress_callback("reading", 0, 1, "Reading QR+ image...")
        
        temp_data = tempfile.NamedTemporaryFile(suffix=".dat", delete=False).name
        
        read_data_snake_v2_streaming(img, canvas_size, stub_w, stub_h, data_byte_count, temp_data,
                                     progress_callback, abort_check)
        
        img.close()
        img = None
        gc.collect()
        
        _dprint("=" * 60)
        _dprint(f"DEBUG: Extracted {data_byte_count} bytes to temp file")
        _dprint(f"DEBUG: Temp file size: {os.path.getsize(temp_data)} bytes")
        _dprint("=" * 60)
        
        with open(temp_data, 'rb') as f:
            header_peek = f.read(4096)
        
        if len(header_peek) < 10:
            raise ValueError(f"Extracted data too short ({len(header_peek)} bytes)")
        
        _dprint("=" * 60)
        _dprint("DEBUG: First 100 bytes of extracted data:")
        _dprint("As hex:", header_peek[:100].hex())
        _dprint("=" * 60)
        
        # Try to decode header first
        # If it fails, the entire data (including header) might be compressed
        try:
            _dprint("DEBUG: Attempting to decode header...")
            filename, file_type, _, _, _ = decode_header(header_peek)
            _dprint(f"DEBUG: Header decoded successfully - Filename: {filename}, Type: {file_type}")
            
            # Header decoded successfully - structure is [header][possibly_compressed_data]
            fname_len = len(filename.encode('utf-8'))
            header_size = 2 + fname_len + 1 + 8 + 4
            content_file = temp_data
            header_was_compressed = False
            
        except (IndexError, UnicodeDecodeError, Exception) as e:
            _dprint(f"DEBUG: Header decode failed: {e}")
            _dprint("DEBUG: Entire data blob (including header) appears to be compressed")
            
            # The entire blob is compressed - decompress it first
            if not compression_flag:
                raise ValueError("Cannot decode header and compression_flag=0. File may be corrupted.")
            
            if progress_callback:
                progress_callback("reading", 0, 1, "Decompressing entire data blob...")
            
            _dprint("DEBUG: Decompressing entire extracted data (header+data)...")
            temp_decompressed = tempfile.NamedTemporaryFile(suffix=".dat", delete=False).name
            
            try:
                with open(temp_data, 'rb') as f_in, open(temp_decompressed, 'wb') as f_out:
                    decompressed_data = zlib.decompress(f_in.read())
                    f_out.write(decompressed_data)
                
                _dprint(f"DEBUG: Decompression successful! {len(decompressed_data)} bytes")
                
                # Now try to decode header from decompressed data
                with open(temp_decompressed, 'rb') as f:
                    header_peek = f.read(4096)
                
                _dprint("=" * 60)
                _dprint("DEBUG: First 100 bytes AFTER decompression:")
                _dprint("As hex:", header_peek[:100].hex())
                _dprint("=" * 60)
                
                filename, file_type, _, _, _ = decode_header(header_peek)
                _dprint(f"DEBUG: Header decoded from decompressed data - Filename: {filename}")
                
                fname_len = len(filename.encode('utf-8'))
                header_size = 2 + fname_len + 1 + 8 + 4
                
                os.remove(temp_data)
                temp_data = None
                content_file = temp_decompressed
                header_was_compressed = True
                
            except zlib.error as ze:
                _dprint("=" * 60)
                _dprint("DECOMPRESSION ERROR!")
                _dprint(f"Error: {ze}")
                _dprint("=" * 60)
                raise ValueError(f"Decompression failed: {ze}")
        
        _dprint(f"DEBUG: Header size: {header_size} bytes")
        _dprint(f"DEBUG: Filename: {filename}")
        _dprint(f"DEBUG: header_was_compressed: {header_was_compressed}")
        _dprint("=" * 60)
        
        # Now handle data decompression if needed
        # If header was already compressed with data, we're done with decompression
        if compression_flag and not header_was_compressed:
            # Data after header is still compressed
            if progress_callback:
                progress_callback("reading", 0, 1, "Decompressing data...")
            
            _dprint("DEBUG: Data after header is compressed, decompressing...")
            temp_decompressed = tempfile.NamedTemporaryFile(suffix=".dat", delete=False).name
            
            try:
                with open(content_file, 'rb') as f_in, open(temp_decompressed, 'wb') as f_out:
                    f_in.seek(header_size)
                    
                    decompressor = zlib.decompressobj()
                    bytes_decompressed = 0
                    
                    while True:
                        chunk = f_in.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        decompressed = decompressor.decompress(chunk)
                        f_out.write(decompressed)
                        bytes_decompressed += len(decompressed)
                    
                    final = decompressor.flush()
                    f_out.write(final)
                    bytes_decompressed += len(final)
                
                _dprint(f"DEBUG: Decompression successful! {bytes_decompressed} bytes")
                _dprint(f"DEBUG: Expected original size: {original_size}")
                _dprint("=" * 60)
                
                os.remove(content_file)
                content_file = temp_decompressed
                temp_decompressed = None
                
            except zlib.error as e:
                _dprint("=" * 60)
                _dprint("DECOMPRESSION ERROR!")
                _dprint(f"Error: {e}")
                _dprint("=" * 60)
                raise ValueError(f"Decompression failed: {e}")
        
        os.makedirs(output_folder, exist_ok=True)
        out_path = os.path.join(output_folder, filename)

        if progress_callback:
            progress_callback("reading", 0, 1, "Saving file...")

        _dprint(f"DEBUG: Saving to: {out_path}")

        with open(content_file, 'rb') as f_in, open(out_path, 'wb') as f_out:
            # Skip header based on how data was decompressed
            if header_was_compressed:
                f_in.seek(header_size)
            elif compression_flag:
                pass  # Header already removed in decompressed file
            else:
                f_in.seek(header_size)

            while True:
                chunk = f_in.read(CHUNK_SIZE)
                if not chunk:
                    break
                f_out.write(chunk)
        
        final_size = os.path.getsize(out_path)
        _dprint(f"DEBUG: File saved successfully! Size: {final_size} bytes")
        _dprint("=" * 60)

        # Verify checksum against saved output file (uses sampled logic for large files)
        if not skip_checksum:
            if progress_callback:
                progress_callback("reading", 0, 1, "Verifying checksum...")
            actual_checksum = compute_checksum_stream(out_path, progress_callback)
            _dprint(f"DEBUG: Expected checksum: {checksum_original.hex()}")
            _dprint(f"DEBUG: Actual checksum:   {actual_checksum.hex()}")
            if actual_checksum != checksum_original:
                os.remove(out_path)  # Remove corrupt output
                raise ValueError("Checksum mismatch! File may be corrupted.")

        if file_type == 1:
            with open(out_path, 'r', encoding='utf-8', errors='replace') as f:
                text_preview = f.read(1024)
            
            if run_requested and is_url(text_preview.strip()):
                url = text_preview.strip().split('\n')[0]
                if url.startswith("www."):
                    url = "http://" + url
                webbrowser.open(url)
        else:
            if autorun_flag and run_requested:
                _, ext = os.path.splitext(out_path)
                ext = ext.lower()
                executables = {".exe", ".bat", ".cmd", ".vbs", ".ps1", ".msi", ".sh"}
                
                if ext in executables:
                    msg = (f"The file {filename} is executable ({ext}).\n"
                           "Running executables can be dangerous.\n"
                           "Do you want to run it anyway?")
                    if confirm_cb:
                        ok = confirm_cb("Warning", msg)
                    else:
                        _dprint(f"WARNING: {msg}")
                        ok = False
                    if not ok:
                        if delete_after:
                            try:
                                os.remove(input_path)
                                _dprint(f"DEBUG: Deleted QR+ image (user declined to run executable)")
                            except Exception as e:
                                _dprint(f"DEBUG: Failed to delete {input_path}: {e}")
                        return out_path
                
                success, error_msg = open_with_default_app(out_path)
                if not success:
                    msg = (f"Could not open file.\nError: {error_msg}\n"
                           f"File saved to: {out_path}")
                    if error_cb:
                        error_cb("Open failed", msg)
                    else:
                        _dprint(f"ERROR: {msg}")
        
        if delete_after:
            _dprint(f"DEBUG: Deleting QR+ image: {input_path}")
            try:
                os.remove(input_path)
                _dprint(f"DEBUG: Successfully deleted QR+ image")
            except Exception as e:
                _dprint(f"DEBUG: Failed to delete {input_path}: {e}")
        
        return out_path
        
    finally:
        if img is not None:
            img.close()
        if temp_data is not None and os.path.exists(temp_data):
            os.remove(temp_data)
        if temp_decompressed is not None and os.path.exists(temp_decompressed):
            os.remove(temp_decompressed)
        # Clean up content_file if it points to a different temp file than the above
        if (content_file is not None
                and content_file != temp_data
                and content_file != temp_decompressed
                and os.path.exists(content_file)):
            os.remove(content_file)
        gc.collect()


def scan_qr_plus_multipart(input_path, output_folder, run_requested=False,
                           skip_checksum=False, delete_after=False, progress_callback=None,
                           abort_check=None, confirm_cb=None, error_cb=None):
    """Scan and reassemble multi-part QR+ images."""
    temp_assembled = None
    temp_part = None
    
    try:
        qr_parts = find_multipart_qr_images(input_path)
        
        if not qr_parts:
            raise ValueError("No valid multi-part QR+ images found in folder.")
        
        qr_parts.sort(key=lambda x: x[1])
        
        total_parts = qr_parts[0][2]
        if len(qr_parts) != total_parts:
            raise ValueError(f"Missing parts! Found {len(qr_parts)} of {total_parts}")
        
        _dprint("=" * 60)
        _dprint(f"DEBUG: Found {len(qr_parts)} parts of multi-part QR+")
        for img_path, pnum, tparts in qr_parts:
            _dprint(f"DEBUG: - {os.path.basename(img_path)}: part_num={pnum}, total={tparts}")
        _dprint("=" * 60)
        
        temp_assembled = tempfile.NamedTemporaryFile(suffix=".dat", delete=False).name
        filename = None
        file_type = None
        checksum_original = None
        compression_flag = None
        autorun_flag_saved = False  # Save autorun from part 0
        
        # First pass: Find part 0 to get header and compression info
        part_0_data = None
        for img_path, pnum, _ in qr_parts:
            if pnum == 0:
                _dprint(f"DEBUG: Reading part 0 metadata from: {img_path}")
                img = Image.open(img_path)
                stub_w, stub_h = verify_stub(img)
                (canvas_size, data_byte_count, comp_flag, 
                 original_size, checksum, autorun_flag, _, _) = read_metadata_v2(img, stub_w, stub_h)
                img.close()
                
                compression_flag = comp_flag
                checksum_original = checksum
                autorun_flag_saved = autorun_flag  # Save it!
                _dprint(f"DEBUG: Compression flag from part 0: {compression_flag}")
                _dprint(f"DEBUG: Checksum from part 0: {checksum_original.hex()}")
                _dprint(f"DEBUG: Autorun flag from part 0: {autorun_flag_saved}")
                break
        
        if compression_flag is None:
            raise ValueError("Could not find part 0 to read compression flag!")
        
        with open(temp_assembled, 'wb') as f_out:
            for idx, (img_path, part_num, _) in enumerate(qr_parts):
                if abort_check and abort_check():
                    raise InterruptedError("Scan aborted by user")
                
                if progress_callback:
                    progress_callback("reading", idx + 1, len(qr_parts), 
                                    f"Reading part {idx + 1}/{len(qr_parts)}...")
                
                _dprint(f"DEBUG: Processing part {idx + 1}/{len(qr_parts)}: {img_path}")
                _dprint(f"DEBUG: This is part_num={part_num} from metadata")
                
                img = None
                try:
                    img = Image.open(img_path)
                    stub_w, stub_h = verify_stub(img)
                    
                    (canvas_size, data_byte_count, comp_flag, 
                     original_size, checksum, autorun_flag, _, _) = read_metadata_v2(img, stub_w, stub_h)
                    
                    temp_part = tempfile.NamedTemporaryFile(suffix=".part", delete=False).name
                    read_data_snake_v2_streaming(img, canvas_size, stub_w, stub_h, data_byte_count, temp_part,
                                                None, abort_check)
                    
                    img.close()
                    img = None
                    gc.collect()
                    
                    with open(temp_part, 'rb') as f_in:
                        part_data = f_in.read()
                    
                    _dprint(f"DEBUG: Part {part_num} - Extracted {len(part_data)} bytes")
                    _dprint(f"DEBUG: Part {part_num} - First 32 bytes (hex): {part_data[:32].hex()}")
                    
                    if part_num == 0:  # Fixed: Check part_num, not idx - only part 0 has header!
                        if compression_flag:
                            # Part 0 data is: compressed(header + file_data)
                            # Decompress the ENTIRE part with one-shot decompression
                            _dprint(f"DEBUG: Part 0 - Decompressing {len(part_data)} bytes to extract header...")
                            
                            try:
                                decompressed_data = zlib.decompress(part_data)
                                _dprint(f"DEBUG: Part 0 - Decompressed to {len(decompressed_data)} bytes")
                            except zlib.error as e:
                                _dprint(f"DEBUG: Part 0 - Decompression FAILED: {e}")
                                _dprint(f"DEBUG: Part 0 - Data is likely NOT compressed despite compression_flag=1")
                                _dprint(f"DEBUG: Part 0 - Treating as uncompressed data...")
                                decompressed_data = part_data
                            
                            # Now decode header from decompressed data
                            fname, ftype, _, _, _ = decode_header(decompressed_data)
                            filename = fname
                            file_type = ftype
                            
                            _dprint(f"DEBUG: Part 0 - Decoded header: {filename}")
                            
                            fname_len = len(filename.encode('utf-8'))
                            header_size = 2 + fname_len + 1 + 8 + 2 + 2
                            
                            _dprint(f"DEBUG: Part 0 - Header size: {header_size} bytes")
                            
                            # Write the decompressed data AFTER the header to output
                            data_after_header = decompressed_data[header_size:]
                            f_out.write(data_after_header)
                            _dprint(f"DEBUG: Part 0 - Wrote {len(data_after_header)} bytes (after removing {header_size}-byte header)")
                        else:
                            # Part 0 data is: header + file_data (uncompressed)
                            _dprint(f"DEBUG: Part 0 - Reading uncompressed data...")
                            
                            fname, ftype, _, _, _ = decode_header(part_data)
                            filename = fname
                            file_type = ftype
                            
                            _dprint(f"DEBUG: Part 0 - Decoded header: {filename}")
                            
                            fname_len = len(filename.encode('utf-8'))
                            header_size = 2 + fname_len + 1 + 8 + 2 + 2
                            
                            _dprint(f"DEBUG: Part 0 - Header size: {header_size} bytes")
                            
                            # Write data after header
                            f_out.write(part_data[header_size:])
                            _dprint(f"DEBUG: Part 0 - Wrote {len(part_data) - header_size} bytes (after removing {header_size}-byte header)")
                    else:
                        # Parts 1-4: ALSO have headers now (in new multipart format)!
                        _dprint(f"DEBUG: Part {part_num} - Processing data...")
                        
                        if compression_flag:
                            _dprint(f"DEBUG: Part {part_num} - Decompressing {len(part_data)} bytes...")
                            try:
                                decompressed_data = zlib.decompress(part_data)
                                
                                # Remove header from decompressed data
                                fname, _, _, _, _ = decode_header(decompressed_data)
                                fname_len = len(fname.encode('utf-8'))
                                header_size = 2 + fname_len + 1 + 8 + 2 + 2
                                data_after_header = decompressed_data[header_size:]
                                
                                f_out.write(data_after_header)
                                _dprint(f"DEBUG: Part {part_num} - Wrote {len(data_after_header)} decompressed bytes (removed {header_size}-byte header)")
                            except zlib.error as e:
                                _dprint(f"DEBUG: Part {part_num} - Decompression FAILED: {e}")
                                _dprint(f"DEBUG: Part {part_num} - Data is likely NOT compressed despite compression_flag=1")
                                _dprint(f"DEBUG: Part {part_num} - Treating as uncompressed...")
                                
                                # Remove header from uncompressed data
                                fname, _, _, _, _ = decode_header(part_data)
                                fname_len = len(fname.encode('utf-8'))
                                header_size = 2 + fname_len + 1 + 8 + 2 + 2
                                data_after_header = part_data[header_size:]
                                
                                f_out.write(data_after_header)
                                _dprint(f"DEBUG: Part {part_num} - Wrote {len(data_after_header)} bytes (removed {header_size}-byte header)")
                        else:
                            _dprint(f"DEBUG: Part {part_num} - Removing header from uncompressed data...")
                            
                            # Remove header
                            fname, _, _, _, _ = decode_header(part_data)
                            fname_len = len(fname.encode('utf-8'))
                            header_size = 2 + fname_len + 1 + 8 + 2 + 2
                            data_after_header = part_data[header_size:]
                            
                            f_out.write(data_after_header)
                            _dprint(f"DEBUG: Part {part_num} - Wrote {len(data_after_header)} bytes (removed {header_size}-byte header)")
                    
                    del part_data
                    
                    os.remove(temp_part)
                    temp_part = None
                    
                finally:
                    if img is not None:
                        img.close()
                    gc.collect()
        
        _dprint(f"DEBUG: Multi-part assembly complete!")
        _dprint(f"DEBUG: Assembled file size: {os.path.getsize(temp_assembled)} bytes")
        _dprint("=" * 60)
        
        # OPTIMIZATION: Verify checksum with progress
        if not skip_checksum:
            if progress_callback:
                progress_callback("reading", len(qr_parts), len(qr_parts), "Verifying checksum...")
            
            _dprint("DEBUG: Computing checksum of assembled file...")
            actual_checksum = compute_checksum_stream(temp_assembled, progress_callback)
            
            _dprint(f"DEBUG: Expected checksum: {checksum_original.hex()}")
            _dprint(f"DEBUG: Actual checksum:   {actual_checksum.hex()}")
            _dprint("=" * 60)
            
            if actual_checksum != checksum_original:
                raise ValueError("Checksum mismatch! Reassembled file may be corrupted.")
        
        os.makedirs(output_folder, exist_ok=True)
        out_path = os.path.join(output_folder, filename)
        
        if progress_callback:
            progress_callback("reading", len(qr_parts), len(qr_parts), "Saving file...")
        
        _dprint(f"DEBUG: Saving to: {out_path}")
        
        if file_type == 1:
            with open(temp_assembled, 'r', encoding='utf-8', errors='replace') as f_in:
                with open(out_path, 'w', encoding='utf-8') as f_out:
                    f_out.write(f_in.read())
        else:
            import shutil
            shutil.move(temp_assembled, out_path)
            temp_assembled = None
        
        final_size = os.path.getsize(out_path)
        _dprint(f"DEBUG: Multi-part file saved successfully! Size: {final_size} bytes")
        _dprint("=" * 60)
        
        # Handle autorun (same logic as single-part)
        if autorun_flag_saved and run_requested:
            _dprint(f"DEBUG: Autorun enabled for multi-part file: {out_path}")
            try:
                _, ext = os.path.splitext(out_path)
                ext = ext.lower()
                executables = {".exe", ".bat", ".cmd", ".vbs", ".ps1", ".msi", ".sh"}
                
                if ext in executables:
                    msg = (f"The file {filename} is executable ({ext}).\n"
                           "Running executables can be dangerous.\n"
                           "Do you want to run it anyway?")
                    if confirm_cb:
                        ok = confirm_cb("Warning", msg)
                    else:
                        _dprint(f"WARNING: {msg}")
                        ok = False
                    if not ok:
                        _dprint(f"DEBUG: User declined to run executable")
                        if delete_after:
                            _dprint(f"DEBUG: Deleting {len(qr_parts)} QR+ image parts...")
                            deleted_count = 0
                            for img_path, _, _ in qr_parts:
                                try:
                                    os.remove(img_path)
                                    deleted_count += 1
                                except Exception as e:
                                    _dprint(f"DEBUG: Failed to delete {img_path}: {e}")
                            _dprint(f"DEBUG: Deleted {deleted_count}/{len(qr_parts)} parts")
                        return out_path
                
                success, error_msg = open_with_default_app(out_path)
                if not success:
                    msg = (f"Could not open file.\nError: {error_msg}\n"
                           f"File saved to: {out_path}")
                    if error_cb:
                        error_cb("Open failed", msg)
                    else:
                        _dprint(f"ERROR: {msg}")
            except Exception as e:
                _dprint(f"DEBUG: Autorun failed: {e}")
        
        if delete_after:
            _dprint(f"DEBUG: Deleting {len(qr_parts)} QR+ image parts...")
            deleted_count = 0
            for img_path, _, _ in qr_parts:
                try:
                    os.remove(img_path)
                    deleted_count += 1
                except Exception as e:
                    _dprint(f"DEBUG: Failed to delete {img_path}: {e}")
            _dprint(f"DEBUG: Deleted {deleted_count}/{len(qr_parts)} parts")
        
        return out_path
        
    finally:
        if temp_assembled is not None and os.path.exists(temp_assembled):
            os.remove(temp_assembled)
        if temp_part is not None and os.path.exists(temp_part):
            os.remove(temp_part)
        gc.collect()