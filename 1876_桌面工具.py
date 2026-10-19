"""本地合成测试数据生成器的 Tkinter 界面。"""
from pathlib import Path
from datetime import datetime
import importlib.util
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("synthetic1876", BASE / "1876_数据生成.py")
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Repeatable Synthetic Data | CSV + SQLite")
        self.geometry("980x590")
        self.minsize(830, 540)
        self.configure(bg="#f3f6fa")
        self.events = queue.Queue()
        self.report = None
        self.result = None
        self.config_path = tk.StringVar(value=str(BASE / "1876_示例配置.json"))
        self.output_path = tk.StringVar(value=str(BASE / ("1876_运行结果_" + datetime.now().strftime("%Y%m%d_%H%M%S"))))
        self.status = tk.StringVar(value="Choose a JSON configuration and a NEW output folder.")
        self.values = {key: tk.StringVar(value="—") for key in ("rows", "unique_ids", "sqlite_rows", "verified_matches")}
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f6fa")
        style.configure("TLabel", background="#f3f6fa", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 23, "bold"), foreground="#172d45")
        style.configure("Value.TLabel", font=("Segoe UI", 28, "bold"), foreground="#087d79")
        style.configure("TButton", padding=8, font=("Segoe UI", 10))
        frame = ttk.Frame(self, padding=28)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Repeatable synthetic data", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Independent portfolio project · Local generation · No live database connection").pack(anchor="w", pady=(6, 22))
        fields = ttk.Frame(frame)
        fields.pack(fill="x")
        fields.columnconfigure(1, weight=1)
        for row, label, variable in [(0, "JSON configuration", self.config_path), (1, "New output folder", self.output_path)]:
            ttk.Label(fields, text=label).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=8)
            ttk.Entry(fields, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=8)
        ttk.Button(fields, text="Browse", command=self.choose_config).grid(row=0, column=2, padx=(10, 0))
        ttk.Button(fields, text="Choose parent", command=self.choose_output).grid(row=1, column=2, padx=(10, 0))
        cards = ttk.Frame(frame)
        cards.pack(fill="x", pady=(28, 20))
        for col, (key, label) in enumerate([( "rows", "DATA ROWS"), ("unique_ids", "UNIQUE UUIDs"), ("sqlite_rows", "SQLITE ROWS"), ("verified_matches", "MATCHED ROWS")]):
            cards.columnconfigure(col, weight=1)
            card = ttk.Frame(cards, padding=12)
            card.grid(row=0, column=col, sticky="nsew")
            ttk.Label(card, text=label, foreground="#61798c").pack(anchor="w")
            ttk.Label(card, textvariable=self.values[key], style="Value.TLabel").pack(anchor="w", pady=(8, 0))
        ttk.Label(frame, text="One configuration. Stable identities. Two verified outputs.", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Sequence · Choice · Integer · Decimal · Date · Boolean\nUUIDv5 identifiers are deterministic and are intended for test data, not secret tokens.", wraplength=870).pack(anchor="w", pady=(10, 22))
        controls = ttk.Frame(frame)
        controls.pack(fill="x")
        self.run_button = ttk.Button(controls, text="Generate and verify", command=self.start)
        self.run_button.pack(side="left")
        self.open_button = ttk.Button(controls, text="Open offline report", command=self.open_report, state="disabled")
        self.open_button.pack(side="left", padx=12)
        ttk.Label(frame, textvariable=self.status, wraplength=870).pack(anchor="w", pady=(18, 4))
        ttk.Label(frame, text="Existing output files and the configuration are preserved.", foreground="#61798c").pack(anchor="w")
        self.after(80, self.poll)

    def choose_config(self):
        path = filedialog.askopenfilename(title="Choose JSON configuration", filetypes=[("JSON configuration", "*.json")])
        if path:
            self.config_path.set(path)

    def choose_output(self):
        folder = filedialog.askdirectory(title="Choose parent for a NEW output folder")
        if folder:
            self.output_path.set(str(Path(folder) / ("1876_运行结果_" + datetime.now().strftime("%Y%m%d_%H%M%S"))))

    def start(self):
        self.run_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.report = None
        self.result = None
        self.status.set("Validating configuration, generating data and verifying SQLite rows…")
        args = (Path(self.config_path.get()), Path(self.output_path.get()))
        self.active_output = args[1]
        def worker():
            try:
                self.events.put((True, engine.generate(*args)))
            except Exception as exc:
                self.events.put((False, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            ok, value = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.run_button.configure(state="normal")
            if ok:
                self.result = value
                for key, variable in self.values.items():
                    variable.set(f"{value[key]:,}")
                self.report = self.active_output / "1876_运行报告.html"
                self.open_button.configure(state="normal")
                self.status.set("Complete. CSV, SQLite, JSON audit and the offline report are ready.")
            else:
                self.status.set("Please correct the input: " + value)
                messagebox.showerror("Generation could not complete", value, parent=self)
        self.after(80, self.poll)

    def open_report(self):
        if self.report and self.report.is_file():
            webbrowser.open(self.report.resolve().as_uri())


if __name__ == "__main__":
    App().mainloop()
