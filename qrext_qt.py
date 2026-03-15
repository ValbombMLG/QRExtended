#!/usr/bin/env python3
"""
QRExtended v3.1 - Qt Edition
Cross-platform GUI for QRExtended with native look and feel
Single-window design with embedded progress and fade transitions
"""

import sys
import os

# Re-launch via pythonw.exe on Windows when running as a .py file,
# so no console window appears on double-click.
# Skipped when running as a frozen .exe (PyInstaller sets sys.frozen).
if sys.platform == "win32" and not getattr(sys, "frozen", False):
    import subprocess
    _exe = sys.executable  # e.g. C:\Python312\python.exe
    if _exe.lower().endswith("python.exe"):
        _pythonw = _exe[:-10] + "pythonw.exe"  # swap python.exe -> pythonw.exe
        if os.path.isfile(_pythonw):
            subprocess.Popen([_pythonw] + sys.argv)
            sys.exit(0)
import glob
import threading
import traceback
import time
from pathlib import Path

try:
    from PySide6.QtWidgets import *
    from PySide6.QtCore import *
    from PySide6.QtGui import *
    Signal = Signal
    Slot = Slot
except ImportError:
    try:
        from PyQt6.QtWidgets import *
        from PyQt6.QtCore import *
        from PyQt6.QtGui import *
        Signal = pyqtSignal
        Slot = pyqtSlot
    except ImportError:
        print("Error: Neither PySide6 nor PyQt6 is installed.")
        print("Please install one of them:")
        print("  pip install PySide6")
        print("  or")
        print("  pip install PyQt6")
        sys.exit(1)

# Import backend modules
from qrext_create import (
    create_qr_plus_from_file, 
    create_qr_plus_from_text,
    create_qr_plus_multipart,
    MAX_WORKER_THREADS
)
from qrext_scan import scan_qr_plus

# Debug mode - when False, suppresses all debug prints and hides the console
def _load_debug_setting():
    """Load persisted debug setting from QSettings."""
    settings = QSettings("VBStudios", "QRExtended")
    return settings.value("debug", False, type=bool)

def _save_debug_setting(value):
    """Persist debug setting to QSettings."""
    settings = QSettings("VBStudios", "QRExtended")
    settings.setValue("debug", value)

def _apply_console_visibility():
    """Detach or reattach the Windows console based on DEBUG flag.
    FreeConsole() fully detaches the process from its console window.
    AllocConsole() creates a new one when debug is turned on at runtime.
    """
    try:
        import platform
        if platform.system() != "Windows":
            return
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if DEBUG:
            # Try to attach to existing console first, allocate new one if none
            if not kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
                kernel32.AllocConsole()
            # Reopen stdout/stderr so print() works again
            import sys
            sys.stdout = open("CONOUT$", "w")
            sys.stderr = open("CONOUT$", "w")
        else:
            kernel32.FreeConsole()
    except Exception:
        pass

