"""清单取件：标准库桌面界面。

业务扫描与文件输出由 core.py 完成；工作线程通过队列向 Tk 主线程报告结果。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from list_io import read_list_file


APP_TITLE = "清单取件"
APP_VERSION = "0.2.1 试验版"
STATUS_NAMES = {
    "unique": "唯一匹配",
    "selected": "人工选择",
    "missing": "未找到",
    "ambiguous": "多个候选",
    "multiple": "多文件命中",
    "duplicate": "清单重复",
    "invalid": "无效项",
    "uncertain": "未确定",
}
FILTERS = {"全部状态": None, **{value: key for key, value in STATUS_NAMES.items()}}
MODE_LABELS = {"full": "完整文件名", "stem": "不含扩展名", "regex": "正则表达式", "extension": "文件后缀"}
MODE_HELP = {
    "full": "示例：合同.pdf。精确匹配整个文件名，同名需选一份。",
    "stem": "示例：合同。忽略最后一个扩展名，同名需选一份。",
    "regex": "示例：^合同.*\\.pdf$。搜索完整文件名；^ 和 $ 限定边界。",
    "extension": "每行可填 pdf / .pdf / *.pdf；[无后缀] 表示没有扩展名。",
}
BG = "#F3F5F8"
PAPER = "#FFFFFF"
INK = "#172B46"
MUTED = "#64748B"
BLUE = "#235DD7"
BORDER = "#DAE1EA"


def count_list_items(text: str) -> int:
    """Keep UI counts consistent with core.scan, including U+2028 in names."""
    return sum(1 for line in re.split(r"\r\n|\n|\r", text) if line.strip())


class ListPickApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_TITLE} {APP_VERSION} · 按清单查找与复制文件")
        initial_height = max(680, min(800, self.root.winfo_screenheight() - 100))
        self.root.geometry(f"1140x{initial_height}")
        self.root.minsize(1100, 680)
        self.root.configure(bg=BG)
        self.events: queue.Queue = queue.Queue()
        self.cancel_event: threading.Event | None = None
        self.busy_kind: str | None = None
        self.closing = False
        self.result: dict | None = None
        self.result_dirty = False
        self.demo_active = False
        self._loading_inputs = False
        self.last_output: str | None = None
        self.visible_rows: dict[str, dict] = {}
        self.input_widgets: list[tk.Widget] = []
        self.output_widgets: list[tk.Widget] = []
        self.source_var = tk.StringVar()
        self.parent_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="full")
        self.mode_label_var = tk.StringVar(value=MODE_LABELS["full"])
        self.mode_help_var = tk.StringVar(value=MODE_HELP["full"])
        self.include_var = tk.StringVar()
        self.exclude_var = tk.StringVar()
        self.search_var = tk.StringVar()
        self.display_count_var = tk.StringVar(value="尚未扫描")
        self.recursive_var = tk.BooleanVar(value=True)
        self.case_var = tk.BooleanVar(value=False)
        self.filter_var = tk.StringVar(value="全部状态")
        self.status_var = tk.StringVar(value="先粘贴清单并选择源文件夹，再扫描核对。")
        self.result_note_var = tk.StringVar(value="扫描后在这里核对，多个候选需明确勾选。")
        self.list_count_var = tk.StringVar(value="0 行")
        self.context_var = tk.StringVar(value="本地处理  /  原文件不改动")
        self.selection_count_var = tk.StringVar(value="计划 0 个文件 / 0 行")
        self.summary_text_var = tk.StringVar(value="扫描后按清单行统计结果")
        self.advanced_var = tk.BooleanVar(value=False)
        self.advanced_caption_var = tk.StringVar(value="展开扫描选项")
        self.summary_vars = {
            "total": tk.StringVar(value="—"),
            "unique": tk.StringVar(value="—"),
            "missing": tk.StringVar(value="—"),
            "ambiguous": tk.StringVar(value="—"),
            "duplicate": tk.StringVar(value="—"),
            "other": tk.StringVar(value="—"),
        }
        self._style()
        self._load_assets()
        self._build()
        self.source_var.trace_add("write", lambda *_args: self._input_changed(origin="source"))
        self.mode_var.trace_add("write", self._mode_changed)
        self.mode_label_var.trace_add("write", self._mode_label_changed)
        self.include_var.trace_add("write", self._input_changed)
        self.exclude_var.trace_add("write", self._input_changed)
        self.search_var.trace_add("write", self._filter_changed)
        self.recursive_var.trace_add("write", self._input_changed)
        self.case_var.trace_add("write", self._input_changed)
        self.filter_var.trace_add("write", self._filter_changed)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_actions()
        self.root.after(80, self._poll)

    def _style(self) -> None:
        self.root.option_add("*Font", "{Microsoft YaHei UI} 10")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PAPER)
        style.configure("TLabel", background=PAPER, foreground=INK, font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", foreground=MUTED, background=PAPER, font=("Microsoft YaHei UI", 9))
        style.configure("Step.TLabel", foreground=INK, background=PAPER, font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 7), background="#EEF2F7", foreground=INK, borderwidth=0)
        style.map("TButton", background=[("active", "#E1E8F2"), ("disabled", "#F0F2F5")], foreground=[("disabled", "#9AA5B5")])
        style.configure("Primary.TButton", background=BLUE, foreground="white", padding=(16, 9), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#184CB8"), ("disabled", "#DCE3EF")], foreground=[("disabled", "#8796AD")])
        style.configure("TRadiobutton", background=PAPER, foreground=INK, font=("Microsoft YaHei UI", 9))
        style.configure("TCheckbutton", background=PAPER, foreground=INK, font=("Microsoft YaHei UI", 9))
        style.configure("TEntry", padding=7, fieldbackground=PAPER, foreground=INK, bordercolor=BORDER)
        style.configure("TCombobox", padding=5, fieldbackground=PAPER, foreground=INK, bordercolor=BORDER)
        row_height = max(29, tkfont.Font(self.root, family="Microsoft YaHei UI", size=9).metrics("linespace") + 6)
        style.configure("Treeview", background=PAPER, fieldbackground=PAPER, foreground=INK, rowheight=row_height, font=("Microsoft YaHei UI", 9), borderwidth=0)
        style.configure("Treeview.Heading", background="#EDF2F8", foreground=INK, font=("Microsoft YaHei UI", 9, "bold"), relief="flat", padding=(8, 8))
        style.map("Treeview", background=[("selected", "#DFEAFE")], foreground=[("selected", "#123F84")])
        style.configure("Horizontal.TProgressbar", background=BLUE, troughcolor="#E7ECF3", borderwidth=0, lightcolor=BLUE, darkcolor=BLUE, thickness=3)
        style.configure("Compact.TButton", padding=(9, 4))

    def _load_assets(self) -> None:
        self._window_icon = None
        self._brand_icon = None
        resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        try:
            ico = resource_root / "assets" / "list-pick.ico"
            if ico.is_file():
                self.root.iconbitmap(str(ico))
            png = resource_root / "assets" / "list-pick.png"
            if png.is_file():
                self._window_icon = tk.PhotoImage(file=str(png))
                self.root.iconphoto(False, self._window_icon)
                self._brand_icon = self._window_icon.subsample(max(1, self._window_icon.width() // 32), max(1, self._window_icon.height() // 32))
        except (tk.TclError, OSError):
            # Optional branding must never prevent access to the local tool.
            self._brand_icon = None

    def _card(self, parent: tk.Widget, **grid_options) -> ttk.Frame:
        outer = tk.Frame(parent, bg=PAPER, highlightbackground=BORDER, highlightthickness=1)
        outer.grid(**grid_options)
        inner = ttk.Frame(outer, style="Card.TFrame", padding=8)
        inner.pack(fill="both", expand=True)
        return inner

    def _build(self) -> None:
        self.root.columnconfigure(0, weight=0, minsize=405)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(1, weight=1)
        header = tk.Frame(self.root, bg=INK, padx=22, pady=6)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        if self._brand_icon is not None:
            tk.Label(header, image=self._brand_icon, bg=INK).pack(side="left", padx=(0, 10))
        tk.Label(header, text=APP_TITLE, font=("Microsoft YaHei UI", 17, "bold"), bg=INK, fg="white").pack(side="left")
        tk.Label(header, text="查找  /  核对  /  复制", font=("Microsoft YaHei UI", 9), bg=INK, fg="#C7D4E7").pack(side="left", padx=(18, 0))
        self.context_label = tk.Label(header, textvariable=self.context_var, font=("Microsoft YaHei UI", 9), bg=INK, fg="#D6E2F3")
        self.context_label.pack(side="right")

        preparation_outer = ttk.Frame(self.root, padding=(20, 12, 0, 0))
        preparation_outer.grid(row=1, column=0, sticky="nsew")
        preparation_outer.columnconfigure(0, weight=1)
        preparation_outer.rowconfigure(0, weight=1)
        preparation_canvas = tk.Canvas(preparation_outer, bg=BG, highlightthickness=0, width=383)
        preparation_canvas.grid(row=0, column=0, sticky="nsew")
        preparation_scroll = ttk.Scrollbar(preparation_outer, orient="vertical", command=preparation_canvas.yview)
        preparation_scroll.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        preparation_canvas.configure(yscrollcommand=preparation_scroll.set)
        preparation = ttk.Frame(preparation_canvas)
        preparation_window = preparation_canvas.create_window(0, 0, anchor="nw", window=preparation)
        self._preparation_canvas = preparation_canvas

        def size_preparation(_event=None) -> None:
            width = max(1, preparation_canvas.winfo_width())
            height = max(preparation.winfo_reqheight(), preparation_canvas.winfo_height())
            preparation_canvas.itemconfigure(preparation_window, width=width, height=height)
            preparation_canvas.configure(scrollregion=(0, 0, width, height))

        preparation_canvas.bind("<Configure>", size_preparation)
        preparation.bind("<Configure>", size_preparation)
        preparation.columnconfigure(0, weight=1)
        preparation.rowconfigure(0, weight=1)
        list_card = self._card(preparation, row=0, column=0, sticky="nsew")
        list_card.columnconfigure(0, weight=1)
        list_card.rowconfigure(3, weight=1)
        list_head = ttk.Frame(list_card, style="Card.TFrame")
        list_head.grid(row=0, column=0, sticky="ew")
        ttk.Label(list_head, text="1. 准备清单", style="Step.TLabel").pack(side="left")
        ttk.Label(list_head, textvariable=self.list_count_var, style="Muted.TLabel").pack(side="right")
        modes = ttk.Frame(list_card, style="Card.TFrame")
        modes.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        ttk.Label(modes, text="匹配方式", style="Muted.TLabel").pack(side="left", padx=(0, 10))
        self.mode_combo = ttk.Combobox(modes, textvariable=self.mode_label_var, values=list(MODE_LABELS.values()), state="readonly", width=20)
        self.mode_combo.pack(side="left", fill="x", expand=True)
        self.input_widgets.append(self.mode_combo)
        mode_help = tk.Label(list_card, textvariable=self.mode_help_var, bg=PAPER, fg=MUTED, font=("Microsoft YaHei UI", 9), height=2, anchor="w", justify="left", wraplength=350)
        mode_help.grid(row=2, column=0, sticky="ew", pady=(0, 5))
        mode_help.bind("<Configure>", lambda event: mode_help.configure(wraplength=max(200, event.width - 4)))
        text_frame = tk.Frame(list_card, bg=PAPER, highlightbackground=BORDER, highlightthickness=1)
        text_frame.grid(row=3, column=0, sticky="nsew")
        self.list_text = tk.Text(text_frame, height=4, width=36, wrap="none", relief="flat", bg="#FBFCFE", fg=INK, insertbackground=INK, padx=9, pady=5, undo=True, font=("Microsoft YaHei UI", 10))
        self.list_text.pack(side="left", fill="both", expand=True)
        list_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.list_text.yview)
        list_scroll.pack(side="right", fill="y")
        self.list_text.configure(yscrollcommand=list_scroll.set)
        self.list_text.bind("<<Modified>>", self._list_changed)
        self.list_text.bind("<Tab>", lambda event: self._leave_list(event, False))
        self.list_text.bind("<Shift-Tab>", lambda event: self._leave_list(event, True))
        self.list_text.bind("<ISO_Left_Tab>", lambda event: self._leave_list(event, True))
        self.input_widgets.append(self.list_text)
        list_buttons = ttk.Frame(list_card, style="Card.TFrame")
        list_buttons.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.import_button = ttk.Button(list_buttons, text="导入 TXT", command=self._import_list)
        self.import_button.pack(side="left")
        self.clear_button = ttk.Button(list_buttons, text="清空", command=self._clear_list)
        self.clear_button.pack(side="left", padx=(8, 0))
        self.demo_button = ttk.Button(list_buttons, text="加载演示", command=self._load_demo)
        self.demo_button.pack(side="right")
        self.input_widgets.extend([self.import_button, self.clear_button, self.demo_button])

        source_card = self._card(preparation, row=1, column=0, sticky="nsew", pady=(10, 0))
        source_card.columnconfigure(0, weight=1)
        ttk.Label(source_card, text="2. 选择源文件夹", style="Step.TLabel").grid(row=0, column=0, sticky="w")
        source_row = ttk.Frame(source_card, style="Card.TFrame")
        source_row.grid(row=1, column=0, sticky="ew", pady=(10, 6))
        source_row.columnconfigure(0, weight=1)
        self.source_entry = ttk.Entry(source_row, textvariable=self.source_var)
        self.source_entry.grid(row=0, column=0, sticky="ew")
        self.source_button = ttk.Button(source_row, text="选择文件夹", command=self._choose_source)
        self.source_button.grid(row=0, column=1, padx=(8, 0))
        self.input_widgets.extend([self.source_entry, self.source_button])
        self.advanced_button = ttk.Button(source_card, textvariable=self.advanced_caption_var, command=self._toggle_advanced, style="Compact.TButton")
        self.advanced_button.grid(row=2, column=0, sticky="ew", pady=(2, 0))
        self.input_widgets.append(self.advanced_button)
        self.advanced_frame = ttk.Frame(source_card, style="Card.TFrame")
        self.advanced_frame.grid(row=3, column=0, sticky="ew", pady=(9, 0))
        self.advanced_frame.columnconfigure(0, weight=1)
        extension_filters = ttk.Frame(self.advanced_frame, style="Card.TFrame")
        extension_filters.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        extension_filters.columnconfigure(0, weight=1, uniform="extension")
        extension_filters.columnconfigure(1, weight=1, uniform="extension")
        ttk.Label(extension_filters, text="包含后缀（可选）", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(extension_filters, text="排除后缀（优先）", style="Muted.TLabel").grid(row=0, column=1, sticky="w", padx=(9, 0))
        self.include_entry = ttk.Entry(extension_filters, textvariable=self.include_var, width=15)
        self.include_entry.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.exclude_entry = ttk.Entry(extension_filters, textvariable=self.exclude_var, width=15)
        self.exclude_entry.grid(row=1, column=1, sticky="ew", padx=(9, 0), pady=(3, 0))
        for entry in (self.include_entry, self.exclude_entry):
            entry.bind("<FocusIn>", lambda _event: self.status_var.set("后缀可用空格、中文或英文逗号、分号分隔，例如 pdf,docx；排除优先。[无后缀] 表示无扩展名。"))
            self.input_widgets.append(entry)
        checks = ttk.Frame(self.advanced_frame, style="Card.TFrame")
        checks.grid(row=1, column=0, sticky="ew")
        for variable, label in [(self.recursive_var, "包含子文件夹"), (self.case_var, "区分大小写")]:
            check = ttk.Checkbutton(checks, text=label, variable=variable)
            check.pack(side="left", padx=(0, 15))
            self.input_widgets.append(check)
        self.advanced_frame.grid_remove()
        self.scan_button = ttk.Button(preparation_outer, text="扫描并核对", command=self._start_scan, style="Primary.TButton")
        self.scan_button.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        def scroll_preparation(event) -> str | None:
            if event.widget in (self.list_text, self.mode_combo):
                return None
            preparation_canvas.yview_scroll(-int(event.delta / 120), "units")
            return "break"

        def reveal_preparation(event) -> None:
            preparation_canvas.update_idletasks()
            widget_top = event.widget.winfo_rooty() - preparation.winfo_rooty()
            widget_bottom = widget_top + event.widget.winfo_height()
            view_top = preparation_canvas.canvasy(0)
            view_bottom = view_top + preparation_canvas.winfo_height()
            total_height = max(1, preparation.winfo_height())
            if widget_top < view_top:
                preparation_canvas.yview_moveto(widget_top / total_height)
            elif widget_bottom > view_bottom:
                preparation_canvas.yview_moveto((widget_bottom - preparation_canvas.winfo_height()) / total_height)

        def bind_preparation(widget) -> None:
            widget.bind("<MouseWheel>", scroll_preparation, add="+")
            if widget in self.input_widgets:
                widget.bind("<FocusIn>", reveal_preparation, add="+")
            for child in widget.winfo_children():
                bind_preparation(child)

        bind_preparation(preparation)

        result_outer = ttk.Frame(self.root, padding=(12, 12, 20, 0))
        result_outer.grid(row=1, column=1, sticky="nsew")
        result_outer.columnconfigure(0, weight=1)
        result_outer.rowconfigure(0, weight=1)
        result_card = self._card(result_outer, row=0, column=0, sticky="nsew")
        result_card.columnconfigure(0, weight=1)
        result_card.rowconfigure(4, weight=1)
        result_head = ttk.Frame(result_card, style="Card.TFrame")
        result_head.grid(row=0, column=0, sticky="ew")
        ttk.Label(result_head, text="核对结果", style="Step.TLabel").pack(side="left")
        ttk.Label(result_head, textvariable=self.display_count_var, style="Muted.TLabel").pack(side="left", padx=(12, 0))
        self.filter_combo = ttk.Combobox(result_head, textvariable=self.filter_var, values=list(FILTERS), state="readonly", width=13)
        self.filter_combo.pack(side="right")
        ttk.Label(result_head, text="显示", style="Muted.TLabel").pack(side="right", padx=(0, 8))
        summaries = tk.Label(result_card, textvariable=self.summary_text_var, bg="#F0F4FA", fg=INK, font=("Microsoft YaHei UI", 9), anchor="w", padx=9, pady=5, wraplength=600)
        summaries.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        self._summary_label = summaries
        summaries.bind("<Configure>", lambda event: summaries.configure(wraplength=max(200, event.width - 18)))
        self.result_note_label = ttk.Label(result_card, textvariable=self.result_note_var, style="Muted.TLabel", wraplength=600)
        self.result_note_label.grid(row=2, column=0, sticky="ew", pady=(0, 5))
        self.result_note_label.bind("<Configure>", lambda event: self.result_note_label.configure(wraplength=max(200, event.width - 4)))
        search_row = ttk.Frame(result_card, style="Card.TFrame")
        search_row.grid(row=3, column=0, sticky="ew", pady=(0, 5))
        search_row.columnconfigure(1, weight=1)
        ttk.Label(search_row, text="结果搜索", style="Muted.TLabel").grid(row=0, column=0, padx=(0, 8))
        self.search_entry = ttk.Entry(search_row, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew")
        self.search_clear_button = ttk.Button(search_row, text="清除", command=lambda: self.search_var.set(""), style="Compact.TButton")
        self.search_clear_button.grid(row=0, column=2, padx=(7, 9))
        ttk.Label(search_row, text="只影响显示", style="Muted.TLabel").grid(row=0, column=3)
        tree_frame = ttk.Frame(result_card, style="Card.TFrame")
        tree_frame.grid(row=4, column=0, sticky="nsew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        columns = ("line", "query", "status", "count", "location")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse", height=5)
        widths = {"line": (40, False), "query": (155, True), "status": (85, False), "count": (70, False), "location": (230, True)}
        headings = {"line": "行", "query": "清单项 / 规则", "status": "状态", "count": "匹配数", "location": "路径或原因"}
        for name in columns:
            self.tree.heading(name, text=headings[name], anchor="w")
            self.tree.column(name, width=widths[name][0], minwidth=widths[name][0] if not widths[name][1] else 140, stretch=widths[name][1], anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        horizontal_scroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        horizontal_scroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=tree_scroll.set, xscrollcommand=horizontal_scroll.set)
        for code, color in {"unique": "#127452", "selected": "#235DD7", "multiple": "#996017", "missing": "#9B531A", "ambiguous": "#996017", "duplicate": "#6A7280", "invalid": "#A13B3B", "uncertain": "#756A95"}.items():
            self.tree.tag_configure(code, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", self._show_selection)
        self.tree.bind("<Double-1>", self._double_click_candidate)
        candidate_bar = ttk.Frame(result_card, style="Card.TFrame")
        candidate_bar.grid(row=5, column=0, sticky="ew", pady=(5, 0))
        self.choose_candidate_button = ttk.Button(candidate_bar, text="选择候选文件", command=self._choose_candidate, style="Compact.TButton")
        self.choose_candidate_button.pack(side="left")
        self.clear_candidate_button = ttk.Button(candidate_bar, text="取消选择", command=self._clear_candidate, style="Compact.TButton")
        self.clear_candidate_button.pack(side="left", padx=(7, 0))
        ttk.Button(candidate_bar, text="详情 / 统计", command=self._open_details, style="Compact.TButton").pack(side="right")
        detail_frame = tk.Frame(result_card, bg="#F7F9FC", highlightbackground=BORDER, highlightthickness=1)
        self._detail_frame = detail_frame
        detail_frame.grid(row=6, column=0, sticky="ew", pady=(7, 0))
        self.detail_text = tk.Text(detail_frame, height=2, width=40, wrap="word", relief="flat", state="disabled", bg="#F7F9FC", fg=MUTED, padx=8, pady=5, font=("Microsoft YaHei UI", 9))
        self.detail_text.pack(side="left", fill="both", expand=True)
        detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self.detail_text.yview)
        detail_scroll.pack(side="right", fill="y")
        self.detail_text.configure(yscrollcommand=detail_scroll.set)
        self._set_detail("选中一行查看完整路径。多个候选需你明确选择，本工具不会自动选最新文件。")
        self.empty_label = tk.Label(self.tree, text="先准备清单并扫描\n匹配结果会显示在这里", bg=PAPER, fg=MUTED, font=("Microsoft YaHei UI", 11), justify="center")
        self.empty_label.place(relx=.5, rely=.55, anchor="center", relwidth=.9)

        export_outer = ttk.Frame(self.root, padding=(20, 7, 20, 6))
        export_outer.grid(row=2, column=0, columnspan=2, sticky="ew")
        export_outer.columnconfigure(0, weight=1)
        export_card = self._card(export_outer, row=0, column=0, sticky="ew")
        export_card.columnconfigure(0, weight=1)
        output_row = ttk.Frame(export_card, style="Card.TFrame")
        output_row.grid(row=0, column=0, sticky="ew")
        output_row.columnconfigure(1, weight=1)
        ttk.Label(output_row, text="3. 导出", style="Step.TLabel").grid(row=0, column=0, padx=(0, 12))
        self.parent_entry = ttk.Entry(output_row, textvariable=self.parent_var)
        self.parent_entry.grid(row=0, column=1, sticky="ew")
        self.parent_button = ttk.Button(output_row, text="选择输出位置", command=self._choose_parent)
        self.parent_button.grid(row=0, column=2, padx=(8, 12))
        self.report_button = ttk.Button(output_row, text="仅导出报告", command=lambda: self._start_export(False))
        self.report_button.grid(row=0, column=3, padx=(0, 8))
        self.copy_button = ttk.Button(output_row, text="复制全部计划", style="Primary.TButton", command=lambda: self._start_export(True))
        self.copy_button.grid(row=0, column=4)
        self.output_widgets.extend([self.parent_entry, self.parent_button])

        footer = ttk.Frame(self.root, padding=(22, 0, 22, 10))
        footer.grid(row=3, column=0, columnspan=2, sticky="ew")
        footer.columnconfigure(0, weight=1)
        progress_strip = tk.Frame(footer, height=4, bg="#E7ECF3")
        progress_strip.grid(row=0, column=0, sticky="ew", pady=(0, 7), columnspan=3)
        progress_strip.pack_propagate(False)
        self.progress = ttk.Progressbar(progress_strip, mode="indeterminate", length=100, maximum=100)
        self.progress.pack(fill="both", expand=True)
        self.status_label = tk.Label(footer, textvariable=self.status_var, bg=BG, fg=MUTED, anchor="w", justify="left", height=2, wraplength=600, font=("Microsoft YaHei UI", 9))
        self.status_label.grid(row=1, column=0, sticky="ew")
        self.status_label.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(100, event.width - 4)))
        self.open_button = ttk.Button(footer, text="打开输出文件夹", command=self._open_output, style="Compact.TButton")
        self.open_button.grid(row=1, column=1, padx=(10, 8))
        self.cancel_button = ttk.Button(footer, text="取消任务", command=self._cancel, style="Compact.TButton")
        self.cancel_button.grid(row=1, column=2)
        self.root.bind("<Control-f>", lambda _event: self.search_entry.focus_set())
        self.root.bind("<F5>", lambda _event: self._start_scan())
        self.root.bind("<Configure>", self._adapt_layout, add="+")
        self.list_text.focus_set()
        self._update_advanced_caption()

    def _adapt_layout(self, event=None) -> None:
        if event is not None and event.widget is not self.root:
            return
        scale = float(self.root.tk.call("tk", "scaling"))
        compact = self.root.winfo_height() < max(760, round(380 * scale))
        self.status_label.configure(height=1 if compact else 2)
        for widget in (self._summary_label, self._detail_frame):
            if compact:
                widget.grid_remove()
            else:
                widget.grid()

    def _open_details(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("核对详情与统计")
        dialog.transient(self.root)
        dialog.geometry("860x430")
        dialog.minsize(650, 320)
        dialog.configure(bg=PAPER)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        text = tk.Text(dialog, wrap="word", bg=PAPER, fg=INK, relief="flat", padx=16, pady=14, font=("Microsoft YaHei UI", 10))
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(dialog, orient="vertical", command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", self.summary_text_var.get() + "\n" + self.result_note_var.get() + "\n\n" + self.detail_text.get("1.0", "end-1c"))
        text.configure(state="disabled")
        ttk.Button(dialog, text="关闭", command=dialog.destroy).grid(row=1, column=0, columnspan=2, sticky="e", padx=16, pady=(0, 12))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    def _leave_list(self, event, backwards: bool) -> str:
        target = event.widget.tk_focusPrev() if backwards else event.widget.tk_focusNext()
        if target is not None:
            target.focus_set()
        return "break"

    def _toggle_advanced(self) -> None:
        self.advanced_var.set(not self.advanced_var.get())
        if self.advanced_var.get():
            self.advanced_frame.grid()
        else:
            self.advanced_frame.grid_remove()
        self._update_advanced_caption()

    def _update_advanced_caption(self) -> None:
        changed = bool(self.include_var.get().strip() or self.exclude_var.get().strip() or not self.recursive_var.get() or self.case_var.get())
        caption = "收起扫描选项" if self.advanced_var.get() else "展开扫描选项"
        self.advanced_caption_var.set(caption + (" · 已修改" if changed else " · 默认范围"))

    def _set_detail(self, text: str) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state="disabled")

    def _mode_label_changed(self, *_args) -> None:
        mode = next((key for key, label in MODE_LABELS.items() if label == self.mode_label_var.get()), None)
        if mode and mode != self.mode_var.get():
            self.mode_var.set(mode)

    def _mode_changed(self, *_args) -> None:
        mode = self.mode_var.get()
        label = MODE_LABELS.get(mode, mode)
        if self.mode_label_var.get() != label:
            self.mode_label_var.set(label)
        self.mode_help_var.set(MODE_HELP.get(mode, ""))
        self._input_changed()

    def _restore_unselected(self, row: dict) -> None:
        multiple = bool(self.result and self.result.get("mode") in ("regex", "extension"))
        row["status"] = "multiple" if multiple else "ambiguous"
        row.pop("selected_candidate", None)
        row.pop("selected_candidates", None)
        count = len(row.get("candidates", []))
        row["reason"] = f"找到 {count} 个匹配文件，请明确选择要复制的文件。" if multiple else f"找到 {count} 份同名文件，请人工核对。"

    @staticmethod
    def _row_selected_paths(row: dict) -> list[str]:
        if row.get("status") == "unique":
            return list(row.get("candidates", []))
        if row.get("status") != "selected":
            return []
        if "selected_candidates" in row:
            selected = row.get("selected_candidates")
            return list(selected) if isinstance(selected, list) else []
        selected = row.get("selected_candidate")
        return [selected] if isinstance(selected, str) and selected else []

    def _copy_plan(self) -> tuple[list[str], int]:
        if self.result is None or self.result_dirty or not self.result.get("complete", False):
            return [], 0
        planned: dict[str, str] = {}
        source_rows = 0
        for row in self.result.get("rows", []):
            paths = self._row_selected_paths(row)
            if paths:
                source_rows += 1
            for path in paths:
                planned.setdefault(os.path.normcase(os.path.abspath(path)), path)
        return list(planned.values()), source_rows

    def _input_changed(self, *_args, origin="options") -> None:
        self._update_advanced_caption()
        if self._loading_inputs:
            return
        if self.demo_active and origin in ("source", "list"):
            self._set_demo_active(False)
        self._invalidate_result()

    def _invalidate_result(self) -> None:
        if self.result is not None:
            self.result_dirty = True
            for row in self.result.get("rows", []):
                if row.get("status") == "selected":
                    self._restore_unselected(row)
            if self.filter_var.get() == "人工选择":
                self.filter_var.set("全部状态")
            self._render_rows()
            self.result_note_var.set("输入已变更，人工选择已清除。下方为旧结果，请重新扫描后再导出。")
            self.status_var.set("请重新扫描：当前清单或扫描选项已变更。")
        self._refresh_actions()

    def _list_changed(self, _event=None) -> None:
        if not self.list_text.edit_modified():
            return
        self.list_text.edit_modified(False)
        text = self.list_text.get("1.0", "end-1c")
        self.list_count_var.set(f"{count_list_items(text)} 行")
        self._input_changed(origin="list")

    def _filter_changed(self, *_args) -> None:
        if self.result is not None:
            self._render_rows()

    def _refresh_actions(self) -> None:
        if not hasattr(self, "copy_button"):
            return
        busy = self.busy_kind is not None
        current = self.result is not None and not self.result_dirty
        complete = bool(current and self.result.get("complete", False))
        eligible = bool(self._copy_plan()[0])
        self.scan_button.configure(state="disabled" if busy else "normal")
        self.copy_button.configure(state="normal" if not busy and complete and eligible else "disabled")
        self.report_button.configure(state="normal" if not busy and current else "disabled")
        self.cancel_button.configure(state="normal" if busy and not self.closing else "disabled")
        self.open_button.configure(state="normal" if not busy and self.last_output else "disabled")
        self.filter_combo.configure(state="disabled" if busy else "readonly")
        row = self._current_row()
        can_choose = bool(not busy and current and complete and row and row.get("status") in ("ambiguous", "multiple", "selected"))
        self.choose_candidate_button.configure(state="normal" if can_choose else "disabled")
        self.clear_candidate_button.configure(state="normal" if can_choose and row.get("status") == "selected" else "disabled")
        self.search_entry.configure(state="disabled" if busy else "normal")
        self.search_clear_button.configure(state="disabled" if busy else "normal")

    def _set_busy(self, kind: str | None) -> None:
        self.busy_kind = kind
        for widget in self.input_widgets + self.output_widgets:
            state = "disabled" if kind else ("readonly" if isinstance(widget, ttk.Combobox) else "normal")
            widget.configure(state=state)
        if kind:
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress["value"] = 0
        self._refresh_actions()

    def _choose_source(self) -> None:
        chosen = filedialog.askdirectory(title="选择要查找文件的源文件夹", parent=self.root)
        if chosen:
            self.source_var.set(chosen)

    def _choose_parent(self) -> None:
        chosen = filedialog.askdirectory(title="选择输出父文件夹（工具会新建一个结果文件夹）", parent=self.root)
        if chosen:
            self.parent_var.set(chosen)

    def _import_list(self) -> None:
        chosen = filedialog.askopenfilename(title="导入文件清单", filetypes=[("文本清单", "*.txt"), ("所有文件", "*.*")], parent=self.root)
        if not chosen:
            return
        try:
            text = read_list_file(chosen)
        except (OSError, ValueError) as exc:
            messagebox.showerror("清单未导入", str(exc), parent=self.root)
            return
        self._replace_list(text)
        self.status_var.set(f"已导入：{Path(chosen).name}。请扫描核对。")

    def _replace_list(self, text: str) -> None:
        self.list_text.delete("1.0", "end")
        self.list_text.insert("1.0", text)
        self.list_text.edit_modified(True)
        self._list_changed()

    def _clear_list(self) -> None:
        self._replace_list("")

    def _set_demo_active(self, active: bool) -> None:
        self.demo_active = active
        self.context_var.set("虚构演示样例  /  原文件不改动" if active else "本地处理  /  原文件不改动")
        self.context_label.configure(fg="#FFD3A6" if active else "#D6E2F3")
        suffix = " · 虚构演示样例" if active else " · 按清单查找与复制文件"
        self.root.title(f"{APP_TITLE} {APP_VERSION}{suffix}")

    def _apply_demo_inputs(self, source: str, text: str) -> None:
        self._loading_inputs = True
        try:
            self.source_var.set(str(Path(source).resolve()))
            self._replace_list(text)
            self.mode_var.set("full")
            self.recursive_var.set(True)
            self.case_var.set(False)
            self.include_var.set("")
            self.exclude_var.set("")
            self.search_var.set("")
            self.filter_var.set("全部状态")
        finally:
            self._loading_inputs = False
        self._invalidate_result()
        self._set_demo_active(True)
        self.status_var.set("已载入虚构演示资料，正在准备扫描。修改清单或源文件夹可退出演示。")

    def _load_demo(self) -> None:
        if self.busy_kind:
            return
        resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        demo_root = resource_root / "演示资料"
        demo_source = demo_root / "源文件"
        demo_list = demo_root / "清单.txt"
        try:
            if not demo_source.is_dir():
                raise FileNotFoundError("演示源文件夹不存在。")
            text = read_list_file(str(demo_list))
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法加载演示", f"请确认“演示资料”已随程序提供。\n\n{exc}", parent=self.root)
            return
        if self.source_var.get().strip() or self.list_text.get("1.0", "end-1c").strip():
            if not messagebox.askyesno("替换当前输入？", "加载演示会替换本窗口的清单、源文件夹及匹配选项，并清除人工选择。\n不会修改或删除任何原文件。是否继续？", parent=self.root):
                return
        self._apply_demo_inputs(str(demo_source), text)
        self._start_scan()

    def _start_scan(self) -> None:
        if self.busy_kind:
            return
        source = self.source_var.get().strip()
        text = self.list_text.get("1.0", "end-1c")
        if not source:
            messagebox.showinfo("还未选择源文件夹", "请先选择要查找文件的源文件夹。", parent=self.root)
            return
        if not text.strip():
            messagebox.showinfo("清单为空", "请粘贴一列文件名，或导入 TXT 清单。", parent=self.root)
            return
        if not Path(source).is_dir():
            messagebox.showerror("源文件夹不可用", "指定的源文件夹不存在或无法访问，请重新选择。", parent=self.root)
            return
        options = {"mode": self.mode_var.get(), "recursive": self.recursive_var.get(), "case_sensitive": self.case_var.get(), "include_extensions": self.include_var.get(), "exclude_extensions": self.exclude_var.get()}
        if self.filter_var.get() == "人工选择":
            self.filter_var.set("全部状态")
        self.cancel_event = threading.Event()
        cancel_event = self.cancel_event
        self.result_dirty = True
        self._set_busy("scan")
        self.status_var.set("正在扫描源文件夹…")
        self.result_note_var.set("正在扫描。取消或扫描不完整时，不允许复制文件。")

        def worker() -> None:
            try:
                from core import scan
                result = scan(source, text, **options, cancel_event=cancel_event, progress=lambda message: self.events.put(("progress", str(message))))
                self.events.put(("scan_done", result))
            except Exception as exc:
                self.events.put(("error", ("扫描未完成", str(exc))))

        threading.Thread(target=worker, name="list-pick-scan", daemon=True).start()

    def _render_rows(self, focus_row: dict | None = None) -> None:
        previous_selection = self.tree.selection()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.visible_rows.clear()
        if self.result is None:
            return
        rows = self.result.get("rows", [])
        counts = {code: 0 for code in STATUS_NAMES}
        for row in rows:
            counts[row.get("status", "uncertain")] = counts.get(row.get("status", "uncertain"), 0) + 1
        self.summary_vars["total"].set(str(len(rows)))
        for code in ("missing", "duplicate"):
            self.summary_vars[code].set(str(counts[code]))
        self.summary_vars["ambiguous"].set(str(counts["ambiguous"] + counts["multiple"]))
        planned, source_rows = self._copy_plan()
        self.summary_vars["unique"].set(str(source_rows))
        self.selection_count_var.set(f"计划 {len(planned)} 个文件 / {source_rows} 行")
        self.summary_vars["other"].set(str(counts["invalid"] + counts["uncertain"]))
        captions = [("total", "清单"), ("unique", "计划"), ("missing", "缺失"), ("ambiguous", "待选"), ("duplicate", "重复"), ("other", "其他")]
        self.summary_text_var.set("  ·  ".join(f"{caption} {self.summary_vars[key].get()}" for key, caption in captions) + "（行）")
        selected_filter = FILTERS.get(self.filter_var.get())
        keyword = self.search_var.get().strip().casefold()
        for index, row in enumerate(rows):
            code = row.get("status", "uncertain")
            if selected_filter and code != selected_filter:
                continue
            candidates = row.get("candidates", [])
            if keyword and not any(keyword in str(value).casefold() for value in [row.get("query", ""), *candidates]):
                continue
            reason = row.get("reason", "")
            if code == "selected":
                chosen = self._row_selected_paths(row)
                location = str(chosen[0]) if len(chosen) == 1 else f"已选择 {len(chosen)} 个文件；点击查看路径"
            else:
                location = str(candidates[0]) if code == "unique" and candidates else reason
            if not location and candidates:
                location = "选择此行查看全部候选路径"
            key = f"row-{index}"
            self.visible_rows[key] = row
            self.tree.insert("", "end", iid=key, values=(row.get("line", ""), row.get("query", ""), STATUS_NAMES.get(code, code), len(candidates), location), tags=(code,))
        self.display_count_var.set(f"显示 {len(self.visible_rows)} / {len(rows)} 行")
        if self.visible_rows:
            self.empty_label.place_forget()
        else:
            self.empty_label.configure(text="没有符合显示条件的行\n显示筛选不会改变复制计划" if rows else "清单没有可显示的行\n请检查输入后重新扫描")
            self.empty_label.place(relx=.5, rely=.55, anchor="center", relwidth=.9)
        target_key = None
        if focus_row is not None:
            target_key = next((key for key, row in self.visible_rows.items() if row is focus_row), None)
        elif previous_selection and previous_selection[0] in self.visible_rows:
            target_key = previous_selection[0]
        if target_key:
            self.tree.selection_set(target_key)
            self.tree.see(target_key)
            self._show_selection()
        else:
            self._set_detail("选中一行查看完整路径。多文件命中需明确选择；精确同名只能选一份。")
        self._refresh_actions()

    def _update_result_note(self) -> None:
        if self.result is None:
            return
        if self.result_dirty:
            self.result_note_var.set("输入已变更，人工选择已清除。下方为旧结果，请重新扫描后再导出。")
            return
        prefix = "虚构演示 · " if self.demo_active else ""
        prefix += MODE_LABELS.get(self.result.get("mode"), "匹配") + " · "
        scanned = self.result.get("scanned", 0)
        filtered = self.result.get("filtered_out", 0)
        if not self.result.get("complete", False):
            self.result_note_var.set(f"{prefix}扫描不完整：检查 {scanned} 个，后缀排除 {filtered} 个。请查看详情；当前仅可导出报告。")
            return
        planned, source_rows = self._copy_plan()
        self.result_note_var.set(f"{prefix}检查 {scanned} 个，后缀排除 {filtered} 个；计划 {len(planned)} 个文件 / {source_rows} 行。")

    def _current_row(self) -> dict | None:
        selected = self.tree.selection()
        return self.visible_rows.get(selected[0]) if selected else None

    def _show_selection(self, _event=None) -> None:
        row = self._current_row()
        if not row:
            self._refresh_actions()
            return
        lines = [f"第 {row.get('line', '—')} 行：{row.get('query', '')}  |  {STATUS_NAMES.get(row.get('status'), row.get('status', ''))}"]
        if row.get("reason"):
            lines.append(str(row["reason"]))
        chosen = self._row_selected_paths(row) if row.get("status") == "selected" else []
        if chosen:
            lines.append(f"本次将复制已选择的 {len(chosen)} 个文件。")
        candidates = row.get("candidates", [])
        if candidates:
            lines.extend(f"{number}. {path}" + ("  （已选择）" if path in chosen else "") for number, path in enumerate(candidates, 1))
        else:
            lines.append("没有可显示的候选路径。")
        self._set_detail("\n".join(lines))
        self._refresh_actions()

    def _can_edit_candidate(self, row: dict | None) -> bool:
        return bool(
            not self.busy_kind and not self.result_dirty and self.result
            and self.result.get("complete", False) and row
            and any(item is row for item in self.result.get("rows", []))
            and row.get("status") in ("ambiguous", "multiple", "selected")
        )

    def _double_click_candidate(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self._choose_candidate()

    def _choose_candidate(self) -> None:
        row = self._current_row()
        if not self._can_edit_candidate(row):
            return
        candidates = list(row.get("candidates", []))
        if len(candidates) < 2:
            return
        multiple_mode = self.result.get("mode") in ("regex", "extension")
        dialog = tk.Toplevel(self.root)
        self.candidate_dialog = dialog
        dialog.title(("选择要复制的文件" if multiple_mode else "选择一份候选文件") + " · " + str(row.get("query", ""))[:80])
        dialog.transient(self.root)
        minimum_width = max(760, round(365 * float(self.root.tk.call("tk", "scaling"))))
        dialog.geometry(f"{max(900, minimum_width)}x520")
        dialog.minsize(minimum_width, 440)
        dialog.configure(bg=PAPER)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(3, weight=1)
        heading_text = "勾选需要的文件，再确认加入复制计划" if multiple_mode else "同名文件只选一份，请核对所在文件夹"
        ttk.Label(dialog, text=heading_text, style="Step.TLabel").grid(row=0, column=0, sticky="w", padx=18, pady=(14, 9))
        search_row = ttk.Frame(dialog, style="Card.TFrame")
        search_row.grid(row=1, column=0, sticky="ew", padx=18)
        search_row.columnconfigure(1, weight=1)
        ttk.Label(search_row, text="搜索候选", style="Muted.TLabel").grid(row=0, column=0, padx=(0, 9))
        search_var = tk.StringVar()
        search_entry = ttk.Entry(search_row, textvariable=search_var)
        search_entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(search_row, text="清除搜索", command=lambda: search_var.set(""), style="Compact.TButton").grid(row=0, column=2, padx=(8, 0))
        count_var = tk.StringVar()
        count_label = ttk.Label(dialog, textvariable=count_var, style="Muted.TLabel", wraplength=850)
        count_label.grid(row=2, column=0, sticky="ew", padx=18, pady=(7, 7))
        count_label.bind("<Configure>", lambda event: count_label.configure(wraplength=max(200, event.width - 4)))
        list_frame = ttk.Frame(dialog, style="Card.TFrame")
        list_frame.grid(row=3, column=0, sticky="nsew", padx=18)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        scale = float(self.root.tk.call("tk", "scaling"))
        icon_size = max(16, round(16 * scale / (4 / 3)))
        ttk.Style(dialog).configure("Candidate.Treeview", rowheight=max(29, round(22 * scale)), font=("Microsoft YaHei UI", 10))
        candidate_tree = ttk.Treeview(list_frame, columns=("path",), show="tree headings", selectmode="browse", style="Candidate.Treeview")
        candidate_tree.heading("#0", text="勾选")
        candidate_tree.column("#0", width=max(58, icon_size + 24), minwidth=58, stretch=False)
        candidate_tree.heading("path", text="相对源文件夹的路径（空格键可切换勾选）", anchor="w")
        candidate_tree.column("path", width=700, minwidth=300, anchor="w")
        candidate_tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(list_frame, orient="vertical", command=candidate_tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(list_frame, orient="horizontal", command=candidate_tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        candidate_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)

        def checkbox_image(checked: bool) -> tk.PhotoImage:
            image = tk.PhotoImage(master=dialog, width=icon_size, height=icon_size)
            image.put(BLUE if checked else "#8B9AAF", to=(1, 1, icon_size - 1, icon_size - 1))
            if not checked:
                image.put(PAPER, to=(2, 2, icon_size - 2, icon_size - 2))
            else:
                points = [(0.25, 0.50), (0.43, 0.69), (0.76, 0.30)]
                for start, end in zip(points, points[1:]):
                    steps = icon_size
                    for step in range(steps + 1):
                        ratio = step / steps
                        x = round((start[0] + (end[0] - start[0]) * ratio) * icon_size)
                        y = round((start[1] + (end[1] - start[1]) * ratio) * icon_size)
                        image.put(PAPER, to=(x, y, min(icon_size, x + 2), min(icon_size, y + 2)))
            return image

        check_images = (checkbox_image(False), checkbox_image(True))
        dialog.candidate_check_images = check_images
        relative_paths = []
        for candidate in candidates:
            try:
                display_path = str(Path(candidate).relative_to(Path(self.result["source"])))
            except ValueError:
                display_path = str(candidate)
            relative_paths.append(display_path)
        originally_selected = row.get("status") == "selected"
        selected_paths = set(self._row_selected_paths(row)) if originally_selected else set()
        visible_indexes: list[int] = []
        preview_var = tk.StringVar(value="点击路径可查看完整位置。搜索只筛列表，不会改变已有勾选。")
        preview = tk.Label(dialog, textvariable=preview_var, bg=PAPER, fg=MUTED, font=("Microsoft YaHei UI", 9), height=2, wraplength=850, anchor="w", justify="left")
        preview.grid(row=4, column=0, sticky="ew", padx=18, pady=(7, 5))
        preview.bind("<Configure>", lambda event: preview.configure(wraplength=max(200, event.width - 4)))
        actions = ttk.Frame(dialog, style="Card.TFrame")
        actions.grid(row=5, column=0, sticky="ew", padx=18, pady=(4, 14))
        use_button = ttk.Button(actions, text="确认使用所选" if multiple_mode else "使用此文件", style="Primary.TButton", state="disabled")
        use_button.pack(side="right")
        ttk.Button(actions, text="取消，不更改", command=dialog.destroy).pack(side="right", padx=(0, 8))

        def update_counts() -> None:
            visible_selected = sum(candidates[index] in selected_paths for index in visible_indexes)
            count_var.set(f"显示 {len(visible_indexes)} / {len(candidates)} 个 · 全部已选 {len(selected_paths)} 个（隐藏已选 {len(selected_paths) - visible_selected} 个）；搜索不改勾选。")
            use_button.configure(state="normal" if selected_paths or originally_selected else "disabled")
            use_button.configure(text="确认取消本行选择" if not selected_paths and originally_selected else ("确认使用所选" if multiple_mode else "使用此文件"))

        def show_path(_event=None) -> None:
            focus = candidate_tree.focus()
            if focus and candidate_tree.exists(focus):
                index = int(focus.split("-")[-1])
                preview_var.set(("已勾选：" if candidates[index] in selected_paths else "未勾选：") + candidates[index])

        def render_candidates(*_args) -> None:
            previous_focus = candidate_tree.focus()
            candidate_tree.delete(*candidate_tree.get_children())
            keyword = search_var.get().strip().casefold()
            visible_indexes[:] = [index for index, path in enumerate(candidates) if not keyword or keyword in path.casefold()]
            for index in visible_indexes:
                checked = candidates[index] in selected_paths
                candidate_tree.insert("", "end", iid=f"candidate-{index}", text="", image=check_images[int(checked)], values=(f"{index + 1}. {relative_paths[index]}",))
            if previous_focus and candidate_tree.exists(previous_focus):
                candidate_tree.focus(previous_focus)
                candidate_tree.selection_set(previous_focus)
            update_counts()
            if not visible_indexes:
                preview_var.set("没有符合搜索的候选。清除搜索可重新查看全部文件；已勾选项仍然保留。")

        def toggle_item(item: str) -> None:
            if not item or not candidate_tree.exists(item):
                return
            index = int(item.split("-")[-1])
            path = candidates[index]
            was_selected = path in selected_paths
            if not multiple_mode:
                selected_paths.clear()
            if was_selected:
                selected_paths.discard(path)
            else:
                selected_paths.add(path)
            for visible_index in visible_indexes:
                candidate_tree.item(f"candidate-{visible_index}", image=check_images[int(candidates[visible_index] in selected_paths)])
            candidate_tree.focus(item)
            candidate_tree.selection_set(item)
            update_counts()
            show_path()

        def click_checkbox(event) -> str | None:
            item = candidate_tree.identify_row(event.y)
            if item and candidate_tree.identify_column(event.x) == "#0":
                toggle_item(item)
                candidate_tree.focus_set()
                return "break"
            return None

        def toggle_keyboard(_event=None) -> str:
            toggle_item(candidate_tree.focus())
            return "break"

        def accept() -> None:
            chosen = [path for path in candidates if path in selected_paths]
            if chosen and self._apply_candidates(row, chosen):
                dialog.destroy()
            elif not chosen and originally_selected and self._clear_row_candidate(row):
                dialog.destroy()

        if multiple_mode:
            def choose_visible() -> None:
                selected_paths.update(candidates[index] for index in visible_indexes)
                render_candidates()

            def clear_visible() -> None:
                selected_paths.difference_update(candidates[index] for index in visible_indexes)
                render_candidates()

            ttk.Button(actions, text="勾选当前结果", command=choose_visible, style="Compact.TButton").pack(side="left")
            ttk.Button(actions, text="取消当前结果", command=clear_visible, style="Compact.TButton").pack(side="left", padx=(6, 0))
        use_button.configure(command=accept)
        candidate_tree.bind("<Button-1>", click_checkbox)
        candidate_tree.bind("<<TreeviewSelect>>", show_path)
        candidate_tree.bind("<space>", toggle_keyboard)
        search_var.trace_add("write", render_candidates)
        dialog.candidate_search_var = search_var
        dialog.candidate_tree = candidate_tree
        dialog.candidate_count_var = count_var
        dialog.candidate_selected_paths = selected_paths
        render_candidates()
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.bind("<Control-f>", lambda _event: search_entry.focus_set())
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dialog.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()
        search_entry.focus_set()
        try:
            self.root.wait_window(dialog)
        finally:
            self.candidate_dialog = None

    def _apply_candidate(self, row: dict, candidate: str) -> bool:
        return self._apply_candidates(row, [candidate])

    def _apply_candidates(self, row: dict, candidates: list[str]) -> bool:
        if not self._can_edit_candidate(row) or not candidates or not all(isinstance(path, str) and path in row.get("candidates", []) for path in candidates):
            return False
        chosen = list(dict.fromkeys(candidates))
        multiple_mode = self.result.get("mode") in ("regex", "extension")
        if not multiple_mode and len(chosen) != 1:
            return False
        row["status"] = "selected"
        row.pop("selected_candidate", None)
        row.pop("selected_candidates", None)
        if multiple_mode:
            row["selected_candidates"] = chosen
            row["reason"] = f"用户已手动选择 {len(chosen)} 个文件；其余候选未复制。"
        else:
            row["selected_candidate"] = chosen[0]
            row["reason"] = "用户已手动选择一份；其余候选未复制。"
        self._render_rows(focus_row=row)
        self._focus_next_visible(row)
        self._update_result_note()
        self.status_var.set(f"本行已人工选择 {len(chosen)} 个文件。其余候选不会复制，可随时取消选择。")
        return True

    def _clear_candidate(self) -> None:
        row = self._current_row()
        self._clear_row_candidate(row)

    def _clear_row_candidate(self, row: dict | None) -> bool:
        if not self._can_edit_candidate(row) or row.get("status") != "selected":
            return False
        self._restore_unselected(row)
        self._render_rows(focus_row=row)
        self._focus_next_visible(row)
        self._update_result_note()
        self.status_var.set("已取消人工选择。这一项不会复制，除非重新选择候选文件。")
        return True

    def _focus_next_visible(self, previous_row: dict) -> None:
        if self.tree.selection() or not self.visible_rows:
            return
        previous_line = previous_row.get("line", 0)
        following = next((key for key, row in self.visible_rows.items() if row.get("line", 0) > previous_line), next(iter(self.visible_rows)))
        self.tree.selection_set(following)
        self.tree.focus(following)
        self.tree.see(following)
        self._show_selection()

    def _start_export(self, copy_files: bool) -> None:
        if self.busy_kind or self.result is None or self.result_dirty:
            return
        if copy_files and not self.result.get("complete", False):
            messagebox.showinfo("扫描不完整", "请完成一次完整扫描后再复制。当前结果可以仅导出报告。", parent=self.root)
            return
        parent = self.parent_var.get().strip()
        if not parent:
            self._choose_parent()
            parent = self.parent_var.get().strip()
            if not parent:
                return
        try:
            parent_path = Path(parent).resolve()
            source_path = Path(self.result["source"]).resolve()
            if not parent_path.is_dir():
                raise ValueError("输出父文件夹不存在或无法访问，请重新选择。")
            if parent_path == source_path or source_path in parent_path.parents:
                raise ValueError("输出位置不能是源文件夹或其子文件夹。请选择另外一个文件夹。")
        except (OSError, ValueError) as exc:
            messagebox.showerror("输出位置不可用", str(exc), parent=self.root)
            return
        if copy_files:
            planned, source_rows = self._copy_plan()
            if not planned:
                messagebox.showinfo("复制计划为空", "请先完成扫描，并为多文件命中明确选择文件。", parent=self.root)
                return
            if not messagebox.askyesno("确认复制全部计划", f"将复制全部计划中的 {len(planned)} 个文件（去重后），来自 {source_rows} 条清单项或规则。\n\n当前结果搜索和状态筛选只影响显示，不减少复制范围。\n输出父文件夹：{parent_path}\n\n原文件保持不变。是否继续？", parent=self.root):
                return
        self.cancel_event = threading.Event()
        cancel_event = self.cancel_event
        result = self.result
        self._set_busy("export")
        self.status_var.set("正在复制文件并导出核对报告…" if copy_files else "正在导出核对报告…")

        def worker() -> None:
            try:
                from core import export_result
                exported = export_result(result, str(parent_path), copy_files=copy_files, cancel_event=cancel_event, progress=lambda message: self.events.put(("progress", str(message))))
                self.events.put(("export_done", (exported, copy_files)))
            except Exception as exc:
                self.events.put(("error", ("输出未完成", str(exc))))

        threading.Thread(target=worker, name="list-pick-export", daemon=True).start()

    def _cancel(self) -> None:
        if self.cancel_event:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status_var.set("正在取消，请等待当前文件处理结束并保存核对结果…")

    def _on_close(self) -> None:
        if not self.busy_kind:
            self.root.destroy()
            return
        if messagebox.askyesno("任务仍在进行", "是否取消当前任务并退出？\n将等待当前文件处理结束和报告保存，原文件不会被改动。", parent=self.root):
            self.closing = True
            self._cancel()

    def _open_output(self) -> None:
        if self.last_output:
            try:
                os.startfile(self.last_output)
            except OSError as exc:
                messagebox.showerror("无法打开输出文件夹", str(exc), parent=self.root)

    def _poll(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "progress":
                    if not (self.cancel_event and self.cancel_event.is_set()):
                        self.status_var.set(payload)
                elif event == "scan_done":
                    self.result = payload
                    self.result_dirty = False
                    self._set_busy(None)
                    self._render_rows()
                    warnings = payload.get("warnings", [])
                    self._update_result_note()
                    if payload.get("complete", False):
                        prefix = "虚构演示扫描完成。" if self.demo_active else "扫描完成。"
                        self.status_var.set(prefix + "多候选可人工选择；选择输出位置后可复制或仅导出报告。")
                    else:
                        self.status_var.set("扫描未完整结束。请打开“详情 / 统计”查看原因，修正后重新扫描。")
                    if warnings:
                        self._set_detail("扫描提示：\n" + "\n".join(f"{i}. {warning}" for i, warning in enumerate(warnings, 1)))
                    self._refresh_actions()
                elif event == "export_done":
                    exported, copy_files = payload
                    self.last_output = str(exported.get("output_dir", "")) or None
                    self._set_busy(None)
                    copied = exported.get("copied", 0)
                    failed = exported.get("failed", 0)
                    cancelled = exported.get("cancelled", False)
                    report_error = exported.get("report_error", "")
                    if report_error:
                        summary = f"报告生成失败；已复制 {copied} 个，复制失败 {failed} 个。"
                    elif cancelled:
                        summary = f"任务已取消；已复制 {copied} 个，失败 {failed} 个。请以核对报告为准。"
                    elif copy_files:
                        summary = f"输出完成：复制 {copied} 个，失败 {failed} 个。核对报告已生成。"
                    else:
                        summary = "核对报告已生成，没有复制源文件。"
                    self.status_var.set(summary)
                    details = summary + "\n输出文件夹：" + str(exported.get("output_dir", ""))
                    if report_error:
                        details += "\n报告失败原因：" + str(report_error)
                    else:
                        details += "\n核对报告：" + str(exported.get("report_path", ""))
                    self._set_detail(details)
                    if report_error and not self.closing:
                        messagebox.showerror("报告未生成", details, parent=self.root)
                    elif failed and not self.closing:
                        messagebox.showwarning("部分文件未复制", summary + "\n请打开核对报告查看失败原因。", parent=self.root)
                elif event == "error":
                    title, detail = payload
                    self._set_busy(None)
                    if self.result_dirty:
                        self.result_note_var.set("本次扫描未完成。上一次结果不可继续导出，请重新扫描。")
                    self.status_var.set(f"{title}：{detail}")
                    if not self.closing:
                        messagebox.showerror(title, detail, parent=self.root)
                if self.closing and not self.busy_kind:
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(80, self._poll)


def main() -> int:
    parser = argparse.ArgumentParser(description="清单取件：按文件名、正则或后缀查找文件，确认后复制并生成核对报告。")
    parser.add_argument("--demo", metavar="SOURCE", help="载入指定源文件夹并自动扫描，用于演示")
    parser.add_argument("--list", dest="list_path", metavar="TXT", help="演示使用的文本清单")
    parser.add_argument("--smoke-test", action="store_true", help="验证窗口、打包资源、匹配与虚构文件复制后退出")
    args = parser.parse_args()
    root = tk.Tk()
    app = ListPickApp(root)
    if args.smoke_test:
        try:
            root.update_idletasks()
            root.update()
            from smoke_check import run
            run(Path(getattr(sys, "_MEIPASS", Path(__file__).parent)))
            return 0
        except Exception as exc:
            if sys.stderr is not None:
                print(f"Smoke test failed: {exc}", file=sys.stderr)
            return 1
        finally:
            root.destroy()
    if args.demo:
        demo_text = ""
        if args.list_path:
            try:
                demo_text = read_list_file(args.list_path)
            except (OSError, ValueError) as exc:
                messagebox.showerror("演示清单无法读取", str(exc), parent=root)
                root.destroy()
                return 1
        app._apply_demo_inputs(str(Path(args.demo).resolve()), demo_text)
        root.after(350, app._start_scan)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
