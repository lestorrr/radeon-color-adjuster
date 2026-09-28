#!/usr/bin/env python3
import sys
import os
import json
import subprocess
import re

# Standard paths
CONFIG_DIR = os.path.expanduser("~/.config")
CONFIG_FILE = os.path.join(CONFIG_DIR, "radeon_color_adjuster.json")
AUTOSTART_DIR = os.path.expanduser("~/.config/autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "radeon-color-adjuster.desktop")

# ----------------- MATRIX MATH FOR SATURATION AND VIBRANCE -----------------
def get_saturation_matrix(s):
    # BT.709 coefficients
    wr, wg, wb = 0.2126, 0.7152, 0.0722
    
    # Row 1
    m00 = (1.0 - s) * wr + s
    m01 = (1.0 - s) * wg
    m02 = (1.0 - s) * wb
    
    # Row 2
    m10 = (1.0 - s) * wr
    m11 = (1.0 - s) * wg + s
    m12 = (1.0 - s) * wb
    
    # Row 3
    m20 = (1.0 - s) * wr
    m21 = (1.0 - s) * wg
    m22 = (1.0 - s) * wb + s
    
    return [
        m00, m01, m02,
        m10, m11, m12,
        m20, m21, m22
    ]

def get_vibrance_matrix(v):
    # BT.709 coefficients
    wr, wg, wb = 0.2126, 0.7152, 0.0722
    
    # Channel-specific scaling factors
    # Red is boosted less (0.6) to preserve skin tones, green standard (1.0), blue more (1.2)
    sr = 1.0 + (v - 1.0) * 0.6
    sg = 1.0 + (v - 1.0) * 1.0
    sb = 1.0 + (v - 1.0) * 1.2
    
    # Row 1
    m00 = (1.0 - sr) * wr + sr
    m01 = (1.0 - sr) * wg
    m02 = (1.0 - sr) * wb
    
    # Row 2
    m10 = (1.0 - sg) * wr
    m11 = (1.0 - sg) * wg + sg
    m12 = (1.0 - sg) * wb
    
    # Row 3
    m20 = (1.0 - sb) * wr
    m21 = (1.0 - sb) * wg
    m22 = (1.0 - sb) * wb + sb
    
    return [
        m00, m01, m02,
        m10, m11, m12,
        m20, m21, m22
    ]

def multiply_matrices(A, B):
    C = [0.0] * 9
    for i in range(3):
        for j in range(3):
            val = 0.0
            for k in range(3):
                val += A[i*3 + k] * B[k*3 + j]
            C[i*3 + j] = val
    return C

def float_to_ctm_pair(val):
    # DRM color management properties expect S31.32 sign-magnitude format
    is_negative = val < 0
    abs_val = abs(val)
    
    # Magnitude is represented in the lower 63 bits of the 64-bit integer
    magnitude = int(round(abs_val * 4294967296.0))
    
    # Clamp to fit in 63 bits
    max_magnitude = (1 << 63) - 1
    if magnitude > max_magnitude:
        magnitude = max_magnitude
        
    n = magnitude
    if is_negative:
        n |= (1 << 63) # Bit 63 is the sign bit
        
    low_32 = n & 0xFFFFFFFF
    high_32 = (n >> 32) & 0xFFFFFFFF
    return low_32, high_32

def matrix_to_ctm_string(matrix):
    pairs = []
    for val in matrix:
        low, high = float_to_ctm_pair(val)
        pairs.extend([low, high])
    return ",".join(map(str, pairs))

# ----------------- SYSTEM CONTROLS -----------------
def get_connected_displays():
    try:
        output = subprocess.check_output("xrandr --current", shell=True).decode()
        displays = []
        for line in output.splitlines():
            if " connected" in line:
                match = re.match(r"^([a-zA-Z0-9\-\:\.]+)\s+connected", line)
                if match:
                    displays.append(match.group(1))
        return displays
    except Exception as e:
        print("Error getting displays:", e)
        return ["default"]

def check_ctm_support(display):
    try:
        output = subprocess.check_output("xrandr --prop", shell=True).decode()
        lines = output.splitlines()
        is_target = False
        for line in lines:
            if " connected" in line:
                name = line.split()[0]
                is_target = (name == display)
            elif is_target:
                if "CTM:" in line:
                    return True
                elif not line.startswith("\t") and not line.startswith(" "):
                    is_target = False
        return False
    except Exception as e:
        print("Error checking CTM support:", e)
        return False

def apply_display_settings(display, enabled, brightness, temp, r_gamma, g_gamma, b_gamma, saturation=1.0, vibrance=1.0, contrast=1.0, has_ctm=True):
    if not enabled:
        # Reset basic controls
        cmd = f"xrandr --output {display} --brightness 1.00 --gamma 1.000:1.000:1.000"
        try:
            subprocess.run(cmd, shell=True, check=True)
        except Exception as e:
            print(f"Failed to reset basics: {e}")
            
        # Reset color transformation matrix
        if has_ctm:
            ctm_cmd = f"xrandr --output {display} --set CTM 0,1,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,1"
            try:
                subprocess.run(ctm_cmd, shell=True, check=True)
            except Exception as e:
                print(f"Failed to reset CTM matrix: {e}")
    else:
        # Calculate temperature gains (Kelvin to RGB)
        r_gain, g_gain, b_gain = 1.0, 1.0, 1.0
        if temp < 6500:
            ratio = (temp - 4000) / 2500.0
            r_gain = 1.0
            g_gain = 0.76 + 0.24 * ratio
            b_gain = 0.45 + 0.55 * ratio
        else:
            ratio = (temp - 6500) / 3500.0
            r_gain = 1.0 - 0.28 * ratio
            g_gain = 1.0 - 0.08 * ratio
            b_gain = 1.0

        # Adjust gamma values by the temperature gains and the contrast factor
        final_r = (r_gamma * contrast) / r_gain
        final_g = (g_gamma * contrast) / g_gain
        final_b = (b_gamma * contrast) / b_gain

        # 1. Apply brightness and gamma parameters
        cmd = f"xrandr --output {display} --brightness {brightness:.2f} --gamma {final_r:.3f}:{final_g:.3f}:{final_b:.3f}"
        try:
            subprocess.run(cmd, shell=True, check=True)
        except Exception as e:
            print(f"Failed to apply settings to {display}: {e}")

        # 2. Apply CTM Matrix if supported
        if has_ctm:
            m_sat = get_saturation_matrix(saturation)
            m_vib = get_vibrance_matrix(vibrance)
            m_final = multiply_matrices(m_sat, m_vib)
            ctm_str = matrix_to_ctm_string(m_final)
            ctm_cmd = f"xrandr --output {display} --set CTM {ctm_str}"
            try:
                subprocess.run(ctm_cmd, shell=True, check=True)
            except Exception as e:
                print(f"Failed to apply CTM matrix to {display}: {e}")

# ----------------- CONFIGURATION MANAGEMENT -----------------
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print("Error loading config:", e)
    return {
        "displays": {},
        "autostart": False
    }

def save_config(config):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        print("Error saving config:", e)

def set_autostart_enabled(enabled):
    if enabled:
        try:
            os.makedirs(AUTOSTART_DIR, exist_ok=True)
            script_path = os.path.abspath(__file__)
            python_exec = os.path.abspath(os.path.join(os.path.dirname(script_path), "venv/bin/python"))
            if not os.path.exists(python_exec):
                python_exec = "python3"

            content = f"""[Desktop Entry]
Type=Application
Name=Radeon Color Adjuster Startup
Comment=Apply saved monitor display color calibrations on session login
Exec={python_exec} {script_path} --apply
Hidden=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
"""
            with open(AUTOSTART_FILE, 'w') as f:
                f.write(content)
            os.chmod(AUTOSTART_FILE, 0o755)
        except Exception as e:
            print("Error creating autostart:", e)
    else:
        if os.path.exists(AUTOSTART_FILE):
            try:
                os.remove(AUTOSTART_FILE)
            except Exception as e:
                print("Error removing autostart:", e)

# ----------------- COMMAND LINE APPLY MODE -----------------
if len(sys.argv) > 1 and sys.argv[1] in ["--apply", "-a"]:
    config = load_config()
    connected = get_connected_displays()
    
    for display in connected:
        if display in config["displays"]:
            d_cfg = config["displays"][display]
            has_ctm = check_ctm_support(display)
            apply_display_settings(
                display=display,
                enabled=d_cfg.get("enabled", True),
                brightness=d_cfg.get("brightness", 1.0),
                temp=d_cfg.get("temperature", 6500),
                r_gamma=d_cfg.get("r_gamma", 1.0),
                g_gamma=d_cfg.get("g_gamma", 1.0),
                b_gamma=d_cfg.get("b_gamma", 1.0),
                saturation=d_cfg.get("saturation", 1.0),
                vibrance=d_cfg.get("vibrance", 1.0),
                contrast=d_cfg.get("contrast", 1.0),
                has_ctm=has_ctm
            )
    sys.exit(0)

# ----------------- GUI APPLICATION (PyQt5) -----------------
try:
    from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                                 QHBoxLayout, QLabel, QSlider, QCheckBox, QComboBox, 
                                 QPushButton, QFrame, QSizePolicy, QGridLayout)
    from PyQt5.QtCore import Qt, QSize
    from PyQt5.QtGui import QColor, QPainter, QPixmap, QPen, QBrush, QLinearGradient
