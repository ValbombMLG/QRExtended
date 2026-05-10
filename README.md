# QRExtended (QR+) — Store Any File Inside a PNG Image

> Turn PNG images into ultra-high-capacity QR-style containers for files, backups, text, archives, and more.

QRExtended (QR+) is an experimental open-source file storage system that encodes any file directly into a PNG image. Unlike traditional QR codes, which are limited to roughly 3 KB of data, QR+ uses full 8-bit grayscale pixel values to store raw binary information.

The result is a valid PNG image that can contain documents, executables, videos, ZIP archives, game files, source code, and other large data payloads.

A small QR code embedded in the image acts as a compatibility stub and links back to the project, while the image itself contains the encoded file data.

## Features

- Encode virtually any file into a PNG image
- Store far more data than standard QR codes
- Browser-based encoder and decoder
- Desktop app for Windows, Linux, and macOS
- Local-only processing — files are never uploaded
- Automatic zlib compression when beneficial
- SHA256 integrity verification during decoding
- Multi-part image support for massive files
- Drag-and-drop support
- Open-source MIT licensed project

---

## Try QR+ Online

### Browser Version (No Install Required)

**https://valbombmlg.github.io/QRExtended**

The web version runs entirely in your browser using Pyodide and Python compiled to WebAssembly.

### Benefits of the Web Version

- No installation required
- Works on desktop and mobile devices
- Fully local file processing
- Fast encoding and decoding
- Cross-platform compatibility

### Browser Version Limitations

- Recommended maximum file size: 512 MB
- Multi-part image encoding is not supported
- Auto-run functionality is unavailable
- First launch may take approximately 15 seconds while the Python runtime downloads

After the initial load, assets are cached locally for faster startup.

---

# What Makes QR+ Different From Standard QR Codes?

Traditional QR codes are binary.
Each pixel is either black or white, meaning every pixel stores only 1 bit of information.

QR+ instead treats grayscale PNG pixels as full bytes:

- Standard QR pixel = 1 bit
- QR+ grayscale pixel = 8 bits

This massively increases storage density.

Combined with dynamically sized PNG canvases, QR+ effectively removes the strict size limitations associated with traditional QR systems.

The encoded image remains a perfectly valid PNG file viewable in any image viewer.

---

# How QR+ Works

Each generated QR+ image contains three main sections:

## 1. Stub QR Code

Located in the top-left corner.

This standard QR code can be scanned by regular QR scanners and links users to the QR+ project repository or decoder.

## 2. Metadata Strip

The grayscale metadata area stores:

- Canvas size
- Compression information
- Original file size
- Checksum data
- Multi-part metadata
- File reconstruction information

## 3. Encoded Data Region

The remainder of the PNG stores the actual file bytes.

Data is encoded using a boustrophedon (snake-style) traversal pattern moving right-to-left across rows.

---

# Compression and Integrity Verification

QR+ automatically compresses files using zlib when compression improves storage efficiency.

During decoding, SHA256 validation ensures the recovered file matches the original.

For extremely large files exceeding 250 MB, QR+ uses sampled checksum verification:

- First 100 MB
- 1 MB every 500 MB
- Last 100 MB

This keeps integrity checks practical while maintaining strong validation.

---

# Desktop Application

The desktop application provides advanced QR+ creation and scanning tools.

## Desktop Features

- Encode files into QR+ PNG images
- Decode QR+ images back into original files
- Multi-part file splitting for huge payloads
- Preview files before extraction
- Drag-and-drop support
- Persistent debug mode
- Native desktop experience
- Cross-platform compatibility

Supported operating systems:

- Windows
- Linux
- macOS

---

# Download QR+

Download the latest version from the Releases page:

`../../releases`

---

# Run From Source

## Requirements

- Python 3.10+
- PySide6

## Installation

```bash
pip install -r requirements.txt
python qrext_qt.py
```

---

# Build Executable From Source

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile qrext_qt.py
```

Compiled output will appear in the `dist/` directory.

---

# QR+ File Format Reference

## File Header Structure

```text
Header (prepended to data):
  fname_len    2 bytes   length of filename in bytes
  filename     N bytes   UTF-8 filename
  file_type    1 byte    0 = binary, 1 = text
  file_size    8 bytes   original file size (64-bit)
  part_num     2 bytes   0-indexed part number
  total_parts  2 bytes   1 for single-part files
```

## Metadata Strip Structure

```text
Metadata strip (greyscale pixels, boustrophedon encoded):
  version          1 byte
  canvas_size      4 bytes
  data_byte_count  8 bytes
  compression      1 byte    1 = zlib compressed
  original_size    8 bytes
  checksum         32 bytes  SHA256 checksum
  autorun_flag     1 byte
  part_num         2 bytes
  total_parts      2 bytes
```

---

# Potential Use Cases

QR+ can be used for:

- Experimental data storage
- Steganography-inspired projects
- Offline file transfer
- Digital preservation
- Archival systems
- Creative coding projects
- Game mod packaging
- Educational demonstrations
- Data density experiments
- Portable backup systems

---

# Changelog

See:

`changelog.txt`

---

# Credits

| Role | Name |
|------|------|
| Designer | ValbombMLG |
| Prototyping | Nova Steele |
| Programming | Ash Claude |

### VB Studios

---

# Links

- Linktree: https://linktr.ee/ValbombMLG
- Ko-fi: https://ko-fi.com/valbombmlg

---

# License

Licensed under the MIT License.

See `LICENSE` for details.

