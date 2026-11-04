"""A simple receipt designer for 80 mm paper, with shared print rendering."""

from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import textwrap
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from tkinter import font as tkfont

from receipt_printing import FIELD_LABELS, FONTS, ReceiptDesign, load_logo, open_receipts, receipt_values


class ReceiptDesigner:
    def __init__(self, parent, design: ReceiptDesign, sample, currency: str, output_dir: Path, on_save):
        self.original = ReceiptDesign.from_json(design.to_json())
        self.sample, self.currency, self.output_dir, self.on_save = sample, currency, output_dir, on_save
        self.order = list(design.field_order)
        self.labels = dict(design.labels)
        self.logo_data = design.logo_data
        self.loading_label = False
        self.window = tk.Toplevel(parent)
        self.window.title("Receipt designer — 80 mm thermal paper")
        self.window.geometry("1020x760")
        self.window.minsize(920, 650)
        self.window.transient(parent)
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(1, weight=1)
        family = tkfont.nametofont("TkDefaultFont", parent).actual("family")
        ttk.Label(self.window, text="Design your 80 mm receipt", font=(family, 16, "bold")).grid(
            row=0, column=0, sticky="w", padx=18, pady=(14, 10))
        content = ttk.Frame(self.window, padding=(18, 0))
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)
        notebook = ttk.Notebook(content)
        notebook.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        general = ttk.Frame(notebook, padding=12)
        general.columnconfigure(1, weight=1)
        field_tab = ttk.Frame(notebook, padding=12)
        field_tab.columnconfigure(0, weight=1)
        field_tab.rowconfigure(1, weight=1)
        notebook.add(general, text="Business & layout")
        notebook.add(field_tab, text="Field order & labels")
        self.variables = {}
        self.multiline = {}
        controls = (("Business name", "merchant_name"), ("Business address", "merchant_address"),
                    ("Business phone", "merchant_phone"), ("Receipt title", "title"),
                    ("Footer message", "footer"), ("Font", "font_family"), ("Font size (8-14 pt)", "font_size"),
                    ("Margins (1-6 mm)", "margin_mm"), ("Header / footer align", "header_alignment"),
                    ("Separator", "separator"))
        choices = {"font_family": FONTS, "header_alignment": ("left", "center", "right"),
                   "separator": ("dashed", "solid", "none")}
        for row, (caption, key) in enumerate(controls):
            ttk.Label(general, text=caption).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=6)
            if key in ("merchant_address", "footer"):
                widget = tk.Text(general, height=3, width=24, wrap="word", font="TkDefaultFont", relief="solid", borderwidth=1)
                widget.insert("1.0", getattr(design, key))
                widget.edit_modified(False)
                widget.bind("<<Modified>>", self.text_changed)
                self.multiline[key] = widget
            else:
                variable = tk.StringVar(value=str(getattr(design, key)))
                self.variables[key] = variable
                if key in choices:
                    widget = ttk.Combobox(general, textvariable=variable, values=choices[key], state="readonly", width=20)
                elif key in ("font_size", "margin_mm"):
                    low, high = (8, 14) if key == "font_size" else (1, 6)
                    widget = ttk.Spinbox(general, textvariable=variable, from_=low, to=high, width=10)
                else:
                    widget = ttk.Entry(general, textvariable=variable, width=24)
                variable.trace_add("write", lambda *_: self.update_preview())
            widget.grid(row=row, column=1, sticky="ew", pady=6)
        ttk.Label(general, text="Logo").grid(row=10, column=0, sticky="w", pady=6)
        logo_controls = ttk.Frame(general)
        logo_controls.grid(row=10, column=1, sticky="ew")
        ttk.Button(logo_controls, text="Choose image...", command=self.choose_logo).pack(side="left")
        ttk.Button(logo_controls, text="Remove", command=self.remove_logo).pack(side="left", padx=(6, 0))
        self.logo_status = tk.StringVar(value="Logo included" if self.logo_data else "No logo")
        ttk.Label(general, textvariable=self.logo_status).grid(row=11, column=1, sticky="w")
        ttk.Label(general, text="Paper: 80 mm · content wraps automatically", wraplength=380).grid(
            row=12, column=0, columnspan=2, sticky="w", pady=(12, 0))

        ttk.Label(field_tab, text="Every receipt field is included. Move fields or rename their labels.",
                  wraplength=370).grid(row=0, sticky="w", pady=(0, 10))
        self.field_list = tk.Listbox(field_tab, height=12, exportselection=False, font="TkDefaultFont")
        self.field_list.grid(row=1, column=0, sticky="nsew")
        self.field_list.bind("<<ListboxSelect>>", self.choose_field)
        movement = ttk.Frame(field_tab)
        movement.grid(row=2, column=0, sticky="ew", pady=10)
        ttk.Button(movement, text="Move up", command=lambda: self.move(-1)).pack(side="left")
        ttk.Button(movement, text="Move down", command=lambda: self.move(1)).pack(side="left", padx=6)
        ttk.Label(field_tab, text="Selected field label").grid(row=3, sticky="w", pady=(5, 5))
        self.field_label = tk.StringVar()
        ttk.Entry(field_tab, textvariable=self.field_label).grid(row=4, sticky="ew")
        self.field_label.trace_add("write", self.rename_field)
        self.rebuild_fields(0)

        preview_frame = ttk.Frame(content)
        preview_frame.grid(row=0, column=1, sticky="nsew")
        preview_frame.rowconfigure(1, weight=1)
        preview_frame.columnconfigure(0, weight=1)
        ttk.Label(preview_frame, text="Layout preview · example receipt").grid(sticky="w", pady=(0, 8))
        self.preview = scrolledtext.ScrolledText(preview_frame, width=40, wrap="word", background="white",
                                              font=("Courier New", 10), padx=14, pady=14, state="disabled")
        self.preview.grid(row=1, sticky="nsew")
        ttk.Label(preview_frame, text="Open Print preview to see the exact layout, logo and paper size.",
                  wraplength=400).grid(row=2, sticky="w", pady=8)
        ttk.Button(preview_frame, text="Open print preview", command=self.browser_preview).grid(row=3, sticky="ew")
        actions = ttk.Frame(self.window, padding=18)
        actions.grid(row=2, sticky="ew")
        actions.columnconfigure(1, weight=1)
        ttk.Button(actions, text="Reset design", command=self.reset).grid(row=0, column=0)
        ttk.Button(actions, text="Cancel", command=self.window.destroy).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(actions, text="Save design", style="Save.TButton", command=self.save).grid(row=0, column=3)
        self.update_preview()

    def design(self):
        values = {key: var.get() for key, var in self.variables.items()}
        values.update({key: widget.get("1.0", "end-1c") for key, widget in self.multiline.items()})
        try:
            values["font_size"] = int(values["font_size"])
            values["margin_mm"] = int(values["margin_mm"])
        except ValueError:
            raise ValueError("Enter a whole number for the font size and margins.") from None
        result = replace(self.original, **values, field_order=list(self.order), labels=dict(self.labels),
                         logo_data=self.logo_data)
        result.validate()
        return result

    def text_changed(self, event):
        if event.widget.edit_modified():
            event.widget.edit_modified(False)
            self.update_preview()

    def update_preview(self):
        if not hasattr(self, "preview"):
            return
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        try:
            design = self.design()
            values = receipt_values(self.sample, self.currency)
            preview_family = design.font_family if sys.platform == "win32" else (
                "Courier" if design.font_family == "Consolas" else "Helvetica")
            self.preview.tag_configure("header", justify=design.header_alignment,
                                       font=(preview_family, design.font_size + 2, "bold"))
            self.preview.tag_configure("footer", justify=design.header_alignment)
            self.preview.configure(font=(preview_family, design.font_size))
            header = "\n".join(value for value in ("[ Business logo ]" if design.logo_data else "",
                                                 design.merchant_name, design.merchant_address,
                                                 design.merchant_phone, design.title) if value)
            self.preview.insert("end", header + "\n\n", "header")
            if design.separator != "none":
                self.preview.insert("end", ("-" if design.separator == "dashed" else "_") * 34 + "\n")
            for key in design.field_order:
                text = f"{design.labels[key]}: {values[key]}"
                self.preview.insert("end", textwrap.fill(text, width=38, replace_whitespace=False) + "\n\n")
            self.preview.insert("end", design.footer, "footer")
        except ValueError as error:
            self.preview.insert("end", str(error))
        self.preview.configure(state="disabled")

    def rebuild_fields(self, index):
        self.field_list.delete(0, "end")
        for key in self.order:
            self.field_list.insert("end", self.labels[key] or FIELD_LABELS[key])
        self.field_list.selection_set(index)
        self.field_list.see(index)
        self.choose_field()

    def choose_field(self, _event=None):
        selected = self.field_list.curselection()
        if selected:
            self.loading_label = True
            self.field_label.set(self.labels[self.order[selected[0]]])
            self.loading_label = False

    def rename_field(self, *_):
        if self.loading_label:
            return
        selected = self.field_list.curselection()
        if selected:
            index = selected[0]
            self.labels[self.order[index]] = self.field_label.get()
            self.field_list.delete(index)
            self.field_list.insert(index, self.field_label.get() or "(Enter a label)")
            self.field_list.selection_set(index)
            self.update_preview()

    def move(self, direction):
        selected = self.field_list.curselection()
        if not selected:
            return
        index, target = selected[0], selected[0] + direction
        if 0 <= target < len(self.order):
            self.order[index], self.order[target] = self.order[target], self.order[index]
            self.rebuild_fields(target)
            self.update_preview()

    def choose_logo(self):
        path = filedialog.askopenfilename(parent=self.window, title="Choose a receipt logo",
                                          filetypes=[("Images", "*.png *.jpg *.jpeg *.gif")])
        if not path:
            return
        try:
            self.logo_data = load_logo(Path(path))
        except (ValueError, OSError) as error:
            messagebox.showerror("Could not load logo", str(error), parent=self.window)
            return
        self.logo_status.set("Logo included in the saved design")
        self.update_preview()

    def remove_logo(self):
        self.logo_data = ""
        self.logo_status.set("No logo")
        self.update_preview()

    def browser_preview(self):
        try:
            open_receipts([self.sample], self.currency, self.design(), self.output_dir, auto_print=False)
        except (ValueError, OSError) as error:
            messagebox.showerror("Could not open preview", str(error), parent=self.window)

    def reset(self):
        if not messagebox.askyesno("Reset design", "Reset this draft to the default 80 mm layout?", parent=self.window):
            return
        defaults = ReceiptDesign()
        for key, variable in self.variables.items():
            variable.set(str(getattr(defaults, key)))
        for key, widget in self.multiline.items():
            widget.delete("1.0", "end")
            widget.insert("1.0", getattr(defaults, key))
        self.order, self.labels = list(defaults.field_order), dict(defaults.labels)
        self.remove_logo()
        self.rebuild_fields(0)
        self.update_preview()

    def save(self):
        try:
            design = self.design()
            self.on_save(design)
        except (ValueError, OSError, sqlite3.Error) as error:
            messagebox.showerror("Design not saved", str(error), parent=self.window)
            return
        self.window.destroy()
