# Windows Disk Space Visualizer GUI
# Python 3.8+, Tkinter

import os
import re
import ctypes
import ctypes.wintypes as wintypes
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import string
from datetime import datetime

DEBUG_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'treemap_debug.log')

# Treemap layout: merge entries below this share of view root into "Other"
OTHER_SIZE_PCT = 0.02
# Minimum estimated rectangle side (px) needed to show a label
MIN_LABEL_WIDTH = 50
MIN_LABEL_HEIGHT = 22

# --- Constants (from original script) ---
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FSCTL_GET_REPARSE_POINT = 0x000900A8
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MAX_REPARSE_SIZE = 16 * 1024
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

# --- WinAPI ---
kernel32 = ctypes.windll.kernel32
CreateFileW = kernel32.CreateFileW
DeviceIoControl = kernel32.DeviceIoControl
CloseHandle = kernel32.CloseHandle
GetFileAttributesW = kernel32.GetFileAttributesW
CreateFileW.restype = wintypes.HANDLE

# --- Reparse buffer (simplified) ---
class GENERIC_REPARSE_BUFFER(ctypes.Structure):
    _fields_ = [
        ("ReparseTag", wintypes.DWORD),
        ("ReparseDataLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("SubstituteNameOffset", wintypes.USHORT),
        ("SubstituteNameLength", wintypes.USHORT),
        ("PrintNameOffset", wintypes.USHORT),
        ("PrintNameLength", wintypes.USHORT),
        ("PathBuffer", ctypes.c_wchar * (MAX_REPARSE_SIZE // 2))
    ]

# --- Helper functions (from original script) ---
def _get_attrs(path):
    a = GetFileAttributesW(path)
    if a == INVALID_FILE_ATTRIBUTES:
        return None
    return a

def is_reparse_point(path):
    a = _get_attrs(path)
    return bool(a is not None and (a & FILE_ATTRIBUTE_REPARSE_POINT))

def is_reparse_point_target_same_volume(path, volume_letter):
    attrs = _get_attrs(path)
    if attrs is None:
        return False
    if not (attrs & FILE_ATTRIBUTE_REPARSE_POINT):
        return True

    flags = FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS
    h = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, 
                    None, OPEN_EXISTING, flags, None)
    if h == INVALID_HANDLE_VALUE or h is None:
        return False

    buf = ctypes.create_string_buffer(MAX_REPARSE_SIZE)
    bytes_returned = wintypes.DWORD(0)
    ok = DeviceIoControl(h, FSCTL_GET_REPARSE_POINT, None, 0, buf, 
                         MAX_REPARSE_SIZE, ctypes.byref(bytes_returned), None)
    CloseHandle(h)
    if not ok:
        return False

    try:
        rdb = GENERIC_REPARSE_BUFFER.from_buffer_copy(buf)
        sub_off = rdb.SubstituteNameOffset // 2
        sub_len = rdb.SubstituteNameLength // 2
        target = "".join(rdb.PathBuffer[sub_off: sub_off + sub_len])
        target_upper = target.upper()
    except Exception:
        return False

    vol_upper = (volume_letter.rstrip(':').upper() + ':')
    
    if r'UNC\\' in target_upper or r'\\??\\UNC\\' in target_upper or r'\\?\\UNC\\' in target_upper:
        return False
    
    m = re.search(r'([A-Z]:)', target_upper)
    if m:
        return (m.group(1) == vol_upper)
    
    if 'VOLUME{' in target_upper:
        return False
    
    return False

class CanvasTooltip:
    """Borderless hover tooltip for treemap cells."""

    def __init__(self, parent):
        self.parent = parent
        self.window = None
        self.label = None

    def show(self, text, x, y):
        if not text:
            self.hide()
            return
        if self.window is None:
            self.window = tk.Toplevel(self.parent)
            self.window.wm_overrideredirect(True)
            self.window.wm_attributes('-topmost', True)
            self.label = tk.Label(
                self.window, text=text, justify=tk.CENTER,
                background='#ffffe0', foreground='#1a1a2e',
                relief=tk.SOLID, borderwidth=1,
                font=('Segoe UI', 9), padx=6, pady=4)
            self.label.pack()
        else:
            self.label.config(text=text)
        self.window.geometry(f'+{x}+{y}')

    def hide(self):
        if self.window is not None:
            self.window.destroy()
            self.window = None
            self.label = None

# --- Node class (from original script) ---
class Node:
    def __init__(self, name, parent=None, is_dir=False, size=0):
        self.name = name
        self.parent = parent
        self.is_dir = is_dir
        self.children = [] if is_dir else None
        self.size = size
        # GUI-specific attributes
        self.rect_coords = None  # (x1, y1, x2, y2)
        self.canvas_id = None    # Canvas item ID

    def add_child(self, ch):
        if self.children is not None:
            self.children.append(ch)

    def full_path(self):
        parts = []
        node = self
        while node is not None:
            parts.append(node.name)
            node = node.parent
        parts = list(reversed(parts))
        if parts and parts[0].endswith(':'):
            if len(parts) == 1:
                return parts[0] + os.sep
            return parts[0] + os.sep + os.path.join(*parts[1:])
        return os.path.join(*parts)

    def aggregate_size(self):
        if not self.is_dir:
            return self.size
        total = 0
        for ch in self.children:
            total += ch.aggregate_size()
        self.size = total
        return total

# --- Build tree function (from original script) ---
def build_tree(path, parent=None, depth=0, max_depth=3, root_volume_letter=None):
    try:
        if parent is None:
            drive, _ = os.path.splitdrive(os.path.normpath(path))
            root_name = drive.rstrip('\\') or path
            node = Node(root_name, parent=None, is_dir=True)
        else:
            node_name = os.path.basename(path) or path
            node = Node(node_name, parent=parent, is_dir=os.path.isdir(path))

        if is_reparse_point(path):
            same = is_reparse_point_target_same_volume(path, root_volume_letter)
            if not same:
                return None

        if node.is_dir and depth < max_depth:
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        try:
                            entry_path = entry.path
                            if entry.is_dir(follow_symlinks=False):
                                if is_reparse_point(entry_path):
                                    same = is_reparse_point_target_same_volume(entry_path, root_volume_letter)
                                    if not same:
                                        continue
                                child = build_tree(entry_path, parent=node, depth=depth+1, 
                                                 max_depth=max_depth, root_volume_letter=root_volume_letter)
                                if child:
                                    node.add_child(child)
                            else:
                                try:
                                    st = entry.stat(follow_symlinks=False)
                                    size = st.st_size
                                except Exception:
                                    size = 0
                                if size > 0:
                                    child = Node(entry.name, parent=node, is_dir=False, size=size)
                                    node.add_child(child)
                        except PermissionError:
                            continue
            except PermissionError:
                return None
        return node
    except Exception:
        return None

class DiskSpaceVisualizer:
    def __init__(self, root):
        self.root = root
        self.root.title("a7in Disk Space Visualizer")
        self.root.state('zoomed')  # Maximize window on Windows
        
        # Data
        self.tree_root = None
        self.view_root = None       # Current focus node for drawing (None = full tree)
        self.rectangles = []        # List of (node, x1, y1, x2, y2) tuples
        self.context_node = None    # Node under cursor for context menu
        self.scanning = False
        self.debug_log_rewrite = False
        self.debug_entries = []
        self.tooltip = CanvasTooltip(root)
        self._tooltip_node = None
        
        # Colors for visualization
        self.colors = [
            "#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEAA7",
            "#DDA0DD", "#98D8C8", "#F7DC6F", "#BB8FCE", "#85C1E9",
            "#F8C471", "#82E0AA", "#F1948A", "#85C1E9", "#D7BDE2"
        ]
        
        self.setup_ui()
        
    def setup_ui(self):
        # Top frame: left controls (50%) + item info panel (50%)
        control_frame = ttk.Frame(self.root)
        control_frame.pack(fill=tk.X, padx=10, pady=5)
        control_frame.columnconfigure(0, weight=1)
        control_frame.columnconfigure(1, weight=1)

        left_frame = ttk.Frame(control_frame)
        left_frame.grid(row=0, column=0, sticky='ew')
        left_frame.columnconfigure(7, weight=1)

        # Drive selection
        ttk.Label(left_frame, text="Drive:").grid(row=0, column=0, padx=(0, 5))
        
        self.drive_var = tk.StringVar(value="C")
        drives = self.get_available_drives()
        self.drive_combo = ttk.Combobox(left_frame, textvariable=self.drive_var, 
                                       values=drives, width=5, state="readonly")
        self.drive_combo.grid(row=0, column=1, padx=(0, 20))
        self.drive_combo.bind('<<ComboboxSelected>>', self.on_drive_changed)
        
        # Depth selection
        ttk.Label(left_frame, text="Depth:").grid(row=0, column=2, padx=(0, 5))
        
        self.depth_var = tk.StringVar(value="2")
        self.depth_combo = ttk.Combobox(left_frame, textvariable=self.depth_var,
                                       values=["1", "2", "3", "4", "5"], width=5, state="readonly")
        self.depth_combo.grid(row=0, column=3, padx=(0, 20))
        self.depth_combo.bind('<<ComboboxSelected>>', self.on_depth_changed)
        
        # Reset view button
        self.reset_button = ttk.Button(left_frame, text="Reset View",
                                       command=self.reset_view, state='disabled')
        self.reset_button.grid(row=0, column=4, padx=(0, 10))
        
        # Scan button
        self.scan_button = ttk.Button(left_frame, text="Scan", command=self.start_scan)
        self.scan_button.grid(row=0, column=5, padx=(0, 20))
        
        # Progress bar
        self.progress = ttk.Progressbar(left_frame, mode='indeterminate')
        self.progress.grid(row=0, column=6, sticky='ew', padx=(0, 10))

        # Scan / view status (compact, left side)
        self.status_label = ttk.Label(left_frame, text="Ready")
        self.status_label.grid(row=0, column=7, sticky='e')

        info_frame = ttk.Frame(control_frame)
        info_frame.grid(row=0, column=1, sticky='ew', padx=(10, 0))
        info_frame.columnconfigure(0, weight=1)

        info_font = ('Segoe UI', 9)
        self.info_path_var = tk.StringVar()
        self.info_detail_var = tk.StringVar()
        self.info_path_entry = tk.Entry(
            info_frame, textvariable=self.info_path_var, font=info_font,
            state='readonly', readonlybackground='white')
        self.info_path_entry.grid(row=0, column=0, sticky='ew', pady=(0, 2))
        self.info_detail_entry = tk.Entry(
            info_frame, textvariable=self.info_detail_var, font=info_font,
            state='readonly', readonlybackground='white')
        self.info_detail_entry.grid(row=1, column=0, sticky='ew')
        
        # Canvas for visualization
        canvas_frame = ttk.Frame(self.root)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        
        self.canvas = tk.Canvas(canvas_frame, bg='white')
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        # Context menu
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Expand to full view", command=self.expand_focused_node)
        self.context_menu.add_command(label="Show Info", command=self.show_context_node_info)
        
        # Bind events
        self.canvas.bind('<Button-1>', self.on_canvas_click)
        self.canvas.bind('<Button-3>', self.on_canvas_right_click)
        self.canvas.bind('<Motion>', self.on_canvas_motion)
        self.canvas.bind('<Leave>', self.on_canvas_leave)
        self.canvas.bind('<Configure>', self.on_canvas_resize)
    
    def get_available_drives(self):
        """Get list of available drive letters"""
        drives = []
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in string.ascii_uppercase:
            if bitmask & 1:
                drives.append(letter)
            bitmask >>= 1
        return drives
    
    def on_drive_changed(self, event=None):
        if not self.scanning:
            self.start_scan()
    
    def on_depth_changed(self, event=None):
        if not self.scanning and self.tree_root:
            # Только перерисовываем, не сканируем заново
            self.draw_visualization()
    
    def start_scan(self):
        if self.scanning:
            return
            
        self.scanning = True
        self.scan_button.config(state='disabled')
        self.progress.start()
        self.status_label.config(text="Scanning...")
        self.canvas.delete("all")
        
        # Start scanning in background thread
        drive = self.drive_var.get()
        depth = int(self.depth_var.get())
        
        thread = threading.Thread(target=self.scan_worker, args=(drive, depth))
        thread.daemon = True
        thread.start()
    
    def scan_worker(self, drive, depth):
        try:
            root_path = f"{drive}:\\"
            root_vol = f"{drive}:"
            
            # Сканируем на полную глубину (100 уровней)
            tree_root = build_tree(root_path, parent=None, depth=0, 
                                 max_depth=100, root_volume_letter=root_vol)
            
            if tree_root:
                tree_root.aggregate_size()
            
            # Update UI in main thread
            self.root.after(0, self.scan_complete, tree_root)
            
        except Exception as e:
            self.root.after(0, self.scan_error, str(e))
    
    def scan_complete(self, tree_root):
        self.tree_root = tree_root
        self.view_root = tree_root
        self.scanning = False
        self.scan_button.config(state='normal')
        self.progress.stop()
        
        if tree_root:
            self.debug_log_rewrite = True
            self.update_view_status()
            self.draw_visualization()
        else:
            self.status_label.config(text="Scan failed")
            messagebox.showerror("Error", "Failed to scan drive. Check permissions.")
    
    def scan_error(self, error_msg):
        self.scanning = False
        self.scan_button.config(state='normal')
        self.progress.stop()
        self.status_label.config(text="Error occurred")
        messagebox.showerror("Scan Error", f"Error during scan: {error_msg}")
    
    def reset_view(self):
        if self.tree_root:
            self.view_root = self.tree_root
            self.debug_log_rewrite = True
            self.update_view_status()
            self.draw_visualization()
    
    def expand_focused_node(self):
        node = self.context_node
        if node and node.is_dir:
            self.view_root = node
            self.debug_log_rewrite = True
            self.update_view_status()
            self.draw_visualization()
    
    def update_view_status(self):
        if not self.tree_root:
            return
        size_text = self.format_size(self.tree_root.size)
        if self.view_root and self.view_root is not self.tree_root:
            self.status_label.config(
                text=f"View: {self.view_root.full_path()} ({self.format_size(self.view_root.size)}) | Drive: {size_text}"
            )
            self.reset_button.config(state='normal')
        else:
            self.status_label.config(text=f"Scanned: {size_text}")
            self.reset_button.config(state='disabled')
    
    def draw_visualization(self):
        root = self.view_root or self.tree_root
        if not root:
            return

        self._hide_tooltip()
        self.canvas.delete("all")
        self.rectangles.clear()

        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()

        if canvas_width <= 1 or canvas_height <= 1:
            self.root.after(100, self.draw_visualization)
            return

        draw_depth = int(self.depth_var.get())
        should_log = self.debug_log_rewrite
        if should_log:
            self.debug_entries = []
        draw_error = None
        try:
            self.draw_node_recursive(root, 0, 0, canvas_width, canvas_height, 0, draw_depth)
        except Exception as exc:
            draw_error = exc
            import traceback
            traceback.print_exc()
        if should_log:
            self._write_debug_log(canvas_width, canvas_height, draw_depth, error=draw_error)
            self.debug_log_rewrite = False
        if draw_error:
            self.status_label.config(text=f"Draw error: {draw_error}")

    def _record_debug_entry(self, node, x0, y0, x1, y1, level):
        view = self.view_root or self.tree_root
        view_size = view.size if view and view.size > 0 else 1
        w = x1 - x0
        h = y1 - y0
        if getattr(node, 'is_other', False):
            name = 'Other'
            path = (f"[Other] {node.other_dir_count} folders, "
                    f"{node.other_file_count} files")
            is_dir = True
            kind = 'O'
        else:
            name = node.name
            path = node.full_path()
            is_dir = node.is_dir
            kind = 'D' if is_dir else 'F'
        self.debug_entries.append({
            'level': level,
            'path': path,
            'name': name,
            'is_dir': is_dir,
            'kind': kind,
            'size': node.size,
            'size_pct': 100.0 * node.size / view_size,
            'px_x': x0,
            'px_y': y0,
            'px_w': w,
            'px_h': h,
            'px_area': w * h,
        })

    def _draw_tracked_rectangle(self, node, x0, y0, x1, y1, level, color, outline_w=1):
        """Draw one canvas rectangle; log exactly once when debug logging is enabled."""
        rect_id = self.canvas.create_rectangle(
            x0, y0, x1, y1, fill=color, outline='#333333', width=outline_w)
        node.rect_coords = (x0, y0, x1, y1)
        node.canvas_id = rect_id
        self.rectangles.append((node, x0, y0, x1, y1))
        if self.debug_log_rewrite:
            self._record_debug_entry(node, x0, y0, x1, y1, level)
        return rect_id

    def _write_debug_log(self, canvas_w, canvas_h, draw_depth, error=None):
        view = self.view_root or self.tree_root
        if not view:
            return

        canvas_area = canvas_w * canvas_h
        view_size = view.size
        children = [ch for ch in (view.children or []) if ch.size > 0]
        children_sum = sum(ch.size for ch in children)

        lines = [
            f"=== Treemap debug {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===",
            f"Log file: {DEBUG_LOG_PATH}",
        ]
        if error:
            lines.append(f"DRAW ERROR: {error}")
        lines.extend([
            f"View root: {view.full_path()}",
            f"View size: {view.size} bytes ({self.format_size(view.size)})",
            f"Direct children: {len(children)} (sum {children_sum} bytes, "
            f"delta vs view {view_size - children_sum:+d})",
            f"Canvas: {canvas_w} x {canvas_h} = {canvas_area} px",
            f"Draw depth: {draw_depth}",
            "",
            f"{'Lvl':>3}  {'Name':<24}  {'Size':>12}  {'Size%':>7}  "
            f"{'Px':>13}  {'PxArea':>8}  {'Px%':>7}  Path",
            "-" * 120,
        ])

        entries = sorted(self.debug_entries, key=lambda e: (e['level'], -e['size']))
        total_px = 0
        for e in entries:
            px_pct = 100.0 * e['px_area'] / canvas_area if canvas_area else 0.0
            total_px += e['px_area']
            kind = e.get('kind', 'D' if e['is_dir'] else 'F')
            lines.append(
                f"{e['level']:3d}  {e['name'][:24]:<24}  "
                f"{self.format_size(e['size']):>12}  {e['size_pct']:6.2f}%  "
                f"{e['px_w']:4d}x{e['px_h']:<4d}  {e['px_area']:8d}  {px_pct:6.2f}%  "
                f"[{kind}] {e['path']}"
            )

        lines.extend([
            "-" * 120,
            f"Drawn items: {len(entries)}",
            f"Canvas rectangles: {len(self.rectangles)}",
        ])
        if len(entries) != len(self.rectangles):
            lines.append(
                f"WARNING: log entries ({len(entries)}) != "
                f"canvas rectangles ({len(self.rectangles)})"
            )
        lines.extend([
            f"Sum pixel areas (all rects, nested): {total_px} px "
            f"({100.0 * total_px / canvas_area:.2f}% of canvas, overlaps expected)",
            "",
            "Level-1 tiles (direct children of view root on screen):",
        ])

        level1 = [e for e in entries if e['level'] == 1]
        level1_px = sum(e['px_area'] for e in level1)
        level1_size = sum(e['size'] for e in level1)
        for e in sorted(level1, key=lambda x: -x['size']):
            px_pct = 100.0 * e['px_area'] / canvas_area if canvas_area else 0.0
            lines.append(
                f"  {e['name']:<24}  {self.format_size(e['size']):>12}  "
                f"size {e['size_pct']:5.2f}%  px {e['px_w']}x{e['px_h']}  "
                f"px {px_pct:5.2f}%"
            )
        lines.append(
            f"  Level-1 total: size {100.0 * level1_size / view_size:.2f}%  "
            f"px {100.0 * level1_px / canvas_area:.2f}% of canvas ({level1_px} px)"
        )

        text = "\n".join(lines) + "\n"
        try:
            with open(DEBUG_LOG_PATH, 'w', encoding='utf-8') as f:
                f.write(text)
            print(f"[treemap debug] written to {DEBUG_LOG_PATH}")
        except OSError as err:
            print(f"[treemap debug] write failed: {err}")
        print(text)
    
    def node_color(self, node, level):
        """Pick a stable color per node; siblings differ, deeper levels are slightly darker."""
        palette = self.colors
        if node.parent and node.parent.children:
            siblings = [ch for ch in node.parent.children if ch.size > 0]
            try:
                idx = siblings.index(node)
            except ValueError:
                idx = 0
            base = palette[idx % len(palette)]
        else:
            base = palette[level % len(palette)]
        if level > 1:
            return self._shade_color(base, 0.92 ** (level - 1))
        return base
    
    @staticmethod
    def _shade_color(hex_color, factor):
        hex_color = hex_color.lstrip('#')
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        r = min(255, int(r * factor))
        g = min(255, int(g * factor))
        b = min(255, int(b * factor))
        return f'#{r:02x}{g:02x}{b:02x}'
    
    def draw_node_recursive(self, node, x, y, width, height, level, max_draw_depth):
        w = int(round(width))
        h = int(round(height))
        if not node or w < 1 or h < 1:
            return

        x0 = int(round(x))
        y0 = int(round(y))
        x1 = x0 + w
        y1 = y0 + h

        color = self.node_color(node, level)
        if getattr(node, 'is_other', False):
            color = '#BDC3C7'
        outline_w = 2 if level == 0 else 1

        self._draw_tracked_rectangle(node, x0, y0, x1, y1, level, color, outline_w)

        if getattr(node, 'is_other', False):
            self._draw_other_label(node, x0, y0, w, h)
        else:
            self._draw_node_label(node, x0, y0, w, h)

        if getattr(node, 'is_other', False):
            return

        if node.is_dir and node.children and level < max_draw_depth:
            children = [ch for ch in node.children if ch.size > 0]
            if not children:
                return
            inset = 1 if level > 0 else 0
            inner_w = w - 2 * inset
            inner_h = h - 2 * inset
            if inner_w < 1 or inner_h < 1:
                return
            self.draw_treemap(children, x0 + inset, y0 + inset,
                              inner_w, inner_h, level + 1, max_draw_depth)

    @staticmethod
    def _label_display_mode(width, height):
        """How much of the label fits: none, partial, or full two-line text."""
        if width < 28 or height < 14:
            return 'none'
        if width < MIN_LABEL_WIDTH or height < MIN_LABEL_HEIGHT:
            return 'partial'
        return 'full'

    def _full_label_text(self, node):
        size_str = self.format_size(node.size)
        if getattr(node, 'is_other', False):
            dirs = getattr(node, 'other_dir_count', 0)
            files = getattr(node, 'other_file_count', 0)
            parts = []
            if dirs:
                parts.append(f"{dirs} folder{'s' if dirs != 1 else ''}")
            if files:
                parts.append(f"{files} file{'s' if files != 1 else ''}")
            count_line = ' / '.join(parts) if parts else 'items'
            return f"{count_line}\n{size_str}"
        return f"{node.name}\n{size_str}"

    def _draw_node_label(self, node, x, y, width, height):
        mode = self._label_display_mode(width, height)
        node.label_fits = (mode == 'full')
        if mode == 'none':
            return

        name = node.name
        size_str = self.format_size(node.size)

        if mode == 'partial':
            text = size_str if width < 70 else name
            font_size = 7
        else:
            text = f"{name}\n{size_str}"
            font_size = min(10, max(7, int(min(width, height) / 12)))

        pad = 3
        self.canvas.create_text(x + width // 2, y + height // 2, text=text,
                                anchor=tk.CENTER, font=('Segoe UI', font_size),
                                fill='#1a1a2e', width=max(1, width - 2 * pad))

    def _draw_other_label(self, node, x, y, width, height):
        mode = self._label_display_mode(width, height)
        node.label_fits = (mode == 'full')
        if mode == 'none':
            return
        dirs = getattr(node, 'other_dir_count', 0)
        files = getattr(node, 'other_file_count', 0)
        size_str = self.format_size(node.size)
        parts = []
        if dirs:
            parts.append(f"{dirs} folder{'s' if dirs != 1 else ''}")
        if files:
            parts.append(f"{files} file{'s' if files != 1 else ''}")
        count_line = ' / '.join(parts) if parts else 'items'
        if mode == 'partial':
            text = size_str
            font_size = 7
        else:
            text = f"{count_line}\n{size_str}"
            font_size = min(10, max(7, int(min(width, height) / 12)))
        pad = 3
        self.canvas.create_text(x + width // 2, y + height // 2, text=text,
                                anchor=tk.CENTER, font=('Segoe UI', font_size),
                                fill='#1a1a2e', width=max(1, width - 2 * pad))
    
    @staticmethod
    def _partition_pixels(total, weights, min_unit=False):
        """Split total pixels across weights; sum(result) == total exactly."""
        total = max(0, int(round(total)))
        n = len(weights)
        if n == 0:
            return []
        if total == 0:
            return [0] * n
        weight_sum = sum(weights)
        if weight_sum <= 0:
            return [0] * n
        if n == 1:
            return [total]

        if min_unit:
            positive = [i for i, w in enumerate(weights) if w > 0]
            if not positive:
                return [0] * n
            if len(positive) <= total:
                sizes = [0] * n
                for i in positive:
                    sizes[i] = 1
                extra_total = total - len(positive)
                if extra_total > 0:
                    extra_weights = [weights[i] for i in positive]
                    extra = DiskSpaceVisualizer._partition_pixels(extra_total, extra_weights)
                    for idx, px in zip(positive, extra):
                        sizes[idx] += px
                return sizes
            order = sorted(positive, key=lambda i: weights[i], reverse=True)[:total]
            sizes = [0] * n
            for i in order:
                sizes[i] = 1
            return sizes

        raw = [total * w / weight_sum for w in weights]
        sizes = [int(r) for r in raw]
        leftover = total - sum(sizes)
        if leftover:
            order = sorted(range(n), key=lambda i: raw[i] - sizes[i], reverse=True)
            for k in range(leftover):
                sizes[order[k % n]] += 1
        return sizes

    @staticmethod
    def _split_strip(total, primary, secondary):
        """Partition total px between a laid-out row and remaining nodes."""
        if secondary <= 0:
            return total, 0
        if primary <= 0:
            return 0, total
        if total <= 1:
            if secondary <= 0:
                return total, 0
            if primary <= 0:
                return 0, total
            return 1, 0
        a, b = DiskSpaceVisualizer._partition_pixels(total, [primary, secondary])
        if a == 0:
            return 1, total - 1
        if b == 0:
            return total - 1, 1
        return a, b

    def _is_synthetic(self, node):
        return getattr(node, 'is_synthetic', False)

    def _other_threshold_bytes(self, fallback_total=1):
        """Size cutoff for Other: OTHER_SIZE_PCT of the current view root."""
        view = self.view_root or self.tree_root
        ref = view.size if view and view.size > 0 else fallback_total
        return ref * (OTHER_SIZE_PCT / 100.0)

    def _make_other_node(self, children):
        node = Node(name='Other', is_dir=True)
        node.is_synthetic = True
        node.is_other = True
        node.grouped_children = list(children)
        node.size = sum(ch.size for ch in children)
        node.other_dir_count = sum(1 for ch in children if ch.is_dir)
        node.other_file_count = sum(1 for ch in children if not ch.is_dir)
        return node

    def _group_small_entries(self, nodes, total_size):
        """Merge siblings each below OTHER_SIZE_PCT of view root into one Other tile."""
        if not nodes or total_size <= 0:
            return nodes
        threshold = self._other_threshold_bytes(total_size)
        kept = []
        other_list = []
        for node in nodes:
            if self._is_synthetic(node):
                kept.append(node)
            elif node.size <= threshold:
                other_list.append(node)
            else:
                kept.append(node)
        if not other_list:
            return nodes
        kept.append(self._make_other_node(other_list))
        kept.sort(key=lambda n: n.size, reverse=True)
        return kept

    def _prepare_treemap_nodes(self, nodes, width, height):
        nodes = [n for n in nodes if n.size > 0]
        if not nodes:
            return nodes
        nodes.sort(key=lambda n: n.size, reverse=True)
        total_size = sum(n.size for n in nodes)
        nodes = self._group_small_entries(nodes, total_size)
        nodes.sort(key=lambda n: n.size, reverse=True)
        return nodes

    def draw_treemap(self, nodes, x, y, width, height, level, max_draw_depth):
        w = int(round(width))
        h = int(round(height))
        if w < 1 or h < 1:
            return
        nodes = self._prepare_treemap_nodes(nodes, w, h)
        if not nodes:
            return
        total_size = sum(n.size for n in nodes)
        if total_size == 0:
            return
        if len(nodes) == 1:
            self.draw_node_recursive(nodes[0], x, y, w, h, level, max_draw_depth)
            return
        if w < 2 or h < 2:
            self._layout_slice(nodes, x, y, w, h, level, max_draw_depth)
            return
        self._squarify_iter(nodes, x, y, w, h, level, max_draw_depth)

    def _layout_slice(self, nodes, x, y, width, height, level, max_draw_depth):
        """Proportional slice layout; single pass, no recursion between rows."""
        x0 = int(round(x))
        y0 = int(round(y))
        w = int(round(width))
        h = int(round(height))
        if w < 1 or h < 1:
            return
        nodes = self._prepare_treemap_nodes(nodes, w, h)
        if not nodes:
            return
        if len(nodes) == 1:
            self.draw_node_recursive(nodes[0], x0, y0, w, h, level, max_draw_depth)
            return

        sizes = [n.size for n in nodes]
        if w >= h:
            widths = self._partition_pixels(w, sizes)
            cx = x0
            for n, nw in zip(nodes, widths):
                if nw > 0:
                    self.draw_node_recursive(n, cx, y0, nw, h, level, max_draw_depth)
                cx += nw
        else:
            heights = self._partition_pixels(h, sizes)
            cy = y0
            for n, nh in zip(nodes, heights):
                if nh > 0:
                    self.draw_node_recursive(n, x0, cy, w, nh, level, max_draw_depth)
                cy += nh

    def _worst_ratio(self, row, side_length):
        if not row or side_length <= 0:
            return float('inf')
        s = sum(n.size for n in row)
        r_max = max(n.size for n in row)
        r_min = min(n.size for n in row)
        side_sq = side_length * side_length
        return max(side_sq * r_max / (s * s), (s * s) / (side_sq * r_min))

    def _squarify_iter(self, nodes, x, y, width, height, level, max_draw_depth):
        """Iterative squarified treemap (no Python recursion between rows)."""
        stack = [(nodes, int(round(x)), int(round(y)),
                  int(round(width)), int(round(height)))]
        while stack:
            nodes, x0, y0, w_tot, h_tot = stack.pop()
            if w_tot < 1 or h_tot < 1:
                continue
            nodes = self._prepare_treemap_nodes(nodes, w_tot, h_tot)
            if not nodes:
                continue
            if len(nodes) == 1:
                self.draw_node_recursive(nodes[0], x0, y0, w_tot, h_tot, level, max_draw_depth)
                continue
            if w_tot < 2 or h_tot < 2:
                self._layout_slice(nodes, x0, y0, w_tot, h_tot, level, max_draw_depth)
                continue

            total_size = sum(n.size for n in nodes)
            row = []
            remaining = list(nodes)
            laid_out = False

            while remaining:
                node = remaining[0]
                candidate = row + [node]
                side = min(w_tot, h_tot)
                if (not row or
                        self._worst_ratio(candidate, side) <= self._worst_ratio(row, side)):
                    row.append(remaining.pop(0))
                else:
                    followups = self._place_row(
                        row, remaining, x0, y0, w_tot, h_tot, total_size, level, max_draw_depth)
                    stack.extend(followups)
                    laid_out = True
                    break

            if not laid_out and row:
                followups = self._place_row(
                    row, remaining, x0, y0, w_tot, h_tot, total_size, level, max_draw_depth)
                stack.extend(followups)

    def _place_row(self, row, remaining, x0, y0, w_tot, h_tot, total_size,
                   level, max_draw_depth):
        """Draw one squarify row; return stack tasks for the remaining region."""
        row_sum = sum(n.size for n in row)
        if row_sum == 0:
            if remaining:
                return [(remaining, x0, y0, w_tot, h_tot)]
            return []

        if remaining and (w_tot < 2 or h_tot < 2):
            self._layout_slice(row + remaining, x0, y0, w_tot, h_tot, level, max_draw_depth)
            return []

        row_sizes = [n.size for n in row]
        tasks = []

        if w_tot >= h_tot:
            if remaining:
                rem_sum = sum(n.size for n in remaining)
                strip_w, rest_w = self._split_strip(w_tot, row_sum, rem_sum)
                if rest_w <= 0 or strip_w <= 0:
                    self._layout_slice(row + remaining, x0, y0, w_tot, h_tot, level, max_draw_depth)
                    return []
            else:
                strip_w, rest_w = w_tot, 0

            cur_y = y0
            for n, rh in zip(row, self._partition_pixels(h_tot, row_sizes, min_unit=True)):
                if rh > 0 and strip_w > 0:
                    self.draw_node_recursive(n, x0, cur_y, strip_w, rh, level, max_draw_depth)
                cur_y += rh

            if remaining and rest_w > 0:
                tasks.append((remaining, x0 + strip_w, y0, rest_w, h_tot))
        else:
            if remaining:
                rem_sum = sum(n.size for n in remaining)
                strip_h, rest_h = self._split_strip(h_tot, row_sum, rem_sum)
                if rest_h <= 0 or strip_h <= 0:
                    self._layout_slice(row + remaining, x0, y0, w_tot, h_tot, level, max_draw_depth)
                    return []
            else:
                strip_h, rest_h = h_tot, 0

            cur_x = x0
            for n, rw in zip(row, self._partition_pixels(w_tot, row_sizes, min_unit=True)):
                if rw > 0 and strip_h > 0:
                    self.draw_node_recursive(n, cur_x, y0, rw, strip_h, level, max_draw_depth)
                cur_x += rw

            if remaining and rest_h > 0:
                tasks.append((remaining, x0, y0 + strip_h, w_tot, rest_h))

        return tasks
    
    def _node_at(self, x, y):
        for node, x1, y1, x2, y2 in reversed(self.rectangles):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return node
        return None

    def _hide_tooltip(self):
        self._tooltip_node = None
        self.tooltip.hide()

    @staticmethod
    def _set_readonly_entry(entry, var, text):
        entry.config(state='normal')
        var.set(text)
        entry.config(state='readonly')

    def _clear_item_info(self):
        self._set_readonly_entry(self.info_path_entry, self.info_path_var, '')
        self._set_readonly_entry(self.info_detail_entry, self.info_detail_var, '')

    def _other_group_path(self, node):
        children = getattr(node, 'grouped_children', None) or []
        if children and children[0].parent:
            return children[0].parent.full_path()
        view = self.view_root or self.tree_root
        return view.full_path() if view else ''

    def _build_info_lines(self, node):
        if getattr(node, 'is_other', False):
            parent_path = self._other_group_path(node)
            line1 = f"{parent_path}{os.sep}Other" if parent_path else "Other"
            dirs = getattr(node, 'other_dir_count', 0)
            files = getattr(node, 'other_file_count', 0)
            details = [
                "Other (small items grouped)",
                f"Size: {self.format_size(node.size)}",
                f"Folders: {dirs}",
                f"Files: {files}",
                f"Total items: {dirs + files}",
            ]
            return line1, ' | '.join(details)
        line1 = node.full_path()
        details = [
            f"Type: {'Directory' if node.is_dir else 'File'}",
            f"Size: {self.format_size(node.size)}",
        ]
        if node.is_dir and node.children:
            details.append(f"Contains: {len(node.children)} items")
        return line1, ' | '.join(details)

    def show_context_node_info(self):
        node = self.context_node
        if not node:
            self._clear_item_info()
            return
        line1, line2 = self._build_info_lines(node)
        self._set_readonly_entry(self.info_path_entry, self.info_path_var, line1)
        self._set_readonly_entry(self.info_detail_entry, self.info_detail_var, line2)

    def on_canvas_motion(self, event):
        node = self._node_at(event.x, event.y)
        if node is None or getattr(node, 'label_fits', True):
            self._hide_tooltip()
            return
        self._tooltip_node = node
        self.tooltip.show(
            self._full_label_text(node),
            event.x_root + 14,
            event.y_root + 14,
        )

    def on_canvas_leave(self, _event=None):
        self._hide_tooltip()
    
    def on_canvas_click(self, event):
        clicked_node = self._node_at(event.x, event.y)
        if clicked_node:
            self.context_node = clicked_node
            self.show_context_node_info()
        else:
            self.context_node = None
            self._clear_item_info()
    
    def on_canvas_right_click(self, event):
        self._hide_tooltip()
        node = self._node_at(event.x, event.y)
        if not node:
            return
        self.context_node = node
        menu = self.context_menu
        menu.entryconfig("Expand to full view",
                         state='normal' if node.is_dir and not self._is_synthetic(node) else 'disabled')
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
    
    def on_canvas_resize(self, event=None):
        if self.tree_root:
            # Redraw after a short delay to avoid too frequent redraws during resize
            self.root.after(100, self.draw_visualization)
    
    @staticmethod
    def format_size(size_bytes):
        """Format bytes as human readable string"""
        if size_bytes == 0:
            return "0 B"
        
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        unit_index = 0
        size = float(size_bytes)
        
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1
        
        return f"{size:.1f} {units[unit_index]}"

def main():
    root = tk.Tk()
    app = DiskSpaceVisualizer(root)
    root.mainloop()

if __name__ == "__main__":
    main()