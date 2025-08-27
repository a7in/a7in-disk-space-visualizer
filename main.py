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
        self.rectangles = []  # List of (node, x1, y1, x2, y2) tuples
        self.scanning = False
        
        # Colors for visualization
        self.colors = [
            "#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEAA7",
            "#DDA0DD", "#98D8C8", "#F7DC6F", "#BB8FCE", "#85C1E9",
            "#F8C471", "#82E0AA", "#F1948A", "#85C1E9", "#D7BDE2"
        ]
        
        self.setup_ui()
        
    def setup_ui(self):
        # Top frame for controls
        control_frame = ttk.Frame(self.root)
        control_frame.pack(fill=tk.X, padx=10, pady=5)
        
        # Drive selection
        ttk.Label(control_frame, text="Drive:").pack(side=tk.LEFT, padx=(0, 5))
        
        self.drive_var = tk.StringVar(value="C")
        drives = self.get_available_drives()
        self.drive_combo = ttk.Combobox(control_frame, textvariable=self.drive_var, 
                                       values=drives, width=5, state="readonly")
        self.drive_combo.pack(side=tk.LEFT, padx=(0, 20))
        self.drive_combo.bind('<<ComboboxSelected>>', self.on_drive_changed)
        
        # Depth selection
        ttk.Label(control_frame, text="Depth:").pack(side=tk.LEFT, padx=(0, 5))
        
        self.depth_var = tk.StringVar(value="2")
        self.depth_combo = ttk.Combobox(control_frame, textvariable=self.depth_var,
                                       values=["1", "2", "3", "4", "5"], width=5, state="readonly")
        self.depth_combo.pack(side=tk.LEFT, padx=(0, 20))
        self.depth_combo.bind('<<ComboboxSelected>>', self.on_depth_changed)
        
        # Scan button
        self.scan_button = ttk.Button(control_frame, text="Scan", command=self.start_scan)
        self.scan_button.pack(side=tk.LEFT, padx=(0, 20))
        
        # Progress bar
        self.progress = ttk.Progressbar(control_frame, mode='indeterminate')
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 20))
        
        # Status label
        self.status_label = ttk.Label(control_frame, text="Ready")
        self.status_label.pack(side=tk.RIGHT)
        
        # Canvas for visualization
        canvas_frame = ttk.Frame(self.root)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        
        self.canvas = tk.Canvas(canvas_frame, bg='white')
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        # Bind events
        self.canvas.bind('<Button-1>', self.on_canvas_click)
        self.canvas.bind('<Configure>', self.on_canvas_resize)
        
        # Initial scan
        self.start_scan()
    
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
        self.scanning = False
        self.scan_button.config(state='normal')
        self.progress.stop()
        
        if tree_root:
            self.status_label.config(text=f"Scanned: {self.format_size(tree_root.size)}")
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
    
    def draw_visualization(self):
        if not self.tree_root:
            return
            
        self.canvas.delete("all")
        self.rectangles.clear()
        
        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()
        
        if canvas_width <= 1 or canvas_height <= 1:
            self.root.after(100, self.draw_visualization)
            return
        
        # Получаем глубину отрисовки из интерфейса
        draw_depth = int(self.depth_var.get())
        
        # Draw root and its children recursively with limited draw depth
        self.draw_node_recursive(self.tree_root, 0, 0, canvas_width, canvas_height, 0, draw_depth)
    
    def draw_node_recursive(self, node, x, y, width, height, level, max_draw_depth):
        if not node or width < 2 or height < 2:
            return
        
        # Draw current node rectangle
        color = self.colors[level % len(self.colors)]
        
        # Make directories slightly transparent by using a lighter shade
        if node.is_dir and level > 0:
            # Create lighter version of color for directories
            rect_id = self.canvas.create_rectangle(x, y, x + width, y + height,
                                                 fill=color, outline='black', width=1)
        else:
            rect_id = self.canvas.create_rectangle(x, y, x + width, y + height,
                                                 fill=color, outline='black', width=2)
        
        # Store rectangle info for click detection
        node.rect_coords = (x, y, x + width, y + height)
        node.canvas_id = rect_id
        self.rectangles.append((node, x, y, x + width, y + height))
        
        # Add text label if rectangle is large enough
        if width > 50 and height > 20:
            text = f"{node.name}\n{self.format_size(node.size)}"
            self.canvas.create_text(x + width//2, y + height//2, text=text,
                                  anchor=tk.CENTER, font=('Arial', 8), 
                                  fill='blue')
        
        # Draw children if this is a directory, has children, and we haven't reached max draw depth
        if node.is_dir and node.children and level < max_draw_depth:
            total_size = sum(child.size for child in node.children if child.size > 0)
            if total_size == 0:
                return
            
            # Sort children by size (largest first) for better visualization
            children = sorted([ch for ch in node.children if ch.size > 0], 
                            key=lambda ch: ch.size, reverse=True)
            
            # Use treemap algorithm to layout children
            self.draw_treemap(children, x + 2, y + 2, width - 4, height - 4, level + 1, max_draw_depth)
    
    def draw_treemap(self, nodes, x, y, width, height, level, max_draw_depth):
        if not nodes or width < 4 or height < 4:
            return
        
        total_size = sum(node.size for node in nodes)
        if total_size == 0:
            return
        
        # Simple treemap algorithm - alternate between horizontal and vertical splits
        vertical_split = width > height
        
        if len(nodes) == 1:
            self.draw_node_recursive(nodes[0], x, y, width, height, level, max_draw_depth)
            return
        
        if vertical_split:
            # Split vertically
            current_x = x
            remaining_width = width
            for i, node in enumerate(nodes):
                if i == len(nodes) - 1:  # Last node gets remaining space
                    node_width = remaining_width
                else:
                    node_width = max(1, int(width * node.size / total_size))
                    remaining_width -= node_width
                
                if node_width > 0:
                    self.draw_node_recursive(node, current_x, y, node_width, height, level, max_draw_depth)
                    current_x += node_width
        else:
            # Split horizontally  
            current_y = y
            remaining_height = height
            for i, node in enumerate(nodes):
                if i == len(nodes) - 1:  # Last node gets remaining space
                    node_height = remaining_height
                else:
                    node_height = max(1, int(height * node.size / total_size))
                    remaining_height -= node_height
                
                if node_height > 0:
                    self.draw_node_recursive(node, x, current_y, width, node_height, level, max_draw_depth)
                    current_y += node_height
    
    def on_canvas_click(self, event):
        # Find which rectangle was clicked (search from smallest to largest to get the most specific)
        x, y = event.x, event.y
        clicked_node = None
        
        # Find the smallest rectangle that contains the click point
        # (rectangles added later are typically smaller/more nested)
        for node, x1, y1, x2, y2 in reversed(self.rectangles):
            if x1 <= x <= x2 and y1 <= y <= y2:
                clicked_node = node
                break
        
        if clicked_node:
            # Show info for clicked node
            info = f"Path: {clicked_node.full_path()}\n"
            info += f"Size: {self.format_size(clicked_node.size)}\n"
            info += f"Type: {'Directory' if clicked_node.is_dir else 'File'}"
            
            if clicked_node.is_dir and clicked_node.children:
                info += f"\nContains: {len(clicked_node.children)} items"
            
            messagebox.showinfo("Item Info", info)
    
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