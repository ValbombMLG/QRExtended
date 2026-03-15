# QRExtended (QR+)

> Store any file inside a PNG image.

Standard QR codes max out at about 3KB. A PNG image has no such limit. QR+ exploits this by treating every pixel as a byte — 256 greyscale shades instead of just black and white — and encoding arbitrary files directly into the image data. The result looks like a noisy greyscale picture with a small QR code in the corner. That stub QR is readable by any standard scanner and points here. The rest of the image *is* your file.

**[Try it in your browser →](https://valbombmlg.github.io/QRExtended)**
No install required. Works on mobile too.

---

## How it works

A standard QR code is 1-bit per pixel (black or white). QR+ uses 8-bit greyscale — one full byte per pixel. Combined with dynamic canvas sizing, there's effectively no size limit imposed by the format itself. The encoded image is a valid PNG that any viewer can open, but only QR+ can decode it back.

Each image contains:
- A **stub QR** in the top-left corner — points to this repo
- A **metadata strip** below it — canvas size, checksum, compression flag, file info
- **Data pixels** filling the rest — your file, encoded in a snake (boustrophedon) pattern, right to left

Files are zlib-compressed before encoding if it helps, and verified with SHA256 on decode. For files over 250 MB the checksum is sampled (first 100 MB + 1 MB every 500 MB + last 100 MB) to keep scan times reasonable.

---

## Get it

### Browser (no install)
**https://valbombmlg.github.io/QRExtended**

Runs entirely in your browser via Pyodide. Nothing is uploaded anywhere — encoding and decoding happen locally on your device. First load takes ~15 seconds while the Python runtime downloads; after that it's cached.

Limitations vs the desktop app:
- 512 MB file size advisory (browser memory)
- Multi-part images not supported
- No auto-run after scan

### Desktop app
Download the latest release for your platform from the [Releases page](../../releases).

Or run from source:
```
pip install -r requirements.txt
python qrext_qt.py
```

Requires Python 3.10+ and PySide6.

---

## Desktop features

- **Creator** — encode any file into a QR+ PNG, with optional multi-part splitting for very large files
- **Scanner** — decode a QR+ PNG back to the original file, with a file preview before committing
- **Drag & drop** — onto the window from either tab
- **Debug mode** — toggle in the footer, persists across sessions
- **Cross-platform** — Windows, macOS, Linux

---

## Build from source

```
pip install pyinstaller
pyinstaller --noconsole --onefile qrext_qt.py
```

Output is in `dist/`.

---

## File format reference

```
Header (prepended to data):
  fname_len    2 bytes   length of filename in bytes
  filename     N bytes   UTF-8 filename
  file_type    1 byte    0 = binary, 1 = text
  file_size    8 bytes   original file size (64-bit)
  part_num     2 bytes   0-indexed part number
  total_parts  2 bytes   1 for single-part files

Metadata strip (greyscale pixels, boustrophedon encoded):
  version          1 byte
  canvas_size      4 bytes
  data_byte_count  8 bytes
  compression      1 byte    1 = zlib compressed
  original_size    8 bytes
  checksum         32 bytes  SHA256 (sampled for large files)
  autorun_flag     1 byte
  part_num         2 bytes
  total_parts      2 bytes
```

---

## Changelog

See [changelog.txt](changelog.txt).

---

## Credits

| Role | Name |
|------|------|
| Designer | ValbombMLG |
| Prototyping | Nova Steele |
| Programming | Ash Claude |

**VB Studios**

---

## License

[MIT](LICENSE)