except ImportError:
    print("PyQt5 is not available. Please run in virtual environment.")
    sys.exit(1)

# Style QSS Sheet
ADRENALIN_STYLE = """
QMainWindow {
    background-color: #0c0d12;
}
QWidget {
    color: #f0f2f5;
    font-family: 'Inter', 'DejaVu Sans', sans-serif;
}
QLabel {
    font-size: 12px;
}
QLabel#headerTitle {
    font-family: 'Rajdhani', 'Inter', sans-serif;
    font-size: 18px;
    font-weight: 700;
    text-transform: uppercase;
    color: #e4e6eb;
}
QLabel#headerSubTitle {
    font-family: 'Rajdhani', 'Inter', sans-serif;
    font-size: 11px;
    font-weight: 900;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: #8d95a5;
}
QFrame#card {
    background-color: #141720;
    border: 1px solid #2a2f3d;
    border-radius: 8px;
}
QFrame#previewCard {
    background-color: #1a1e28;
    border: 1px solid #2a2f3d;
    border-radius: 8px;
}
QCheckBox {
    font-size: 12px;
}
QCheckBox::indicator {
    width: 36px;
    height: 20px;
}
QCheckBox::indicator:unchecked {
    image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='36' height='20'><rect width='36' height='20' rx='10' fill='%23252a37' stroke='%232a2f3d'/><circle cx='10' cy='10' r='7' fill='%238d95a5'/></svg>");
}
QCheckBox::indicator:checked {
    image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='36' height='20'><rect width='36' height='20' rx='10' fill='rgba%28233,27,34,0.15%29' stroke='%23e91b22'/><circle cx='26' cy='10' r='7' fill='%23e91b22'/></svg>");
}
QComboBox {
    background-color: #212530;
    border: 1px solid #2a2f3d;
    border-radius: 4px;
    padding: 6px 12px;
    color: #f0f2f5;
    font-size: 12px;
}
QComboBox:hover {
    border-color: #e91b22;
}
QComboBox QAbstractItemView {
    background-color: #1a1e28;
    border: 1px solid #2a2f3d;
    selection-background-color: #e91b22;
    selection-color: #ffffff;
}
QSlider::groove:horizontal {
    border: none;
    height: 4px;
    background: #252a37;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: #e91b22;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #ffffff;
    border: 2px solid #e91b22;
    width: 12px;
    height: 12px;
    margin-top: -4px;
    margin-bottom: -4px;
    border-radius: 6px;
}
QSlider::handle:horizontal:hover {
    background-color: #e91b22;
    border-color: #ffffff;
}
QSlider:disabled::sub-page:horizontal {
    background: #444;
}
QSlider:disabled::handle:horizontal {
    border-color: #555;
    background: #888;
}
QPushButton {
    background-color: #212530;
    border: 1px solid #2a2f3d;
    border-radius: 4px;
    padding: 6px 14px;
    font-size: 11px;
    font-weight: bold;
    color: #f0f2f5;
}
QPushButton:hover {
    background-color: #2a2f3d;
    border-color: #8d95a5;
    color: #ffffff;
}
QPushButton#primaryBtn {
    background-color: #e91b22;
    color: #ffffff;
    border: none;
}
QPushButton#primaryBtn:hover {
    background-color: #ff3c43;
}
QPushButton#resetBtn {
    background: transparent;
    border: none;
    color: #8d95a5;
    padding: 2px;
}
QPushButton#resetBtn:hover {
    color: #e91b22;
}
"""

class PreviewWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.brightness = 1.0
        self.temperature = 6500
        self.r_gamma = 1.0
        self.g_gamma = 1.0
        self.b_gamma = 1.0
        self.saturation = 1.0
        self.vibrance = 1.0
        self.contrast = 1.0
        self.custom_color_enabled = True
        
        # Pre-render standard patterns
        self.preview_pixmap = QPixmap(360, 200)
        self.preview_pixmap.fill(QColor("#000000"))
        
        self.grayscale_pixmap = QPixmap(360, 200)
        
        self.render_test_pattern()

    def render_test_pattern(self):
        # Color Pattern rendering
        painter = QPainter(self.preview_pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Sky Gradient
        sky_grad = QLinearGradient(0, 0, 0, 110)
        sky_grad.setColorAt(0, QColor("#08020f"))
        sky_grad.setColorAt(0.5, QColor("#220735"))
        sky_grad.setColorAt(1, QColor("#4c0b46"))
        painter.fillRect(0, 0, 360, 110, sky_grad)
        
        # Stars
        painter.setPen(QColor("#ffffff"))
        stars = [(30, 20), (120, 15), (75, 40), (280, 25), (320, 35), (200, 10), (160, 45)]
        for x, y in stars:
            painter.drawPoint(x, y)
            
        # Cyber Sun
        sun_grad = QLinearGradient(0, 35, 0, 110)
        sun_grad.setColorAt(0, QColor("#ffeb3b"))
        sun_grad.setColorAt(0.5, QColor("#ff4081"))
        sun_grad.setColorAt(1, QColor("#9c27b0"))
        painter.setBrush(QBrush(sun_grad))
        painter.setPen(Qt.NoPen)
        painter.drawChord(110, 35, 140, 140, 0 * 16, 180 * 16)
        
        # Sun horizontal slices
        painter.setBrush(QBrush(QColor("#08020f")))
        for y in range(85, 110, 4):
            painter.fillRect(100, y, 160, 2, QColor("#08020f"))
            
        # Grid Floor Background
        painter.fillRect(0, 110, 360, 90, QColor("#0b001a"))
        
        # Perspective Grid lines
        painter.setPen(QPen(QColor("#ea00d9"), 1))
        # Verticals
        for i in range(-8, 9):
            painter.drawLine(180 + i * 2, 110, 180 + i * 45, 200)
        # Horizontals
        for i in range(1, 9):
            y_pos = 110 + (200 - 110) * ((i / 8.0) ** 2.2)
            painter.drawLine(0, int(y_pos), 360, int(y_pos))
            
        # Color bar indicators
        bar_colors = [QColor("#ff0000"), QColor("#00ff00"), QColor("#0000ff"), QColor("#ffffff")]
        for idx, col in enumerate(bar_colors):
            painter.fillRect(310 + idx * 10, 130, 8, 50, col)

        painter.end()

        # Generate Grayscale fallback using QImage conversion
        qimg = self.preview_pixmap.toImage().convertToFormat(QPixmap.toImage(self.preview_pixmap).Format_Grayscale8)
        self.grayscale_pixmap = QPixmap.fromImage(qimg)

    def update_params(self, enabled, brightness, temp, r_gamma, g_gamma, b_gamma, saturation, vibrance, contrast):
        self.custom_color_enabled = enabled
        self.brightness = brightness
        self.temperature = temp
        self.r_gamma = r_gamma
        self.g_gamma = g_gamma
        self.b_gamma = b_gamma
        self.saturation = saturation
        self.vibrance = vibrance
        self.contrast = contrast
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        
        # 1. Base rendering and Saturation calculation
        rendered = QPixmap(self.preview_pixmap.size())
        rendered.fill(Qt.transparent)
        
        r_paint = QPainter(rendered)
        
        # If Saturation or Vibrance is below 100%, blend in the grayscale copy
        eff_sat = self.saturation * self.vibrance if self.custom_color_enabled else 1.0
        if eff_sat < 1.0:
            r_paint.drawPixmap(0, 0, self.preview_pixmap)
            r_paint.setOpacity(1.0 - eff_sat)
            r_paint.drawPixmap(0, 0, self.grayscale_pixmap)
            r_paint.setOpacity(1.0)
        else:
            r_paint.drawPixmap(0, 0, self.preview_pixmap)
            
            # If Saturation/Vibrance is high, overlay self with composition rules to simulate vibrant boost
            if eff_sat > 1.0:
                boost_alpha = int(min(120, 120 * (eff_sat - 1.0)))
                r_paint.setCompositionMode(QPainter.CompositionMode_Overlay)
                r_paint.setOpacity(boost_alpha / 255.0)
                r_paint.drawPixmap(0, 0, self.preview_pixmap)
                r_paint.setOpacity(1.0)
                r_paint.setCompositionMode(QPainter.CompositionMode_SourceOver)
        
        r_paint.end()

        # 2. Apply warm/cool temp filters and brightness/contrast overlays
        if self.custom_color_enabled:
            filter_painter = QPainter(rendered)
            
            # Contrast Overlay (High = overlay boost, Low = flat mid-grey blend)
            if self.contrast > 1.0:
                c_alpha = int(min(100, 100 * (self.contrast - 1.0) / 0.5))
                filter_painter.setCompositionMode(QPainter.CompositionMode_Overlay)
                filter_painter.setOpacity(c_alpha / 255.0)
                filter_painter.drawPixmap(0, 0, rendered) # self blend overlay
                filter_painter.setOpacity(1.0)
                filter_painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            elif self.contrast < 1.0:
                c_alpha = int(90 * (1.0 - self.contrast) / 0.5)
                filter_painter.fillRect(rendered.rect(), QColor(128, 128, 128, min(c_alpha, 90)))
            
            # Temperature Overlay
            if self.temperature < 6500:
                ratio = (6500 - self.temperature) / 2500.0
                alpha = int(90 * ratio)
                filter_painter.fillRect(rendered.rect(), QColor(255, 140, 0, alpha))
            else:
                ratio = (self.temperature - 6500) / 3500.0
                alpha = int(75 * ratio)
                filter_painter.fillRect(rendered.rect(), QColor(0, 120, 255, alpha))
            
            # Brightness Overlay
            if self.brightness > 1.0:
                alpha = int(120 * (self.brightness - 1.0) / 0.5)
                filter_painter.fillRect(rendered.rect(), QColor(255, 255, 255, min(alpha, 120)))
            elif self.brightness < 1.0:
                alpha = int(180 * (1.0 - self.brightness))
                filter_painter.fillRect(rendered.rect(), QColor(0, 0, 0, min(alpha, 180)))
                
            # Basic Gamma adjustments overlays
            if self.r_gamma > 1.05:
                filter_painter.fillRect(rendered.rect(), QColor(255, 0, 0, int(40 * (self.r_gamma - 1.0))))
            elif self.r_gamma < 0.95:
                filter_painter.fillRect(rendered.rect(), QColor(0, 255, 255, int(40 * (1.0 - self.r_gamma))))
            if self.g_gamma > 1.05:
                filter_painter.fillRect(rendered.rect(), QColor(0, 255, 0, int(40 * (self.g_gamma - 1.0))))
            elif self.g_gamma < 0.95:
                filter_painter.fillRect(rendered.rect(), QColor(255, 0, 255, int(40 * (1.0 - self.g_gamma))))
            if self.b_gamma > 1.05:
                filter_painter.fillRect(rendered.rect(), QColor(0, 0, 255, int(40 * (self.b_gamma - 1.0))))
            elif self.b_gamma < 0.95:
                filter_painter.fillRect(rendered.rect(), QColor(255, 255, 0, int(40 * (1.0 - self.b_gamma))))

            filter_painter.end()

        # Paint final product
        x = (self.width() - rendered.width()) // 2
        y = (self.height() - rendered.height()) // 2
        painter.drawPixmap(x, y, rendered)

class AdrenalinColorAdjuster(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Radeon Color Adjuster (unofficial)")
        self.setMinimumSize(940, 680)
        self.setStyleSheet(ADRENALIN_STYLE)
        
        self.config = load_config()
        self.displays = get_connected_displays()
        self.active_display = self.displays[0] if self.displays else "default"
        self.has_ctm = check_ctm_support(self.active_display)
        
        self.setup_ui()
        self.load_display_settings()

    def setup_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        
        top_layout = QVBoxLayout(main_widget)
        top_layout.setContentsMargins(15, 10, 15, 15)
        top_layout.setSpacing(12)

        # 1. HEADER
        header_widget = QWidget()
        header_widget.setFixedHeight(45)
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(0, 0, 0, 0)
        
        title_vbox = QVBoxLayout()
        title_vbox.setSpacing(2)
        lbl_title = QLabel("Radeon Color Adjuster")
        lbl_title.setObjectName("headerTitle")
        lbl_sub = QLabel("Display color tuning for Linux · unofficial, inspired by AMD Adrenalin")
        lbl_sub.setObjectName("headerSubTitle")
        title_vbox.addWidget(lbl_title)
        title_vbox.addWidget(lbl_sub)
        header_layout.addLayout(title_vbox)
        
        header_layout.addStretch()
        
        lbl_disp = QLabel("Active Monitor:")
        header_layout.addWidget(lbl_disp)
        
        self.combo_displays = QComboBox()
        self.combo_displays.addItems(self.displays)
        self.combo_displays.setFixedWidth(240)
        self.combo_displays.currentIndexChanged.connect(self.display_changed)
        header_layout.addWidget(self.combo_displays)
        
        top_layout.addWidget(header_widget)

        # 2. MAIN CONTENTS SPLIT LAYOUT
        content_hbox = QHBoxLayout()
        content_hbox.setSpacing(15)
        
        sliders_vbox = QVBoxLayout()
        sliders_vbox.setSpacing(10)
        
        # Toggle Card
        toggle_card = QFrame()
        toggle_card.setObjectName("card")
        toggle_card_layout = QHBoxLayout(toggle_card)
        toggle_card_layout.setContentsMargins(15, 10, 15, 10)
        
        toggle_text_layout = QVBoxLayout()
        toggle_text_layout.setSpacing(2)
        lbl_toggle = QLabel("Custom Color Calibration")
        lbl_toggle.setStyleSheet("font-weight: bold; font-size: 13px; color: #ffffff;")
        lbl_toggle_desc = QLabel("Enable custom adjustments, temperature controls and channel overrides.")
        lbl_toggle_desc.setStyleSheet("font-size: 10px; color: #8d95a5;")
        toggle_text_layout.addWidget(lbl_toggle)
        toggle_text_layout.addWidget(lbl_toggle_desc)
        toggle_card_layout.addLayout(toggle_text_layout)
        
        self.chk_custom_color = QCheckBox()
        self.chk_custom_color.setChecked(True)
        self.chk_custom_color.stateChanged.connect(self.toggle_custom_color)
        toggle_card_layout.addWidget(self.chk_custom_color)
        
        sliders_vbox.addWidget(toggle_card)

        # CORE SLIDERS CARD
        self.adj_card = QFrame()
        self.adj_card.setObjectName("card")
        adj_grid = QGridLayout(self.adj_card)
        adj_grid.setContentsMargins(15, 15, 15, 15)
        adj_grid.setVerticalSpacing(12)
        adj_grid.setHorizontalSpacing(10)
        
        # Configuration parameters sliders details
        self.sliders_data = [
            (0, "Color Temperature (K)", 4000, 10000, 6500, 1, "temperature", " K"),
            (1, "Display Brightness", 20, 150, 100, 100.0, "brightness", "%"),
            (2, "Display Contrast", 50, 150, 100, 100.0, "contrast", "%"),
            (3, "Color Saturation", 0, 200, 100, 100.0, "saturation", "%"),
            (4, "Color Vibrance", 0, 200, 100, 100.0, "vibrance", "%"),
            (5, "Red Channel Gamma", 50, 200, 100, 100.0, "r_gamma", "%"),
            (6, "Green Channel Gamma", 50, 200, 100, 100.0, "g_gamma", "%"),
            (7, "Blue Channel Gamma", 50, 200, 100, 100.0, "b_gamma", "%")
        ]
        
        self.sliders = {}
        self.value_labels = {}
        
        for idx, name, val_min, val_max, default, scale, key, suffix in self.sliders_data:
            lbl_name = QLabel(name)
            lbl_name.setStyleSheet("font-weight: 500; color: #8d95a5; font-size: 11px;")
            adj_grid.addWidget(lbl_name, idx*2, 0)
            
            lbl_val = QLabel(f"{default}{suffix}")
            lbl_val.setStyleSheet("font-family: monospace; font-size: 11px; font-weight: bold; min-width: 55px;")
            lbl_val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.value_labels[key] = lbl_val
            adj_grid.addWidget(lbl_val, idx*2, 1)
            
            btn_reset = QPushButton("↺")
            btn_reset.setObjectName("resetBtn")
            btn_reset.setToolTip(f"Reset {name} to default")
            btn_reset.clicked.connect(lambda checked, k=key, d=default: self.reset_slider(k, d))
            adj_grid.addWidget(btn_reset, idx*2, 2)
            
            slider = QSlider(Qt.Horizontal)
            slider.setRange(val_min, val_max)
            slider.setValue(default)
            slider.valueChanged.connect(lambda val, k=key, s=scale, suf=suffix: self.slider_changed(k, val, s, suf))
            self.sliders[key] = slider
            adj_grid.addWidget(slider, idx*2+1, 0, 1, 3)
            
        sliders_vbox.addWidget(self.adj_card)
        
        # Reset Panel & Presets options card
        presets_card = QFrame()
        presets_card.setObjectName("card")
        presets_layout = QHBoxLayout(presets_card)
        presets_layout.setContentsMargins(15, 10, 15, 10)
        
        btn_reset_all = QPushButton("Reset All Parameters")
        btn_reset_all.clicked.connect(self.reset_all_settings)
        presets_layout.addWidget(btn_reset_all)
        
        # Autostart Apply
        self.chk_autostart = QCheckBox("Apply settings on system login")
        self.chk_autostart.stateChanged.connect(self.autostart_changed)
        presets_layout.addWidget(self.chk_autostart)
        
        sliders_vbox.addWidget(presets_card)
        content_hbox.addLayout(sliders_vbox, stretch=4)
        
        # Right side panel for Monitor graphics preview
        right_panel_vbox = QVBoxLayout()
        right_panel_vbox.setSpacing(12)
        
        self.preview_card = QFrame()
        self.preview_card.setObjectName("previewCard")
        preview_layout = QVBoxLayout(self.preview_card)
        preview_layout.setContentsMargins(15, 12, 15, 12)
        
        lbl_prev_title = QLabel("CALIBRATION SIMULATOR PREVIEW")
        lbl_prev_title.setStyleSheet("font-family: 'Rajdhani', sans-serif; font-size: 11px; font-weight: bold; text-transform: uppercase; color: #8d95a5;")
        preview_layout.addWidget(lbl_prev_title)
        
        self.preview_widget = PreviewWidget()
        self.preview_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        preview_layout.addWidget(self.preview_widget)
        
        right_panel_vbox.addWidget(self.preview_card, stretch=6)
        
        # Preset Tuning Quick profiles card
        presets_box = QFrame()
        presets_box.setObjectName("card")
        presets_box_layout = QVBoxLayout(presets_box)
        presets_box_layout.setContentsMargins(15, 12, 15, 12)
        presets_box_layout.setSpacing(8)
        
        lbl_presets_title = QLabel("DISPLAY COLOR ENHANCEMENT PROFILES")
        lbl_presets_title.setStyleSheet("font-family: 'Rajdhani', sans-serif; font-size: 11px; font-weight: bold; text-transform: uppercase; color: #8d95a5;")
        presets_box_layout.addWidget(lbl_presets_title)
        
        profiles_grid = QHBoxLayout()
        profiles_grid.setSpacing(8)
        
        preset_profiles = [
            ("Standard", self.apply_std_profile),
            ("Vivid Gaming", self.apply_vivid_profile),
            ("Cinema Warm", self.apply_cinema_profile),
            ("FPS Performance", self.apply_fps_profile)
        ]
        
        for name, callback in preset_profiles:
            btn = QPushButton(name)
            btn.clicked.connect(callback)
            profiles_grid.addWidget(btn)
            
        presets_box_layout.addLayout(profiles_grid)
        right_panel_vbox.addWidget(presets_box, stretch=4)
        
        content_hbox.addLayout(right_panel_vbox, stretch=5)
        top_layout.addLayout(content_hbox)

        # FOOTER
        footer_widget = QWidget()
        footer_widget.setFixedHeight(30)
        footer_layout = QHBoxLayout(footer_widget)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        
        self.lbl_status = QLabel("Active calibration parameters sync operational.")
        self.lbl_status.setStyleSheet("color: #8d95a5; font-size: 11px;")
        footer_layout.addWidget(self.lbl_status)
        
        footer_layout.addStretch()
        
        self.lbl_ctm_status = QLabel("CTM Enabled")
        self.lbl_ctm_status.setStyleSheet("color: #00e676; font-size: 11px; font-weight: bold;")
        footer_layout.addWidget(self.lbl_ctm_status)
        
        lbl_x11 = QLabel("| Backend: X11 / XRandR")
        lbl_x11.setStyleSheet("color: #8d95a5; font-size: 11px;")
        footer_layout.addWidget(lbl_x11)
        
        top_layout.addWidget(footer_widget)

    # ----------------- EVENT CALLBACKS -----------------
    def load_display_settings(self):
        s_config = self.config.get("displays", {})
        self.chk_autostart.blockSignals(True)
        self.chk_autostart.setChecked(self.config.get("autostart", False))
        self.chk_autostart.blockSignals(False)
        
        index = self.combo_displays.findText(self.active_display)
        if index >= 0:
            self.combo_displays.setCurrentIndex(index)
            
        # Detect CTM hardware support for active monitor
        self.has_ctm = check_ctm_support(self.active_display)
        self.update_ctm_ui_state()

        if self.active_display in s_config:
            d_cfg = s_config[self.active_display]
            
            self.chk_custom_color.blockSignals(True)
            self.chk_custom_color.setChecked(d_cfg.get("enabled", True))
            self.chk_custom_color.blockSignals(False)
            
            for key, slider in self.sliders.items():
                slider.blockSignals(True)
                slider.setValue(d_cfg.get(key, self.get_slider_default(key)))
                slider.blockSignals(False)
                
            self.update_labels()
            self.toggle_custom_color(self.chk_custom_color.isChecked())
        else:
            self.reset_all_settings()

    def update_ctm_ui_state(self):
        if self.has_ctm:
            self.lbl_ctm_status.setText("Saturation/Vibrance hardware matrix active")
            self.lbl_ctm_status.setStyleSheet("color: #00e676; font-size: 11px; font-weight: bold;")
            self.sliders["saturation"].setEnabled(True)
            self.sliders["vibrance"].setEnabled(True)
        else:
            self.lbl_ctm_status.setText("Saturation/Vibrance matrix not supported by driver on this monitor")
            self.lbl_ctm_status.setStyleSheet("color: #ff5252; font-size: 11px; font-weight: bold;")
            self.sliders["saturation"].setDisabled(True)
            self.sliders["vibrance"].setDisabled(True)

    def save_current_display_settings(self):
        if "displays" not in self.config:
            self.config["displays"] = {}
            
        d_cfg = {
            "enabled": self.chk_custom_color.isChecked(),
        }
        for key in ["temperature", "brightness", "contrast", "saturation", "vibrance", "r_gamma", "g_gamma", "b_gamma"]:
            d_cfg[key] = self.sliders[key].value()

        self.config["displays"][self.active_display] = d_cfg
        save_config(self.config)

    def get_slider_default(self, key):
        for idx, name, val_min, val_max, default, scale, k, suffix in self.sliders_data:
            if k == key:
                return default
        return 100

    def display_changed(self, index):
        self.active_display = self.combo_displays.currentText()
        self.load_display_settings()
        self.lbl_status.setText(f"Active monitor calibration target switched to: {self.active_display}")

    def toggle_custom_color(self, state_checked):
        enabled = bool(state_checked)
        self.adj_card.setEnabled(enabled)
        if enabled:
            self.update_ctm_ui_state() # Re-verify CTM enabling rules
            
        self.trigger_apply()
        self.save_current_display_settings()

    def slider_changed(self, key, value, scale, suffix):
        scaled_val = value / scale if scale > 1 else value
        if scale == 100.0:
            self.value_labels[key].setText(f"{int(scaled_val * 100)}{suffix}")
        else:
            self.value_labels[key].setText(f"{value}{suffix}")
            
        self.trigger_apply()
        self.save_current_display_settings()

    def reset_slider(self, key, default_val):
        self.sliders[key].setValue(default_val)

    def reset_all_settings(self):
        for key, slider in self.sliders.items():
            slider.blockSignals(True)
            slider.setValue(self.get_slider_default(key))
            slider.blockSignals(False)
            
        self.chk_custom_color.blockSignals(True)
        self.chk_custom_color.setChecked(True)
        self.chk_custom_color.blockSignals(False)
        
        self.toggle_custom_color(True)
        self.update_labels()
        self.trigger_apply()
        self.save_current_display_settings()
        self.lbl_status.setText("All color calibration variables restored to system standard defaults.")

    def update_labels(self):
        for idx, name, val_min, val_max, default, scale, key, suffix in self.sliders_data:
            val = self.sliders[key].value()
            scaled_val = val / scale if scale > 1 else val
            if scale == 100.0:
                self.value_labels[key].setText(f"{int(scaled_val * 100)}{suffix}")
            else:
                self.value_labels[key].setText(f"{val}{suffix}")

    def trigger_apply(self):
        enabled = self.chk_custom_color.isChecked()
        brightness = self.sliders["brightness"].value() / 100.0
        temp = self.sliders["temperature"].value()
        contrast = self.sliders["contrast"].value() / 100.0
        saturation = self.sliders["saturation"].value() / 100.0 if self.has_ctm else 1.0
        vibrance = self.sliders["vibrance"].value() / 100.0 if self.has_ctm else 1.0
        r_gamma = self.sliders["r_gamma"].value() / 100.0
        g_gamma = self.sliders["g_gamma"].value() / 100.0
        b_gamma = self.sliders["b_gamma"].value() / 100.0
        
        # Update preview canvas
        self.preview_widget.update_params(
            enabled=enabled,
            brightness=brightness,
            temp=temp,
            r_gamma=r_gamma,
            g_gamma=g_gamma,
            b_gamma=b_gamma,
            saturation=saturation,
            vibrance=vibrance,
            contrast=contrast
        )
        
        # Apply configurations via hardware pipeline
        apply_display_settings(
            display=self.active_display,
            enabled=enabled,
            brightness=brightness,
            temp=temp,
            r_gamma=r_gamma,
            g_gamma=g_gamma,
            b_gamma=b_gamma,
            saturation=saturation,
            vibrance=vibrance,
            contrast=contrast,
            has_ctm=self.has_ctm
        )

    def autostart_changed(self, state_checked):
        enabled = bool(state_checked)
        self.config["autostart"] = enabled
        save_config(self.config)
        set_autostart_enabled(enabled)
        
        msg = "Autostart hook active. Calibrations will restore on system login." if enabled else "Autostart hook removed."
        self.lbl_status.setText(msg)

    # Quick profile applications
    def apply_std_profile(self):
        self.reset_all_settings()

    def apply_vivid_profile(self):
        self.sliders["temperature"].setValue(6800)
        self.sliders["brightness"].setValue(105)
        self.sliders["contrast"].setValue(115)
        self.sliders["r_gamma"].setValue(115)
        self.sliders["g_gamma"].setValue(115)
        self.sliders["b_gamma"].setValue(125)
        if self.has_ctm:
            self.sliders["saturation"].setValue(125)
            self.sliders["vibrance"].setValue(115)
        self.lbl_status.setText("Vivid profile applied: Enhanced contrast, boosted saturation & white points.")

    def apply_cinema_profile(self):
        self.sliders["temperature"].setValue(5500)
        self.sliders["brightness"].setValue(90)
        self.sliders["contrast"].setValue(95)
        self.sliders["r_gamma"].setValue(100)
        self.sliders["g_gamma"].setValue(105)
        self.sliders["b_gamma"].setValue(85)
        if self.has_ctm:
            self.sliders["saturation"].setValue(95)
            self.sliders["vibrance"].setValue(90)
        self.lbl_status.setText("Cinema profile applied: Warm theatrical curves, relaxed colors.")

    def apply_fps_profile(self):
        self.sliders["temperature"].setValue(7200)
        self.sliders["brightness"].setValue(115)
        self.sliders["contrast"].setValue(120)
        self.sliders["r_gamma"].setValue(125)
        self.sliders["g_gamma"].setValue(120)
        self.sliders["b_gamma"].setValue(110)
        if self.has_ctm:
            self.sliders["saturation"].setValue(110)
            self.sliders["vibrance"].setValue(120)
        self.lbl_status.setText("FPS profile applied: Expanded detail saturation & dynamic midtones.")

def main():
    app = QApplication(sys.argv)
    window = AdrenalinColorAdjuster()
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