def _disable_power_throttling():
    """
    Prevent Windows from throttling the process when minimized.
    Sets HIGH priority and disables EcoQoS/power throttling via SetProcessInformation.
    Safe no-op on non-Windows platforms.
    """
    try:
        import platform
        if platform.system() != "Windows":
            return
        import ctypes, ctypes.wintypes

        kernel32 = ctypes.windll.kernel32

        # Raise process priority to ABOVE_NORMAL so the scheduler doesn't starve us
        # HIGH_PRIORITY_CLASS can interfere with system responsiveness, ABOVE_NORMAL is safer
        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS)

        # Disable power throttling (EcoQoS) - Windows 11 / Server 2022+
        # struct PROCESS_POWER_THROTTLING_STATE {
        #   ULONG Version;       // must be 1
        #   ULONG ControlMask;   // which fields to apply
        #   ULONG StateMask;     // 0 = disable throttling
        # }
        PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1

        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
            _fields_ = [
                ("Version",     ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask",   ctypes.c_ulong),
            ]

        throttle_state = PROCESS_POWER_THROTTLING_STATE()
        throttle_state.Version     = 1
        throttle_state.ControlMask = PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        throttle_state.StateMask   = 0  # 0 = no throttling

        ProcessPowerThrottling = 4  # enum value for SetProcessInformation
        ctypes.windll.kernel32.SetProcessInformation(
            kernel32.GetCurrentProcess(),
            ProcessPowerThrottling,
            ctypes.byref(throttle_state),
            ctypes.sizeof(throttle_state)
        )
    except Exception:
        pass  # Silently ignore on older Windows versions that don't support this


# Load persisted setting before anything else runs
DEBUG = _load_debug_setting()
_apply_console_visibility()
_disable_power_throttling()

# Persistent settings
LAST_OUTPUT_FOLDER = str(Path.home() / "Documents")


class DropLineEdit(QLineEdit):
    """A plain QLineEdit with placeholder styling - drop handling is done at window level."""
    pass

# Executable extensions that should trigger warnings (whitelist approach - only warn on these)
EXECUTABLE_EXTENSIONS = {
    '.exe', '.bat', '.cmd', '.vbs', '.ps1', '.msi', '.sh', 
    '.app', '.dmg', '.pkg', '.deb', '.rpm', '.run',
    '.com', '.scr', '.pif', '.jar', '.application'
}


def play_completion_sound():
    """Play a system sound when operation completes"""
    try:
        import platform
        system = platform.system()
        
        if system == 'Windows':
            import winsound
            winsound.MessageBeep(winsound.MB_OK)
        elif system == 'Darwin':  # macOS
            os.system('afplay /System/Library/Sounds/Glass.aiff &')
        else:  # Linux
            os.system('paplay /usr/share/sounds/freedesktop/stereo/complete.oga &')
    except Exception as e:
        pass  # Sound failure is non-critical
        # Silently fail if sound doesn't work


class WorkerSignals(QObject):
    """Signals for worker threads"""
    progress = Signal(str, int, int, str)  # step, part_num, total_parts, step_text
    finished = Signal(object)  # result (path or list of paths)
    error = Signal(str)  # error message
    cancelled = Signal()


class QRWorker(QRunnable):
    """Worker thread for QR+ operations"""
    
    def __init__(self, operation, *args, **kwargs):
        super().__init__()
        self.operation = operation
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.abort_flag = False
        
    def abort(self):
        self.abort_flag = True
        
    def check_abort(self):
        return self.abort_flag
    
    def progress_callback(self, step, part_num, total_parts, step_text):
        if DEBUG: import builtins; builtins.print(f"DEBUG UI: Progress callback - {step}, {part_num}/{total_parts}, {step_text}")
        self.signals.progress.emit(step, part_num, total_parts, step_text)
    
    @Slot()
    def run(self):
        try:
            # Add progress callback and abort check to kwargs
            self.kwargs['progress_callback'] = self.progress_callback
            self.kwargs['abort_check'] = self.check_abort
            
            result = self.operation(*self.args, **self.kwargs)
            
            if not self.abort_flag:
                self.signals.finished.emit(result)
            else:
                self.signals.cancelled.emit()
                
        except InterruptedError:
            self.signals.cancelled.emit()
        except Exception as e:
            # Print full traceback to console
            if DEBUG:
                import builtins
                builtins.print("=" * 60)
                builtins.print("ERROR IN WORKER:")
                builtins.print("=" * 60)
                traceback.print_exc()
                builtins.print("=" * 60)
            self.signals.error.emit(str(e))


class ProgressWidget(QWidget):
    """Embedded progress widget with ETA, elapsed time, steps, and cancel button"""
    
    cancelRequested = Signal()
    
    def __init__(self, message, parent=None):
        super().__init__(parent)
        self.start_time = time.time()
        self.last_progress = 0
        self.last_update_time = time.time()
        self.eta_samples = []
        self.last_step_text = "Initializing..."
        self._received_first_callback = False

        # Elapsed time timer - updates the label every second, no processEvents()
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.timeout.connect(self._tick_elapsed)
        self.elapsed_timer.start(1000)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(12)
        layout.addStretch()

        # Title
        self.message_label = QLabel(message)
        self.message_label.setStyleSheet("font-size: 16pt; font-weight: bold;")
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.message_label)

        # Step label
        self.step_label = QLabel("Initializing...")
        self.step_label.setStyleSheet("font-size: 11pt;")
        self.step_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.step_label)

        # Progress bar - always determinate (0-1000 internal range for smooth animation)
        # We drive our own indeterminate animation instead of using Qt's max=0 mode,
        # which doesn't respect border-radius on Windows.
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(24)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid #ddd;
                border-radius: 11px;
                background-color: #f0f0f0;
            }
            QProgressBar::chunk {
                background-color: #4CAF50;
                border-radius: 9px;
                margin: 2px;
            }
        """)
        layout.addWidget(self.progress_bar)

        # Indeterminate animation: pulse a chunk back and forth via a timer
        self._indeterminate = True
        self._anim_pos = 0
        self._anim_dir = 1
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick_indeterminate)
        self._anim_timer.start(16)  # ~60fps

        # Status row: percent + ETA on left, elapsed on right
        status_row = QWidget()
        status_layout = QHBoxLayout(status_row)
        status_layout.setContentsMargins(0, 0, 0, 0)

        self.status_label = QLabel("Starting...")
        self.status_label.setStyleSheet("font-size: 10pt; color: #999;")
        status_layout.addWidget(self.status_label)

        status_layout.addStretch()

        self.elapsed_label = QLabel("0:00 elapsed")
        self.elapsed_label.setStyleSheet("font-size: 10pt; color: #999;")
        status_layout.addWidget(self.elapsed_label)

        layout.addWidget(status_row)
        layout.addSpacing(10)

        # Cancel button
        self.cancel_button = QPushButton("Cancel Operation")
        self.cancel_button.setFixedSize(180, 40)
        self.cancel_button.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                border: none;
                padding: 10px 24px;
                font-size: 11pt;
                font-weight: bold;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #d32f2f; }
            QPushButton:disabled { background-color: #cccccc; }
        """)
        self.cancel_button.clicked.connect(self.on_cancel)

        btn_container = QWidget()
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.addWidget(self.cancel_button)
        btn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(btn_container)
        layout.addStretch()

    def _tick_indeterminate(self):
        """Drive custom indeterminate bounce animation."""
        if not self._indeterminate:
            return
        CHUNK = 200   # chunk width in internal units (out of 1000)
        SPEED = 8
        self._anim_pos += self._anim_dir * SPEED
        if self._anim_pos >= 1000 - CHUNK:
            self._anim_pos = 1000 - CHUNK
            self._anim_dir = -1
        elif self._anim_pos <= 0:
            self._anim_pos = 0
            self._anim_dir = 1
        # Shift the visible chunk by setting value; we rely on the chunk margin trick
        # to show only a portion — set both min and value to simulate a sliding window
        self.progress_bar.setMinimum(self._anim_pos)
        self.progress_bar.setMaximum(self._anim_pos + CHUNK)
        self.progress_bar.setValue(self._anim_pos + CHUNK)

    def _tick_elapsed(self):
        """Update elapsed time label every second."""
        elapsed = int(time.time() - self.start_time)
        minutes, seconds = divmod(elapsed, 60)
        self.elapsed_label.setText(f"{minutes}:{seconds:02d} elapsed")

    def stop_timers(self):
        """Stop all timers - call when the widget is no longer active."""
        self._anim_timer.stop()
        self.elapsed_timer.stop()

    def on_cancel(self):
        reply = QMessageBox.question(
            self,
            "Cancel Operation",
            "Are you sure you want to cancel this operation?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.step_label.setText("Cancelling... Please wait.")
            self.step_label.setStyleSheet("font-size: 11pt; color: red;")
            self.cancel_button.setEnabled(False)
            self.cancelRequested.emit()

    def _set_indeterminate(self, enabled):
        """Switch between indeterminate bounce and normal 0-100% mode."""
        self._indeterminate = enabled
        if not enabled:
            self.progress_bar.setMinimum(0)
            self.progress_bar.setMaximum(1000)
            self.progress_bar.setValue(0)

    def reset(self):
        """Reset progress for a new operation."""
        self.start_time = time.time()
        self.last_progress = 0
        self.last_update_time = time.time()
        self.eta_samples = []
        self.last_step_text = "Initializing..."
        self._received_first_callback = False
        self._anim_pos = 0
        self._anim_dir = 1
        self._set_indeterminate(True)
        self.status_label.setText("Starting...")
        self.status_label.setStyleSheet("font-size: 10pt; color: #999;")
        self.step_label.setText("Initializing...")
        self.step_label.setStyleSheet("font-size: 11pt;")
        self.elapsed_label.setText("0:00 elapsed")
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancel Operation")

    def update_progress(self, step, part_num, total_parts, step_text):
        """Update progress bar, step label, and ETA."""
        # Switch from indeterminate bounce to determinate on first real callback
        if not self._received_first_callback:
            self._received_first_callback = True
            self._set_indeterminate(False)

        # Map step + part position to a 0-100 percentage
        if step == "checksum":
            progress = min(5, int((part_num / max(total_parts, 1)) * 5))
        elif step == "compress":
            progress = int(5 + (part_num / max(total_parts, 1)) * 40)
        elif step == "encode":
            progress = int(45 + (part_num / max(total_parts, 1)) * 50)
        elif step == "done":
            progress = int(45 + ((part_num + 1) / max(total_parts, 1)) * 50)
        elif step == "reading":
            progress = int((part_num / max(total_parts, 1)) * 100)
        else:
            progress = 0

        progress = min(100, progress)
        self.progress_bar.setValue(progress * 10)  # internal scale 0-1000
        self.step_label.setText(step_text)

        # ETA calculation
        current_time = time.time()
        if progress > self.last_progress and progress > 0:
            time_diff = current_time - self.last_update_time
            progress_diff = progress - self.last_progress

            if time_diff > 0.1 and progress_diff > 0:
                speed = progress_diff / time_diff
                self.eta_samples.append(speed)
                if len(self.eta_samples) > 5:
                    self.eta_samples.pop(0)

            self.last_update_time = current_time
            self.last_progress = progress

        if self.eta_samples and progress > 0:
            avg_speed = sum(self.eta_samples) / len(self.eta_samples)
            if avg_speed > 0:
                eta_seconds = (100 - progress) / avg_speed
                if eta_seconds < 60:
                    eta_str = f"~{int(eta_seconds)}s remaining"
                elif eta_seconds < 3600:
                    eta_str = f"~{int(eta_seconds / 60)}m {int(eta_seconds % 60)}s remaining"
                else:
                    eta_str = f"~{int(eta_seconds / 3600)}h {int((eta_seconds % 3600) / 60)}m remaining"
                self.status_label.setText(f"{progress}%  —  {eta_str}")
            else:
                self.status_label.setText(f"{progress}%")
        else:
            self.status_label.setText(f"{progress}%")

    def set_message(self, message):
        self.message_label.setText(message)


class CreatorTab(QWidget):
    """Creator tab with single/multi-part modes"""
    
    showProgress = Signal()
    hideProgress = Signal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_worker = None
        self.init_ui()
        
    def init_ui(self):
        # Stacked widget to switch between form, progress, and result
        self.stack = QStackedWidget()
        
        # Form page
        self.form_page = QWidget()
        self.init_form_ui()
        self.stack.addWidget(self.form_page)
        
        # Progress page
        self.progress_page = ProgressWidget("Processing...")
        self.progress_page.cancelRequested.connect(self.on_cancel_requested)
        self.stack.addWidget(self.progress_page)
        
        # Result page
        self.result_page = QWidget()
        self.init_result_ui()
        self.stack.addWidget(self.result_page)
        
        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)
        
    def init_form_ui(self):
        layout = QGridLayout(self.form_page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(5)
        
        # Mode selection
        mode_frame = QWidget()
        mode_layout = QHBoxLayout(mode_frame)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        
        mode_label = QLabel("Input Mode:")
        mode_label.setStyleSheet("font-weight: bold;")
        mode_layout.addWidget(mode_label)
        
        self.mode_group = QButtonGroup()
        self.file_radio = QRadioButton("File")
        self.text_radio = QRadioButton("Text")
        self.file_radio.setChecked(True)
        self.mode_group.addButton(self.file_radio, 0)
        self.mode_group.addButton(self.text_radio, 1)
        
        mode_layout.addWidget(self.file_radio)
        mode_layout.addWidget(self.text_radio)
        mode_layout.addStretch()
        
        layout.addWidget(mode_frame, 0, 0, 1, 3)
        
        self.file_radio.toggled.connect(self.update_mode)
        
        # File input frame
        self.file_frame = QWidget()
        file_layout = QHBoxLayout(self.file_frame)
        file_layout.setContentsMargins(0, 0, 0, 0)
        
        file_layout.addWidget(QLabel("Input File:"))
        self.input_file = DropLineEdit()
        self.input_file.setPlaceholderText("Drop a file here or browse...")
        file_layout.addWidget(self.input_file, 1)
        
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self.browse_input_file)
        file_layout.addWidget(browse_btn)
        
        self.input_file.textChanged.connect(self.validate_input_path)
        
        layout.addWidget(self.file_frame, 1, 0, 1, 3)
        
        # Text input frame
        self.text_frame = QWidget()
        text_layout = QHBoxLayout(self.text_frame)
        text_layout.setContentsMargins(0, 0, 0, 0)
        
        text_layout.addWidget(QLabel("Text Input:"))
        self.input_text = QLineEdit()
        text_layout.addWidget(self.input_text, 1)
        
        layout.addWidget(self.text_frame, 2, 0, 1, 3)
        self.text_frame.hide()
        
        # Multi-part frame
        self.multipart_frame = QWidget()
        mp_layout = QHBoxLayout(self.multipart_frame)
        mp_layout.setContentsMargins(0, 0, 0, 0)
        
        self.multipart_check = QCheckBox("Multi-part mode (split large files)")
        self.multipart_check.toggled.connect(self.update_multipart)
        mp_layout.addWidget(self.multipart_check)
        
        mp_layout.addWidget(QLabel("Split size (MB):"))
        self.split_size = QSpinBox()
        self.split_size.setMinimum(1)
        self.split_size.setMaximum(1000)
        self.split_size.setValue(50)
        self.split_size.setEnabled(False)
        mp_layout.addWidget(self.split_size)
        mp_layout.addStretch()
        
        layout.addWidget(self.multipart_frame, 3, 0, 1, 3)
        
        # Output
        output_layout = QHBoxLayout()
        self.output_label = QLabel("Output Folder:")
        output_layout.addWidget(self.output_label)
        
        self.output_path = QLineEdit()
        self.output_path.setText(LAST_OUTPUT_FOLDER)
        output_layout.addWidget(self.output_path, 1)
        
        # Output filename (optional override)
        output_layout.addWidget(QLabel("Filename:"))
        self.output_filename = QLineEdit()
        self.output_filename.setPlaceholderText("(auto from input)")
        self.output_filename.setMaximumWidth(200)
        output_layout.addWidget(self.output_filename)
        
        self.output_button = QPushButton("Browse")
        self.output_button.clicked.connect(self.browse_output_folder)
        output_layout.addWidget(self.output_button)
        
        layout.addLayout(output_layout, 4, 0, 1, 3)
        
        # Options
        options_label = QLabel("Options:")
        options_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(options_label, 5, 0, 1, 3)
        
        self.autorun_check = QCheckBox("Allow autorun (file will open after scan)")
        self.autorun_check.setChecked(True)
        layout.addWidget(self.autorun_check, 6, 0, 1, 3)
        
        self.delete_source_check = QCheckBox("Delete source file after creation (use with caution!)")
        self.delete_source_check.setStyleSheet("color: red;")
        layout.addWidget(self.delete_source_check, 7, 0, 1, 3)
        
        # Spacer
        layout.setRowStretch(8, 1)
        
        # Create button
        create_btn = QPushButton("Create QR+")
        create_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 8px 30px;
                font-size: 11pt;
                font-weight: bold;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
        """)
        create_btn.clicked.connect(self.on_create)
        layout.addWidget(create_btn, 9, 1, alignment=Qt.AlignmentFlag.AlignCenter)
    
    def init_result_ui(self):
        """Initialize the result/success page"""
        layout = QVBoxLayout(self.result_page)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(20)
        
        layout.addStretch()
        
        # Success icon/title
        self.result_title = QLabel("✅ QR+ Created Successfully!")
        self.result_title.setStyleSheet("font-size: 18pt; font-weight: bold; color: #4CAF50;")
        self.result_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.result_title)
        
        # Result details
        self.result_details = QLabel()
        self.result_details.setStyleSheet("font-size: 11pt;")
        self.result_details.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.result_details.setWordWrap(True)
        layout.addWidget(self.result_details)
        
        layout.addSpacing(20)
        
        # Back button
        back_btn = QPushButton("Create Another QR+")
        back_btn.setFixedSize(200, 40)
        back_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 10px 24px;
                font-size: 11pt;
                font-weight: bold;
                border-radius: 6px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
        """)
        back_btn.clicked.connect(self.switch_to_form)
        
        btn_container = QWidget()
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.addWidget(back_btn)
        btn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(btn_container)
        
        layout.addStretch()
        
    def update_mode(self):
        """Update UI based on selected mode"""
        if self.file_radio.isChecked():
            self.file_frame.show()
            self.text_frame.hide()
            self.multipart_frame.show()
        else:
            self.file_frame.hide()
            self.text_frame.show()
            self.multipart_frame.hide()
            self.multipart_check.setChecked(False)
    
    def update_multipart(self):
        """Update UI based on multipart checkbox"""
        if self.multipart_check.isChecked():
            self.split_size.setEnabled(True)
        else:
            self.split_size.setEnabled(False)
    
    def validate_input_path(self, path=None):
        """Visually validate the input file field."""
        path = path or self.input_file.text()
        if not path:
            self.input_file.setStyleSheet("")
        elif os.path.isfile(path):
            self.input_file.setStyleSheet("border: 2px solid #4CAF50; border-radius: 4px;")
        else:
            self.input_file.setStyleSheet("border: 2px solid #f44336; border-radius: 4px;")

    def browse_input_file(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Select Input File", "", "All Files (*.*)")
        if filename:
            self.input_file.setText(filename)
            # Auto-populate output filename from input
            base_name = os.path.splitext(os.path.basename(filename))[0]
            self.output_filename.setPlaceholderText(f"{base_name}.png")
    
    def browse_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.output_path.setText(folder)
    
    def switch_to_progress(self):
        """Fade to progress view"""
        self.progress_page.reset()
        self.stack.setCurrentWidget(self.progress_page)
        
    def switch_to_form(self):
        """Fade back to form view"""
        self.progress_page.stop_timers()
        self.stack.setCurrentWidget(self.form_page)
    
    def on_cancel_requested(self):
        """Handle cancel request from progress widget"""
        if self.current_worker:
            self.current_worker.abort()
    
    def on_create(self):
        """Handle Create button click"""
        # Validation
        if self.file_radio.isChecked():
            input_path = self.input_file.text()
            if not input_path:
                QMessageBox.critical(self, "Error", "Please select an input file.")
                return
            if not os.path.isfile(input_path):
                QMessageBox.critical(self, "Error", "Input file does not exist.")
                return
        else:
            text_input = self.input_text.text()
            if not text_input:
                QMessageBox.critical(self, "Error", "Please enter some text.")
                return
        
        output_folder = self.output_path.text()
        if not output_folder:
            QMessageBox.critical(self, "Error", "Please specify an output folder.")
            return
        
        # Create output folder if it doesn't exist
        os.makedirs(output_folder, exist_ok=True)
        
        # Switch to progress view
        self.switch_to_progress()
        
        # Create worker based on mode
        if self.file_radio.isChecked():
            input_path = self.input_file.text()
            
            # Determine output filename
            if self.output_filename.text():
                # User provided custom name
                base_name = self.output_filename.text()
                # Remove .png extension if user added it
                if base_name.endswith('.png'):
                    base_name = base_name[:-4]
            else:
                # Use input filename
                base_name = os.path.splitext(os.path.basename(input_path))[0]
            
            # Check if we should use multi-part
            file_size = os.path.getsize(input_path)
            split_size_bytes = self.split_size.value() * 1024 * 1024
            
            if self.multipart_check.isChecked() and file_size > split_size_bytes:
                # Multi-part mode - backend will create files like: name_part001.png, name_part002.png
                self.current_worker = QRWorker(
                    create_qr_plus_multipart,
                    input_path,
                    output_folder,
                    self.split_size.value(),
                    self.autorun_check.isChecked()
                )
                # Note: The backend adds _partXXX automatically, but we need to rename after
                self.pending_rename = (output_folder, base_name, True)  # (folder, base_name, is_multipart)
            else:
                # Single file mode
                output_path = os.path.join(output_folder, f"{base_name}.png")
                self.current_worker = QRWorker(
                    create_qr_plus_from_file,
                    input_path,
                    output_path,
                    self.autorun_check.isChecked()
                )
                self.pending_rename = None
        else:
            # Text mode
            if self.output_filename.text():
                base_name = self.output_filename.text()
                if base_name.endswith('.png'):
                    base_name = base_name[:-4]
            else:
                base_name = "text_qrplus"
            
            output_path = os.path.join(output_folder, f"{base_name}.png")
            self.current_worker = QRWorker(
                create_qr_plus_from_text,
                self.input_text.text(),
                output_path,
                self.autorun_check.isChecked()
            )
            self.pending_rename = None
        
        # Connect signals
        self.current_worker.signals.progress.connect(self.progress_page.update_progress)
        self.current_worker.signals.finished.connect(
            lambda result: self.on_create_finished(result, input_path if self.file_radio.isChecked() else None)
        )
        self.current_worker.signals.error.connect(self.on_create_error)
        self.current_worker.signals.cancelled.connect(self.on_create_cancelled)
        
        # Start worker
        QThreadPool.globalInstance().start(self.current_worker)
    
    def on_create_finished(self, result, input_path):
        """Handle successful creation"""
        # Handle renaming for multi-part with custom names
        if hasattr(self, 'pending_rename') and self.pending_rename is not None:
            output_folder, base_name, is_multipart = self.pending_rename
            
            if is_multipart and isinstance(result, list):
                # Rename files from backend format to our format
                # Backend creates: filename_part001.ext.png, filename_part002.ext.png
                # We want: basename-part-1.png, basename-part-2.png
                renamed_files = []
                for i, old_path in enumerate(result, start=1):
                    new_name = f"{base_name}-part-{i}.png"
                    new_path = os.path.join(output_folder, new_name)
                    try:
                        if old_path != new_path and os.path.exists(old_path):
                            os.rename(old_path, new_path)
                            renamed_files.append(new_path)
                        else:
                            renamed_files.append(old_path)
                    except Exception as e:
                        if DEBUG: import builtins; builtins.print(f"Warning: Could not rename {old_path} to {new_path}: {e}")
                        renamed_files.append(old_path)
                
                result = renamed_files
        
        # BUGFIX: If result is True/False instead of a path, reconstruct the expected path
        if isinstance(result, bool) or result is None:
            if DEBUG: import builtins; builtins.print(f"WARNING: Backend returned {result} instead of path, reconstructing...")
            # Reconstruct the expected output path
            if self.file_radio.isChecked():
                input_path_used = self.input_file.text()
                output_folder = self.output_path.text()
                
                if self.output_filename.text():
                    base_name = self.output_filename.text()
                    if base_name.endswith('.png'):
                        base_name = base_name[:-4]
                else:
                    base_name = os.path.splitext(os.path.basename(input_path_used))[0]
                
                result = os.path.join(output_folder, f"{base_name}.png")
                if DEBUG: import builtins; builtins.print(f"Reconstructed path: {result}")
            else:
                # Text mode
                output_folder = self.output_path.text()
                if self.output_filename.text():
                    base_name = self.output_filename.text()
                    if base_name.endswith('.png'):
                        base_name = base_name[:-4]
                else:
                    base_name = "text_qrplus"
                result = os.path.join(output_folder, f"{base_name}.png")
                if DEBUG: import builtins; builtins.print(f"Reconstructed path: {result}")
        
        # Calculate sizes and ratio
        details_text = ""
        if self.file_radio.isChecked():
            original_size = os.path.getsize(input_path)
            
            if isinstance(result, list):
                # Multi-part - calculate total output size
                total_output_size = 0
                for part_path in result:
                    if os.path.exists(part_path):
                        total_output_size += os.path.getsize(part_path)
                
                ratio = total_output_size / original_size if original_size > 0 else 0
                
                details_text = (
                    f"Parts created: {len(result)}\n"
                    f"Output folder: {os.path.dirname(result[0])}\n\n"
                    f"Original size: {original_size:,} bytes\n"
                    f"Total QR+ size: {total_output_size:,} bytes\n"
                    f"Ratio: {ratio:.2f}x"
                )
            else:
                # Single file
                output_size = 0
                if os.path.exists(result):
                    output_size = os.path.getsize(result)
                
                ratio = output_size / original_size if original_size > 0 else 0
                
                details_text = (
                    f"Output: {os.path.basename(result)}\n"
                    f"Location: {os.path.dirname(result)}\n\n"
                    f"Original size: {original_size:,} bytes\n"
                    f"QR+ size: {output_size:,} bytes\n"
                    f"Ratio: {ratio:.2f}x"
                )
            
            # Delete source if requested
            if self.delete_source_check.isChecked():
                try:
                    os.remove(input_path)
                    details_text += "\n\nSource file deleted"
                except Exception as e:
                    details_text += f"\n\n⚠️ Could not delete source file: {e}"
        else:
            # Text mode
            original_size = len(self.input_text.text())
            output_size = 0
            if isinstance(result, str) and os.path.exists(result):
                output_size = os.path.getsize(result)
            
            ratio = output_size / original_size if original_size > 0 else 0
            
            details_text = (
                f"Output: {os.path.basename(result) if isinstance(result, str) else 'text_qrplus.png'}\n"
                f"Location: {os.path.dirname(result) if isinstance(result, str) else self.output_path.text()}\n\n"
                f"Original size: {original_size:,} bytes\n"
                f"QR+ size: {output_size:,} bytes\n"
                f"Ratio: {ratio:.2f}x"
            )
        
        # Show result page
        self.result_details.setText(details_text)
        self.stack.setCurrentWidget(self.result_page)
        
        # Play completion sound
        play_completion_sound()
    
    def on_create_error(self, error):
        """Handle creation error"""
        self.switch_to_form()
        QMessageBox.critical(self, "Error", f"Failed to create QR+:\n{error}")
    
    def on_create_cancelled(self):
        """Handle cancellation"""
        self.switch_to_form()
        
        # Clean up any partially created files
        if hasattr(self, 'pending_rename') and self.pending_rename is not None:
            output_folder, base_name, is_multipart = self.pending_rename
            if is_multipart:
                # Clean up any partial multi-part files
                import glob
                pattern = os.path.join(output_folder, f"{base_name}-part-*.png")
                for partial_file in glob.glob(pattern):
                    try:
                        os.remove(partial_file)
                        if DEBUG: import builtins; builtins.print(f"Cleaned up partial file: {partial_file}")
                    except Exception as e:
                        if DEBUG: import builtins; builtins.print(f"Could not clean up {partial_file}: {e}")
                
                # Also clean up backend-created files with _partXXX pattern
                if self.file_radio.isChecked():
                    input_path = self.input_file.text()
                    if input_path:
                        input_base = os.path.splitext(os.path.basename(input_path))[0]
                        pattern = os.path.join(output_folder, f"{input_base}_part*.png")
                        for partial_file in glob.glob(pattern):
                            try:
                                os.remove(partial_file)
                                if DEBUG: import builtins; builtins.print(f"Cleaned up partial file: {partial_file}")
                            except Exception as e:
                                if DEBUG: import builtins; builtins.print(f"Could not clean up {partial_file}: {e}")
        
        # Show minimal notification
        QMessageBox.information(self, "Cancelled", "Operation was cancelled. Any partial files have been cleaned up.")


class ScannerTab(QWidget):
    """Scanner tab with single/multi-part support"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_worker = None
        self.init_ui()
        
    def init_ui(self):
        # Stacked widget to switch between form, preview, progress, and result
        self.stack = QStackedWidget()
        
        # Form page
        self.form_page = QWidget()
        self.init_form_ui()
        self.stack.addWidget(self.form_page)
        
        # Preview page (shows file info before scanning)
        self.preview_page = QWidget()
        self.init_preview_ui()
        self.stack.addWidget(self.preview_page)
        
        # Progress page
        self.progress_page = ProgressWidget("Scanning...")
        self.progress_page.cancelRequested.connect(self.on_cancel_requested)
        self.stack.addWidget(self.progress_page)
        
        # Result page
        self.result_page = QWidget()
        self.init_result_ui()
        self.stack.addWidget(self.result_page)
        
        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)
        
    def init_form_ui(self):
        layout = QGridLayout(self.form_page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(5)
        
        # Input
        input_layout = QHBoxLayout()
        input_layout.addWidget(QLabel("QR+ Input:"))
        
        self.input_path = DropLineEdit()
        self.input_path.setPlaceholderText("Drop a QR+ image here or browse...")
        input_layout.addWidget(self.input_path, 1)
        
        input_btn = QPushButton("Browse")
        input_btn.clicked.connect(self.browse_input)
        input_layout.addWidget(input_btn)
        
        self.input_path.textChanged.connect(self.validate_input_path)
        
        layout.addLayout(input_layout, 0, 0, 1, 3)
        
        # Output
        output_layout = QHBoxLayout()
        output_layout.addWidget(QLabel("Output Folder:"))
        
        self.output_path = QLineEdit()
        self.output_path.setText(LAST_OUTPUT_FOLDER)
        output_layout.addWidget(self.output_path, 1)
        
        output_btn = QPushButton("Browse")
        output_btn.clicked.connect(self.browse_output)
        output_layout.addWidget(output_btn)
        
        layout.addLayout(output_layout, 1, 0, 1, 3)
        
        # Options
        options_label = QLabel("Options:")
        options_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(options_label, 2, 0, 1, 3)
        
        self.preview_check = QCheckBox("Show file preview before scanning (recommended)")
        self.preview_check.setChecked(True)
        layout.addWidget(self.preview_check, 3, 0, 1, 3)
        
        self.run_check = QCheckBox("Auto-run after scan (unsafe for unknown files)")
        self.run_check.setStyleSheet("color: red;")
        layout.addWidget(self.run_check, 4, 0, 1, 3)
        
        self.skip_check = QCheckBox("Skip checksum verification (unsafe)")
        self.skip_check.setStyleSheet("color: red;")
        layout.addWidget(self.skip_check, 5, 0, 1, 3)
        
        self.delete_qr_check = QCheckBox("Delete QR+ after successful scan")
        self.delete_qr_check.setStyleSheet("color: orange;")
        layout.addWidget(self.delete_qr_check, 6, 0, 1, 3)
        
        # Spacer
        layout.setRowStretch(7, 1)
        
        # Scan button
        scan_btn = QPushButton("Scan QR+")
        scan_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                padding: 8px 30px;
                font-size: 11pt;
                font-weight: bold;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
        """)
        scan_btn.clicked.connect(self.on_scan)
        layout.addWidget(scan_btn, 8, 1, alignment=Qt.AlignmentFlag.AlignCenter)
    
    def init_preview_ui(self):
        """Initialize the preview page that shows file metadata"""
        layout = QVBoxLayout(self.preview_page)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(20)
        
        layout.addStretch()
        
        # Title
        preview_title = QLabel("📋 File Preview")
        preview_title.setStyleSheet("font-size: 18pt; font-weight: bold; color: #2196F3;")
        preview_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(preview_title)
        
        # Info text
        info_text = QLabel("This QR+ image contains the following file:")
        info_text.setStyleSheet("font-size: 11pt;")
        info_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info_text)
        
        layout.addSpacing(10)
        
        # Metadata frame
        self.preview_metadata_frame = QWidget()
        self.preview_metadata_frame.setObjectName("previewMetadataFrame")
        self.preview_metadata_frame.setStyleSheet("""
            QWidget#previewMetadataFrame {
                background-color: rgba(33, 150, 243, 0.1);
                border: 2px solid #2196F3;
                border-radius: 8px;
            }
        """)
        metadata_layout = QVBoxLayout(self.preview_metadata_frame)
        metadata_layout.setContentsMargins(20, 16, 20, 16)
        metadata_layout.setSpacing(6)

        # Line 1: Filename
        self.preview_filename_label = QLabel()
        self.preview_filename_label.setStyleSheet(
            "font-size: 13pt; font-weight: bold; border: none; background: transparent;"
        )
        self.preview_filename_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_filename_label.setWordWrap(True)
        metadata_layout.addWidget(self.preview_filename_label)

        # Line 2: Extension type
        self.preview_extension_label = QLabel()
        self.preview_extension_label.setStyleSheet(
            "font-size: 11pt; color: #555; border: none; background: transparent;"
        )
        self.preview_extension_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_extension_label.setWordWrap(True)
        metadata_layout.addWidget(self.preview_extension_label)

        # Line 3: File size
        self.preview_size_label = QLabel()
        self.preview_size_label.setStyleSheet(
            "font-size: 10pt; color: #888; border: none; background: transparent;"
        )
        self.preview_size_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        metadata_layout.addWidget(self.preview_size_label)

        layout.addWidget(self.preview_metadata_frame)
        
        # Warning if autorun is enabled
        self.preview_warning_frame = QWidget()
        self.preview_warning_frame.setObjectName("previewWarningFrame")
        self.preview_warning_frame.setStyleSheet("""
            QWidget#previewWarningFrame {
                background-color: rgba(244, 67, 54, 0.1);
                border: 2px solid #f44336;
                border-radius: 8px;
            }
        """)
        warning_layout = QVBoxLayout(self.preview_warning_frame)
        warning_layout.setContentsMargins(12, 10, 12, 10)
        warning_icon = QLabel("⚠️ WARNING")
        warning_icon.setStyleSheet("font-size: 14pt; font-weight: bold; color: #f44336; border: none; background: transparent;")
        warning_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        warning_layout.addWidget(warning_icon)
        
        self.preview_warning_text = QLabel()
        self.preview_warning_text.setStyleSheet("font-size: 10pt; color: #d32f2f; border: none; background: transparent;")
        self.preview_warning_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_warning_text.setWordWrap(True)
        warning_layout.addWidget(self.preview_warning_text)
        
        layout.addWidget(self.preview_warning_frame)
        self.preview_warning_frame.hide()  # Hidden by default
        
        layout.addSpacing(10)
        
        # Buttons
        btn_container = QWidget()
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.setSpacing(15)
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedSize(120, 40)
        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #e0e0e0;
                color: #333;
                border: none;
                padding: 10px 24px;
                font-size: 11pt;
                font-weight: bold;
                border-radius: 6px;
            }
            QPushButton:hover {
                background-color: #bdbdbd;
                color: #111;
            }
        """)
        cancel_btn.clicked.connect(self.switch_to_form)
        btn_layout.addWidget(cancel_btn)
        
        proceed_btn = QPushButton("Proceed with Scan")
        proceed_btn.setFixedSize(180, 40)
        proceed_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                padding: 10px 24px;
                font-size: 11pt;
                font-weight: bold;
                border-radius: 6px;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
        """)
        proceed_btn.clicked.connect(self.on_preview_proceed)
        btn_layout.addWidget(proceed_btn)
        
        btn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(btn_container)
        
        layout.addStretch()
    
    def init_result_ui(self):
        """Initialize the result/success page"""
        layout = QVBoxLayout(self.result_page)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(20)
        
        layout.addStretch()
        
        # Success icon/title
        self.result_title = QLabel("✅ QR+ Scanned Successfully!")
        self.result_title.setStyleSheet("font-size: 18pt; font-weight: bold; color: #2196F3;")
        self.result_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.result_title)
        
        # Result details
        self.result_details = QLabel()
        self.result_details.setStyleSheet("font-size: 11pt;")
        self.result_details.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.result_details.setWordWrap(True)
        layout.addWidget(self.result_details)
        
        layout.addSpacing(20)
        
        # Back button
        back_btn = QPushButton("Scan Another QR+")
        back_btn.setFixedSize(200, 40)
        back_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                padding: 10px 24px;
                font-size: 11pt;
                font-weight: bold;
                border-radius: 6px;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
        """)
        back_btn.clicked.connect(self.switch_to_form)
        
        btn_container = QWidget()
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.addWidget(back_btn)
        btn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(btn_container)
        
        layout.addStretch()
    
    def validate_input_path(self, path=None):
        """Visually validate the input file field."""
        path = path or self.input_path.text()
        if not path:
            self.input_path.setStyleSheet("")
        elif os.path.isfile(path):
            self.input_path.setStyleSheet("border: 2px solid #4CAF50; border-radius: 4px;")
        else:
            self.input_path.setStyleSheet("border: 2px solid #f44336; border-radius: 4px;")

    def browse_input(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Select QR+ Image", "", "PNG Image (*.png);;All Files (*.*)"
        )
        if filename:
            self.input_path.setText(filename)
    
    def browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.output_path.setText(folder)
    
    def switch_to_progress(self):
        """Fade to progress view"""
        self.progress_page.reset()
        self.stack.setCurrentWidget(self.progress_page)
        
    def switch_to_form(self):
        """Fade back to form view"""
        self.progress_page.stop_timers()
        self.stack.setCurrentWidget(self.form_page)
    
    def on_cancel_requested(self):
        """Handle cancel request from progress widget"""
        if self.current_worker:
            self.current_worker.abort()
    
    def on_scan(self):
        """Handle Scan button click - validate inputs and show preview if enabled"""
        input_file = self.input_path.text()
        output_folder = self.output_path.text()
        
        if not input_file or not output_folder:
            QMessageBox.critical(self, "Error", "Please select both input image and output folder.")
            return
        
        if not os.path.isfile(input_file):
            QMessageBox.critical(self, "Error", "Input file does not exist.")
            return
        
        # Store scan parameters for later use
        self.pending_scan_input = input_file
        self.pending_scan_output = output_folder
        
        # If preview is enabled, show preview screen
        if self.preview_check.isChecked():
            self.show_preview()
        else:
            # Skip preview and go directly to scanning
            self.start_scan()
    
    def show_preview(self):
        """Show preview screen - loads metadata in background thread to keep UI responsive."""
        # Switch to preview page immediately with a loading state
        self.preview_filename_label.setText("Loading...")
        self.preview_extension_label.setText("")
        self.preview_size_label.setText("")
        self.preview_warning_frame.hide()
        self.stack.setCurrentWidget(self.preview_page)

        input_file = self.pending_scan_input

        class PreviewWorker(QRunnable):
            def __init__(self, path):
                super().__init__()
                self.path = path
                self.signals = WorkerSignals()

            @Slot()
            def run(self):
                try:
                    from PIL import Image
                    from qrext_scan import read_metadata_v2, verify_stub, read_data_snake_v2_streaming
                    import tempfile

                    img = Image.open(self.path)
                    stub_w, stub_h = verify_stub(img)
                    (canvas_size, data_byte_count, compression_flag,
                     original_size, checksum_original, autorun_flag,
                     part_num, total_parts) = read_metadata_v2(img, stub_w, stub_h)

                    # Extract just enough data to decode the header
                    import tempfile as _tf
                    _tmp = _tf.NamedTemporaryFile(delete=False)
                    temp_output = _tmp.name
                    _tmp.close()
                    filename = "Unknown"
                    try:
                        read_data_snake_v2_streaming(img, canvas_size, stub_w, stub_h,
                                                     min(1024, data_byte_count), temp_output,
                                                     None, None)
                        with open(temp_output, 'rb') as f:
                            header_data = f.read(1024)
                        if compression_flag:
                            import zlib
                            try:
                                header_data = zlib.decompress(header_data)
                            except Exception:
                                pass
                        fname_len = int.from_bytes(header_data[0:2], 'big')
                        filename = header_data[2:2+fname_len].decode('utf-8', errors='replace')
                        os.remove(temp_output)
                    except Exception:
                        pass

                    img.close()

                    result = {
                        'filename': filename,
                        'original_size': original_size,
                        'compression_flag': compression_flag,
                        'autorun_flag': autorun_flag,
                        'part_num': part_num,
                        'total_parts': total_parts,
                    }
                    self.signals.finished.emit(result)

                except Exception as e:
                    self.signals.error.emit(str(e))

        worker = PreviewWorker(input_file)
        worker.signals.finished.connect(self._on_preview_loaded)
        worker.signals.error.connect(self._on_preview_error)
        QThreadPool.globalInstance().start(worker)

    def _on_preview_loaded(self, result):
        """Populate preview UI once metadata worker finishes."""
        filename    = result['filename']
        original_size = result['original_size']
        autorun_flag  = result['autorun_flag']
        part_num      = result['part_num']
        total_parts   = result['total_parts']

        _, ext = os.path.splitext(filename)
        ext_lower = ext.lower()

        ext_meanings = {
            '.txt':'Text Document','.md':'Markdown Document','.rtf':'Rich Text Document',
            '.pdf':'PDF Document','.doc':'Word Document','.docx':'Word Document',
            '.xls':'Excel Spreadsheet','.xlsx':'Excel Spreadsheet','.csv':'CSV Spreadsheet',
            '.ppt':'PowerPoint Presentation','.pptx':'PowerPoint Presentation',
            '.odt':'OpenDocument Text','.ods':'OpenDocument Spreadsheet','.odp':'OpenDocument Presentation',
            '.png':'PNG Image','.jpg':'JPEG Image','.jpeg':'JPEG Image','.gif':'GIF Image',
            '.bmp':'Bitmap Image','.svg':'Vector Image','.webp':'WebP Image',
            '.tiff':'TIFF Image','.tif':'TIFF Image','.ico':'Icon File','.heic':'HEIC Image','.raw':'RAW Image',
            '.mp3':'MP3 Audio','.wav':'WAV Audio','.flac':'FLAC Audio','.aac':'AAC Audio',
            '.ogg':'OGG Audio','.wma':'Windows Media Audio','.m4a':'M4A Audio',
            '.mp4':'MP4 Video','.avi':'AVI Video','.mkv':'MKV Video','.mov':'QuickTime Video',
            '.wmv':'Windows Media Video','.flv':'Flash Video','.webm':'WebM Video','.m4v':'M4V Video',
            '.veg':'Vegas Pro Project',
            '.zip':'ZIP Archive','.rar':'RAR Archive','.7z':'7-Zip Archive','.tar':'TAR Archive',
            '.gz':'GZip Archive','.bz2':'BZip2 Archive','.xz':'XZ Archive',
            '.cab':'Windows Cabinet Archive','.iso':'Disk Image',
            '.exe':'Executable Program','.msi':'Windows Installer','.app':'macOS Application',
            '.dmg':'macOS Disk Image','.pkg':'macOS Package','.deb':'Debian Package',
            '.rpm':'RPM Package','.appimage':'Linux AppImage','.com':'DOS Executable','.scr':'Windows Screensaver',
            '.sh':'Shell Script','.bash':'Bash Script','.bat':'Batch Script','.cmd':'Command Script',
            '.ps1':'PowerShell Script','.vbs':'VBScript','.py':'Python Script','.js':'JavaScript File',
            '.ts':'TypeScript File','.rb':'Ruby Script','.pl':'Perl Script','.lua':'Lua Script',
            '.r':'R Script','.html':'HTML Document','.htm':'HTML Document','.css':'Stylesheet',
            '.php':'PHP Script','.asp':'ASP Script',
            '.json':'JSON Data','.xml':'XML Document','.yaml':'YAML Config','.yml':'YAML Config',
            '.toml':'TOML Config','.ini':'Config File','.cfg':'Config File','.conf':'Config File',
            '.reg':'Registry File','.env':'Environment Config','.properties':'Properties File',
            '.dll':'Windows Library','.so':'Linux Shared Library','.dylib':'macOS Library',
            '.sys':'System Driver','.drv':'Device Driver','.bin':'Binary File','.dat':'Data File',
            '.db':'Database File','.sqlite':'SQLite Database','.sql':'SQL Script','.log':'Log File','.tmp':'Temporary File',
            '.c':'C Source File','.cpp':'C++ Source File','.h':'C/C++ Header',
            '.cs':'C# Source File','.java':'Java Source File','.class':'Java Class File','.jar':'Java Archive',
            '.go':'Go Source File','.rs':'Rust Source File','.swift':'Swift Source File','.kt':'Kotlin Source File',
            '.blend':'Blender Project','.fbx':'3D Model (FBX)','.obj':'3D Model (OBJ)','.stl':'3D Model (STL)',
            '.gltf':'3D Model (glTF)','.glb':'3D Model (GLB)','.psd':'Photoshop Document',
            '.ai':'Adobe Illustrator File','.aep':'After Effects Project','.prproj':'Premiere Pro Project',
            '.nk':'Nuke Comp File','.dwg':'AutoCAD Drawing','.dxf':'CAD Exchange File',
            '.pak':'Game Data Package','.wad':'Game Data File','.vpk':'Valve Package File',
            '.bsp':'Map File','.sav':'Save File','.rom':'ROM Image',
            '.gba':'Game Boy Advance ROM','.nds':'Nintendo DS ROM','.nes':'NES ROM','.sfc':'SNES ROM',
            '.ttf':'TrueType Font','.otf':'OpenType Font','.woff':'Web Font','.woff2':'Web Font',
        }
        ext_meaning = ext_meanings.get(ext_lower, 'Unknown File Type')

        if original_size < 1024:
            size_str = f"{original_size} bytes"
        elif original_size < 1024 * 1024:
            size_str = f"{original_size / 1024:.1f} KB"
        elif original_size < 1024 * 1024 * 1024:
            size_str = f"{original_size / (1024 * 1024):.1f} MB"
        else:
            size_str = f"{original_size / (1024 * 1024 * 1024):.2f} GB"

        filename_display = filename
        if total_parts > 1:
            filename_display += f"  [Part {part_num + 1} of {total_parts}]"
        self.preview_filename_label.setText(filename_display)

        extension_display = f"{ext if ext else 'No extension'}  —  {ext_meaning}"
        self.preview_extension_label.setText(extension_display)
        self.preview_size_label.setText(f"Extracted size: {size_str}")

        if autorun_flag and self.run_check.isChecked():
            self.preview_warning_frame.show()
            self.preview_warning_text.setText(
                "This file has AUTORUN enabled and you have chosen to run it after extraction.\n"
                "The file will open automatically after scanning.\n"
                "Only proceed if you trust the source!"
            )
        elif ext_lower in EXECUTABLE_EXTENSIONS:
            self.preview_warning_frame.show()
            self.preview_warning_text.setText(
                f"This is an executable file ({ext_meaning}).\n"
                "Be cautious when opening files from unknown sources!"
            )
        else:
            self.preview_warning_frame.hide()

    def _on_preview_error(self, error):
        """Handle preview metadata load failure."""
        self.switch_to_form()
        QMessageBox.critical(self, "Preview Error",
            f"Could not read QR+ metadata:\n{error}\n\nThe file may be corrupted or not a valid QR+ image.")
    def on_preview_proceed(self):
        """User clicked 'Proceed' on preview screen"""
        self.start_scan()
    
    def start_scan(self):
        """Actually start the scanning process"""
        input_file = self.pending_scan_input
        output_folder = self.pending_scan_output

        # Switch to progress view
        self.switch_to_progress()

        # Dialog callbacks — must marshal to main thread since they show Qt UI
        def confirm_cb(title, message):
            """Called from worker thread — blocks until user responds."""
            result = [False]
            event  = __import__('threading').Event()
            def _show():
                result[0] = QMessageBox.question(
                    None, title, message,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                ) == QMessageBox.StandardButton.Yes
                event.set()
            QTimer.singleShot(0, _show)
            event.wait()
            return result[0]

        def error_cb(title, message):
            """Called from worker thread — fire and forget."""
            QTimer.singleShot(0, lambda: QMessageBox.critical(None, title, message))

        # Create worker
        self.current_worker = QRWorker(
            scan_qr_plus,
            input_file,
            output_folder,
            self.run_check.isChecked(),
            self.skip_check.isChecked(),
            self.delete_qr_check.isChecked(),
            confirm_cb=confirm_cb,
            error_cb=error_cb,
        )
        
        # Connect signals
        self.current_worker.signals.progress.connect(self.progress_page.update_progress)
        self.current_worker.signals.finished.connect(self.on_scan_finished)
        self.current_worker.signals.error.connect(self.on_scan_error)
        self.current_worker.signals.cancelled.connect(self.on_scan_cancelled)
        
        # Start worker
        QThreadPool.globalInstance().start(self.current_worker)
    
    def on_scan_finished(self, result):
        """Handle successful scan"""
        output_size = 0
        if os.path.exists(result):
            output_size = os.path.getsize(result)
        
        details_text = (
            f"Output: {os.path.basename(result)}\n"
            f"Location: {os.path.dirname(result)}\n\n"
            f"Extracted size: {output_size:,} bytes"
        )
        
        # Show result page
        self.result_details.setText(details_text)
        self.stack.setCurrentWidget(self.result_page)
        
        # Play completion sound
        play_completion_sound()
    
    def on_scan_error(self, error):
        """Handle scan error"""
        self.switch_to_form()
        QMessageBox.critical(self, "Error", f"Failed to scan QR+:\n{error}")
    
    def on_scan_cancelled(self):
        """Handle cancellation"""
        self.switch_to_form()
        QMessageBox.information(self, "Cancelled", "Operation was cancelled by user.")


class CreditsDialog(QDialog):
    """Credits popup dialog"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Credits")
        self.setFixedSize(340, 280)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(0)

        title = QLabel("Credits")
        title.setStyleSheet("font-size: 20pt; font-weight: bold;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addSpacing(24)

        credits_data = [
            ("Designer", "ValbombMLG"),
            ("Prototyping", "Nova Steele"),
            ("Programming", "Ash Claude"),
        ]

        for role, name in credits_data:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)

            role_label = QLabel(f"{role}:")
            role_label.setStyleSheet("font-size: 11pt; font-weight: bold;")
            role_label.setFixedWidth(120)
            role_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row_layout.addWidget(role_label)
            row_layout.addSpacing(16)

            name_label = QLabel(name)
            name_label.setStyleSheet("font-size: 11pt;")
            row_layout.addWidget(name_label)
            row_layout.addStretch()

            layout.addWidget(row)
            layout.addSpacing(10)

        layout.addSpacing(16)

        studio = QLabel("VB Studios")
        studio.setStyleSheet("font-size: 12pt; font-weight: bold; color: gray;")
        studio.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(studio)
        layout.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setFixedSize(100, 34)
        close_btn.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)


class MainWindow(QMainWindow):
    """Main application window"""
    
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self._base_style = ""
        self.init_ui()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls and os.path.isfile(urls[0].toLocalFile()):
                event.acceptProposedAction()
                self.centralWidget().setStyleSheet(
                    "QWidget#central { border: 2px solid #4CAF50; background-color: rgba(76, 175, 80, 0.05); }"
                )
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        self.centralWidget().setStyleSheet("")

    def dropEvent(self, event):
        self.centralWidget().setStyleSheet("")
        if event.mimeData().hasUrls():
            path = event.mimeData().urls()[0].toLocalFile()
            if os.path.isfile(path):
                current_tab = self.tabs.currentIndex()
                if current_tab == 0:
                    self.creator_tab.input_file.setText(path)
                    self.creator_tab.validate_input_path(path)
                else:
                    self.scanner_tab.input_path.setText(path)
                    self.scanner_tab.validate_input_path(path)
                event.acceptProposedAction()
        
    def init_ui(self):
        self.setWindowTitle("QRExtended v3.1")
        self.setFixedSize(750, 470)

        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 6)

        self.tabs = QTabWidget()
        self.creator_tab = CreatorTab()
        self.scanner_tab = ScannerTab()
        self.tabs.addTab(self.creator_tab, "  Creator  ")
        self.tabs.addTab(self.scanner_tab, "  Scanner  ")
        layout.addWidget(self.tabs)

        # Footer
        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(4, 2, 4, 2)
        footer_layout.setSpacing(8)

        def make_link(text, url):
            lbl = QLabel(f'<a href="{url}" style="color:#2196F3;text-decoration:none;">{text}</a>')
            lbl.setStyleSheet("font-size: 8pt;")
            lbl.setOpenExternalLinks(True)
            lbl.setTextFormat(Qt.TextFormat.RichText)
            return lbl

        # Left: GitHub + Ko-fi
        footer_layout.addWidget(make_link("GitHub", "https://github.com/ValbombMLG/QRExtended"))
        sep1 = QLabel("·")
        sep1.setStyleSheet("font-size: 8pt; color: gray;")
        footer_layout.addWidget(sep1)
        footer_layout.addWidget(make_link("Ko-fi", "https://ko-fi.com/valbombmlg"))
        footer_layout.addStretch()

        # Centre: version + credits link
        version_label = QLabel("QRExtended v3.1")
        version_label.setStyleSheet("font-size: 8pt; color: gray;")
        footer_layout.addWidget(version_label)
        sep2 = QLabel("·")
        sep2.setStyleSheet("font-size: 8pt; color: gray;")
        footer_layout.addWidget(sep2)
        credits_link = QLabel('<a href="#" style="color:#2196F3;text-decoration:none;">Credits</a>')
        credits_link.setStyleSheet("font-size: 8pt;")
        credits_link.setTextFormat(Qt.TextFormat.RichText)
        credits_link.setCursor(Qt.CursorShape.PointingHandCursor)
        credits_link.mousePressEvent = lambda e: CreditsDialog(self).exec()
        footer_layout.addWidget(credits_link)
        footer_layout.addStretch()

        # Right: Debug toggle
        self.debug_check = QCheckBox("Debug")
        self.debug_check.setStyleSheet("font-size: 8pt; color: gray;")
        self.debug_check.setChecked(DEBUG)
        self.debug_check.stateChanged.connect(self._on_debug_toggled)
        footer_layout.addWidget(self.debug_check)

        layout.addWidget(footer)
        self.center_on_screen()

    def _on_debug_toggled(self, state):
        global DEBUG
        DEBUG = bool(state)
        _save_debug_setting(DEBUG)
        _apply_console_visibility()
    
    def center_on_screen(self):
        screen = QApplication.primaryScreen().geometry()
        size = self.geometry()
        self.move(
            (screen.width() - size.width()) // 2,
            (screen.height() - size.height()) // 2
        )


def main():
    app = QApplication(sys.argv)
    
    # Set application metadata
    app.setApplicationName("QRExtended")
    app.setOrganizationName("QRExtended")
    app.setApplicationVersion("3.1")
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()