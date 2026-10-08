"""Tkinter desktop UI. All IFC logic lives in detection / repair / verify."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__, theme
from .detection import analyze
from .footprint import cached_plan_data
from .model import MODE_BRANCH, MODE_STOREY, DetectionReport, RepairOptions
from .plan_view import PlanView
from .repair import OutputSafetyError, check_output_path, default_output_path, repair_file
from .reporting import format_detection
from .theme import BG, BORDER, FONTS, MUTED, SURFACE, TEXT

AUTO = "Auto-detect blocks"
KPIS = [("buildings", "Buildings"), ("blocks", "Blocks"), ("linked", "Linked branches"),
        ("moves", "Elements to move"), ("warnings", "Warnings")]


class App(ttk.Frame):
    def __init__(self, root: tk.Tk):
        super().__init__(root)
        self.root = root
        self.report: DetectionReport | None = None
        self.include_ids: set[int] | None = None
        self.jobs: queue.Queue = queue.Queue()
        self.busy = False
        self.tab_frames: dict[str, ttk.Frame] = {}
        self.kpi_values: dict[str, tk.Label] = {}
        self.kpi_notes: dict[str, tk.Label] = {}

        self.path_var = tk.StringVar()
        self.master_var = tk.StringVar(value=AUTO)
        self.mode_var = tk.StringVar(value=MODE_STOREY)
        self.tol_var = tk.StringVar(value="0.3")
        self.remove_var = tk.BooleanVar(value=True)
        self.rebase_var = tk.BooleanVar(value=True)
        self.show_empty_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Open an IFC file to begin.")
        self.file_title = tk.StringVar(value="No file loaded")
        self.file_folder = tk.StringVar(value="Choose a Revit IFC export to analyse.")
        self.file_meta = tk.StringVar(value="")

        self._build()
        self.pack(fill="both", expand=True)
        self.root.after(100, self._poll)

    # ------------------------------------------------------------ layout

    def _build(self) -> None:
        self._build_header()
        self._build_statusbar()
        body = ttk.Frame(self, padding=(16, 16, 16, 12))
        body.pack(fill="both", expand=True)
        self._build_sidebar(body)
        self._build_main(body)

    def _build_header(self) -> None:
        header = tk.Frame(self, background=SURFACE)
        header.pack(fill="x")
        inner = tk.Frame(header, background=SURFACE)
        inner.pack(fill="x", padx=18, pady=12)
        theme.app_mark(inner).pack(side="left")
        titles = tk.Frame(inner, background=SURFACE)
        titles.pack(side="left", padx=12)
        ttk.Label(titles, text="IFCBuildingStorey Repair Tool", style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text="Move Revit linked-model elements onto the correct master IfcBuildingStorey",
                  style="Muted.Surface.TLabel").pack(anchor="w")
        ttk.Label(inner, text=f"v{__version__}", style="Muted.Surface.TLabel").pack(side="right", padx=(12, 0))
        self.status_pill = theme.pill(inner, "No file", "neutral")
        self.status_pill.pack(side="right")
        tk.Frame(self, background=BORDER, height=1).pack(fill="x")

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self, background=SURFACE)
        bar.pack(side="bottom", fill="x")
        tk.Frame(self, background=BORDER, height=1).pack(side="bottom", fill="x")
        inner = tk.Frame(bar, background=SURFACE)
        inner.pack(fill="x", padx=18, pady=7)
        ttk.Label(inner, textvariable=self.status_var, style="Muted.Surface.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(inner, mode="indeterminate", length=180,
                                        style="Thin.Horizontal.TProgressbar")

    def _section(self, parent, title: str) -> tk.Frame:
        box = theme.card(parent, fill="x", pady=(0, 12))
        inner = tk.Frame(box, background=SURFACE)
        inner.pack(fill="x", padx=16, pady=14)
        ttk.Label(inner, text=title.upper(), style="Section.Surface.TLabel").pack(anchor="w", pady=(0, 8))
        return inner

    def _build_sidebar(self, parent) -> None:
        side = tk.Frame(parent, background=BG, width=310)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        model = self._section(side, "Model")
        ttk.Label(model, textvariable=self.file_title, style="Strong.Surface.TLabel", wraplength=270).pack(anchor="w")
        ttk.Label(model, textvariable=self.file_folder, style="Muted.Surface.TLabel", wraplength=270).pack(anchor="w")
        ttk.Label(model, textvariable=self.file_meta, style="Muted.Surface.TLabel").pack(anchor="w", pady=(2, 0))
        buttons = tk.Frame(model, background=SURFACE)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Open IFC...", style="Primary.TButton", command=self._browse).pack(side="left")
        self.analyze_btn = ttk.Button(buttons, text="Re-analyse", command=self._analyze)
        self.analyze_btn.pack(side="left", padx=(8, 0))

        detection = self._section(side, "Detection")
        ttk.Label(detection, text="Blocks", style="Surface.TLabel").pack(anchor="w")
        self.master_combo = ttk.Combobox(detection, textvariable=self.master_var, values=[AUTO], state="readonly")
        self.master_combo.pack(fill="x", pady=(4, 10))
        ttk.Label(detection, text="Match linked levels by", style="Surface.TLabel").pack(anchor="w")
        segment = tk.Frame(detection, background=SURFACE)
        segment.pack(fill="x", pady=(4, 10))
        for value, label in ((MODE_STOREY, "Each level Z"), (MODE_BRANCH, "Building Z")):
            ttk.Radiobutton(segment, text=label, value=value, variable=self.mode_var,
                            style="Segment.Toolbutton").pack(side="left", fill="x", expand=True)
        ttk.Label(detection, text="Elevation tolerance", style="Surface.TLabel").pack(anchor="w")
        row = tk.Frame(detection, background=SURFACE)
        row.pack(fill="x", pady=(4, 0))
        ttk.Entry(row, textvariable=self.tol_var, width=8).pack(side="left")
        ttk.Label(row, text="metres", style="Muted.Surface.TLabel").pack(side="left", padx=8)

        output = self._section(side, "Output")
        ttk.Checkbutton(output, text="Remove empty linked branches", variable=self.remove_var,
                        style="Surface.TCheckbutton").pack(anchor="w", pady=2)
        ttk.Checkbutton(output, text="Rebase placements to master storey", variable=self.rebase_var,
                        style="Surface.TCheckbutton").pack(anchor="w", pady=2)

        export = theme.card(side, side="bottom", fill="x")
        inner = tk.Frame(export, background=SURFACE)
        inner.pack(fill="x", padx=16, pady=14)
        self.export_btn = ttk.Button(inner, text="Export fixed IFC", style="Primary.TButton",
                                     command=self._export, state="disabled")
        self.export_btn.pack(fill="x")
        ttk.Label(inner, text="Writes a new file with a change log, report and block plan. "
                              "The original IFC is never overwritten.",
                  style="Muted.Surface.TLabel", wraplength=270).pack(anchor="w", pady=(8, 0))

    def _build_main(self, parent) -> None:
        main = ttk.Frame(parent)
        main.pack(side="left", fill="both", expand=True, padx=(16, 0))

        kpis = ttk.Frame(main)
        kpis.pack(fill="x")
        for index, (key, label) in enumerate(KPIS):
            kpis.columnconfigure(index, weight=1, uniform="kpi")
            box = theme.card(kpis)
            box.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 12, 0))
            inner = tk.Frame(box, background=SURFACE)
            inner.pack(fill="x", padx=14, pady=10)
            tk.Label(inner, text=label, background=SURFACE, foreground=MUTED, font=FONTS["small"]).pack(anchor="w")
            value = tk.Label(inner, text="-", background=SURFACE, foreground=TEXT, font=FONTS["kpi"])
            value.pack(anchor="w")
            note = tk.Label(inner, text=" ", background=SURFACE, foreground=MUTED, font=FONTS["small"])
            note.pack(anchor="w")
            self.kpi_values[key] = value
            self.kpi_notes[key] = note

        self.tabs = ttk.Notebook(main)
        self.tabs.pack(fill="both", expand=True, pady=(14, 0))
        self.plan_view = PlanView(self.tabs, on_apply=self._apply_boundaries)
        self.tabs.add(self.plan_view, text="Block plan")
        self.tab_frames["Block plan"] = self.plan_view
        self.buildings = self._tree_tab(
            "Buildings",
            [("role", 80), ("include", 70), ("id", 70), ("guid", 190), ("block", 210), ("z", 100),
             ("storeys", 70), ("elements", 80), ("reason", 440)],
            hint="Double-click a linked building to include or exclude it. Detection then runs again.",
        )
        self.buildings.bind("<Double-1>", self._toggle_include)
        self.mapping = self._tree_tab(
            "Changes",
            [("building", 170), ("source", 230), ("source_z", 90), ("block", 210), ("target", 170),
             ("target_z", 90), ("delta", 70), ("elements", 80), ("status", 80)],
            hint="Source names are labels only. Matching uses absolute elevation.",
        )
        ttk.Checkbutton(self.tab_frames["Changes"], text="Show empty containers", variable=self.show_empty_var,
                        command=self._fill_mapping, style="Surface.TCheckbutton").pack(anchor="w", pady=(8, 0))
        self.elements = self._tree_tab(
            "Elements",
            [("id", 80), ("guid", 190), ("class", 170), ("name", 260), ("relation", 90), ("from", 180),
             ("block", 210), ("to", 150)],
            hint="Every element that will move to a master storey.",
        )
        self.summary = self._text_tab("Report")
        self.log = self._text_tab("Export")

        self.empty = theme.card(main)
        self.empty.place(in_=self.tabs, x=0, y=0, relwidth=1, relheight=1)
        centre = tk.Frame(self.empty, background=SURFACE)
        centre.place(relx=0.5, rely=0.45, anchor="center")
        theme.app_mark(centre, size=46).pack()
        tk.Label(centre, text="Open an IFC file to start", background=SURFACE, foreground=TEXT,
                 font=FONTS["title"]).pack(pady=(14, 4))
        tk.Label(centre, text="The app finds linked-model branches, shows each block on a plan,\n"
                              "and moves their elements to the matching master storeys.",
                 background=SURFACE, foreground=MUTED, font=FONTS["body"], justify="center").pack()
        ttk.Button(centre, text="Open IFC...", style="Primary.TButton", command=self._browse).pack(pady=(16, 0))

    def _tab_frame(self, title: str) -> ttk.Frame:
        frame = ttk.Frame(self.tabs, style="Surface.TFrame", padding=12)
        self.tabs.add(frame, text=title)
        self.tab_frames[title] = frame
        return frame

    def _text_tab(self, title: str) -> tk.Text:
        frame = self._tab_frame(title)
        text = tk.Text(frame, wrap="none", font=FONTS["mono"], background=SURFACE, foreground=TEXT, relief="flat",
                       borderwidth=0, highlightthickness=0, padx=6, pady=4, insertbackground=TEXT)
        ys = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set, state="disabled")
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        text.pack(fill="both", expand=True)
        text.tag_configure("pass", foreground=theme.SUCCESS)
        text.tag_configure("fail", foreground=theme.DANGER, font=(FONTS["mono"][0], FONTS["mono"][1], "bold"))
        text.tag_configure("heading", font=FONTS["bold"])
        return text

    def _tree_tab(self, title: str, columns, hint: str = "") -> ttk.Treeview:
        frame = self._tab_frame(title)
        if hint:
            ttk.Label(frame, text=hint, style="Muted.Surface.TLabel").pack(anchor="w", pady=(0, 8))
        inner = tk.Frame(frame, background=SURFACE, highlightthickness=1, highlightbackground=BORDER)
        inner.pack(fill="both", expand=True)
        tree = ttk.Treeview(inner, columns=[c for c, _ in columns], show="headings")
        for col, width in columns:
            tree.heading(col, text=col.replace("_", " ").upper(), anchor="w")
            tree.column(col, width=width, anchor="w", stretch=col in ("name", "reason", "source"))
        ys = ttk.Scrollbar(inner, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        tree.tag_configure("odd", background=theme.SUBTLE)
        tree.tag_configure("warn", foreground=theme.DANGER)
        tree.tag_configure("master", foreground=theme.PRIMARY)
        tree.tag_configure("muted", foreground="#9CA3AF")
        return tree

    @staticmethod
    def _set_text(widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        for line_no, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("[PASS]") or stripped.startswith("PASS"):
                widget.tag_add("pass", f"{line_no}.0", f"{line_no}.end")
            elif stripped.startswith("[FAIL]") or "CHECK" in stripped or stripped.startswith("[excluded]"):
                widget.tag_add("fail", f"{line_no}.0", f"{line_no}.end")
            elif stripped.endswith(":") and not line.startswith(" "):
                widget.tag_add("heading", f"{line_no}.0", f"{line_no}.end")
        widget.configure(state="disabled")

    # ------------------------------------------------------------ state display

    def _set_status(self, text: str, pill: str, kind: str) -> None:
        self.status_var.set(text)
        theme.set_pill(self.status_pill, pill, kind)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        if busy:
            self.progress.pack(side="right")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()
        self.analyze_btn.configure(state="disabled" if busy or not self.path_var.get() else "normal")

    def _show_file(self, path: str) -> None:
        self.file_title.set(os.path.basename(path))
        self.file_folder.set(os.path.dirname(path))
        try:
            self.file_meta.set(f"{os.path.getsize(path) / 1e6:,.1f} MB")
        except OSError:
            self.file_meta.set("")

    def _fill_kpis(self, r: DetectionReport) -> None:
        linked = r.linked_buildings
        values = {
            "buildings": (len(r.buildings), f"{r.site_count} site(s)"),
            "blocks": (len(r.blocks), "auto + drawn" if getattr(r, "manual_boundaries", None) else "auto-detected"),
            "linked": (sum(b.included for b in linked), f"of {len(linked)} found"),
            "moves": (len(r.moves), f"{len({m.target_id for m in r.moves})} target storeys"),
            "warnings": (len(r.warnings), "see Report" if r.warnings else "none"),
        }
        for key, (value, note) in values.items():
            self.kpi_values[key].configure(text=f"{value:,}")
            self.kpi_notes[key].configure(text=note)
        self.kpi_values["warnings"].configure(foreground=theme.WARN if r.warnings else TEXT)

    # ------------------------------------------------------------ actions

    def _browse(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("IFC files", "*.ifc"), ("All files", "*.*")])
        if path:
            self.path_var.set(path)
            self.include_ids = None
            self.master_var.set(AUTO)
            self.plan_view.boundaries = []
            self._analyze()

    def _repair_options(self) -> RepairOptions | None:
        try:
            tol = float(self.tol_var.get())
            if tol < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid tolerance", "Tolerance must be a non-negative number of metres.")
            return None
        master = self.master_var.get()
        master_id = None if master == AUTO else int(master.split()[0].lstrip("#"))
        return RepairOptions(
            mode=self.mode_var.get(),
            tolerance_m=tol,
            remove_empty_branches=self.remove_var.get(),
            rebase_placements=self.rebase_var.get(),
            master_building_id=master_id,
            include_building_ids=self.include_ids,
            manual_boundaries=list(self.plan_view.boundaries),
        )

    def _run(self, label: str, pill: str, func, on_done) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self._set_status(label, pill, "info")
        self.export_btn.configure(state="disabled")

        def work():
            try:
                self.jobs.put((on_done, func(), None))
            except Exception as exc:  # reported in the UI thread
                self.jobs.put((on_done, None, exc))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                on_done, value, error = self.jobs.get_nowait()
                self._set_busy(False)
                on_done(value, error)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _analyze(self) -> None:
        path = self.path_var.get().strip()
        if not os.path.isfile(path):
            messagebox.showerror("File not found", "Select an existing IFC file.")
            return
        options = self._repair_options()
        if options is None:
            return
        self._show_file(path)
        self._run(f"Analysing {os.path.basename(path)}: reading geometry for block boundaries...", "Analysing",
                  lambda: analyze(path, options), self._analyzed)

    def _analyzed(self, report: DetectionReport | None, error) -> None:
        if error is not None:
            self._set_status("Analysis failed.", "Failed", "danger")
            messagebox.showerror("Analysis failed", str(error))
            return
        self.report = report
        self.empty.place_forget()
        self.file_meta.set(f"{self.file_meta.get().split(' · ')[0]} · {report.schema} · {report.length_unit}")
        self.master_combo.configure(
            values=[AUTO] + [f"#{b.step_id} {b.global_id} ({len(b.storeys)} storeys) - single block"
                             for b in report.buildings]
        )
        self._fill_kpis(report)
        self._set_text(self.summary, format_detection(report))
        self._fill_buildings()
        self._fill_mapping()
        self._fill_elements()
        self.plan_view.show(report, cached_plan_data(report.path))
        can_export = report.repair_needed or (self.remove_var.get() and bool(report.empty_linked_branches))
        self.export_btn.configure(state="normal" if can_export else "disabled")
        if report.repair_needed:
            self._set_status(f"{len(report.moves):,} element(s) to reassign. Check the block plan, then export.",
                             "Repair needed", "warn")
        elif can_export:
            self._set_status("No elements to move. Empty linked branches can be removed.", "Clean-up only", "info")
        else:
            self._set_status("All elements are already in master storeys.", "No repair needed", "success")
        has_plan = bool(getattr(report, "plan_products", None)) and cached_plan_data(report.path) is not None
        self.tabs.select(self.plan_view if has_plan and len(report.buildings) > 1 else self.tab_frames["Report"])

    @staticmethod
    def _row_tags(index: int, *tags: str) -> tuple:
        return tuple(t for t in tags if t) + (("odd",) if index % 2 else ())

    def _fill_buildings(self) -> None:
        tree, r = self.buildings, self.report
        tree.delete(*tree.get_children())
        u = r.length_unit
        for index, b in enumerate(r.buildings):
            tag = "master" if b.role == "master" else ("" if b.included else "muted")
            include = "-" if b.role == "master" else ("yes" if b.included else "no")
            reason = b.reason if b.role == "master" else f"{b.block_reason}; {b.reason}"
            tree.insert("", "end", iid=str(b.step_id), tags=self._row_tags(index, tag), values=(
                b.role, include, b.step_id, b.global_id, self._block_label(b.block_id),
                f"{b.z:,.1f} {u}", len(b.storeys), b.total_elements, reason))

    def _block_label(self, master_id) -> str:
        for number, block in enumerate(self.report.blocks, 1):
            if block.master_id == master_id:
                return f"Block {number} ({block.master_global_id})"
        return ""

    def _fill_mapping(self) -> None:
        if self.report is None:
            return
        tree, r = self.mapping, self.report
        tree.delete(*tree.get_children())
        rows = [m for m in r.mappings if m.element_count or self.show_empty_var.get()]
        for index, m in enumerate(rows):
            ok = m.within_tolerance or not m.element_count
            tree.insert("", "end", tags=self._row_tags(index, "" if ok else "warn"), values=(
                f"#{m.building_id} {m.building_name}", f"{m.source_kind} #{m.source_id} '{m.source_name}'",
                f"{m.source_z:,.1f}", self._block_label(m.target_building_id),
                f"#{m.target_id} {m.target_name}", f"{m.target_z:,.1f}",
                f"{m.delta:,.1f}", m.element_count, "OK" if ok else "CHECK"))

    def _fill_elements(self) -> None:
        tree = self.elements
        tree.delete(*tree.get_children())
        for index, m in enumerate(self.report.moves):
            tree.insert("", "end", tags=self._row_tags(index), values=(
                m.step_id, m.global_id, m.ifc_class, m.name, m.relation, f"#{m.source_id} {m.source_name}",
                self._block_label(m.target_building_id), m.target_name))

    def _apply_boundaries(self) -> None:
        if self.report is None or self.busy:
            return
        self.include_ids = None  # let detection re-decide which branches are included
        self.master_var.set(AUTO)
        self._analyze()

    def _toggle_include(self, event) -> None:
        if self.report is None or self.busy:
            return
        iid = self.buildings.identify_row(event.y)
        building = next((b for b in self.report.buildings if str(b.step_id) == iid), None)
        if building is None or building.role != "linked":
            return
        current = {b.step_id for b in self.report.included_buildings}
        current ^= {building.step_id}
        self.include_ids = current
        self._analyze()

    def _export(self) -> None:
        if self.report is None:
            return
        src = self.path_var.get().strip()
        if any(not m.within_tolerance for m in self.report.mappings if m.element_count):
            if not messagebox.askyesno("Elevation warnings",
                                       "Some linked containers are outside the elevation tolerance.\nExport anyway?"):
                return
        suggestion = default_output_path(src)
        out = filedialog.asksaveasfilename(
            initialdir=os.path.dirname(suggestion), initialfile=os.path.basename(suggestion),
            defaultextension=".ifc", filetypes=[("IFC files", "*.ifc")])
        if not out:
            return
        try:
            check_output_path(src, out, overwrite_output=True)  # dialog already confirmed replacing other files
        except OutputSafetyError as exc:
            messagebox.showerror("Not allowed", str(exc))
            return
        options = self._repair_options()
        if options is None:
            return
        options.include_building_ids = {b.step_id for b in self.report.included_buildings}
        self._run("Repairing and verifying. Large models take a few minutes...", "Exporting",
                  lambda: repair_file(src, out, options, overwrite_output=True), self._exported)

    def _exported(self, result, error) -> None:
        self.export_btn.configure(state="normal")
        if error is not None:
            self._set_status("Export failed.", "Export failed", "danger")
            messagebox.showerror("Export failed", str(error))
            return
        lines = ["Files:", f"  Input       {result.input_path}", f"  Output      {result.output_path or '(not written)'}"]
        if result.output_path:
            lines += [f"  Change log  {result.changelog_csv}", f"  Report      {result.report_md}",
                      f"  Block plan  {result.block_plan_svg or '(no geometry)'}", "",
                      "Summary:",
                      f"  Elements reassigned       {len(result.report.moves):,}",
                      f"  Placements rebased        {len(result.rebased_ids):,}",
                      f"  Spatial elements removed  {len(result.removed_spatial):,}"]
        if result.notes:
            lines += ["", "Notes:"] + [f"  {n}" for n in result.notes]
        if result.verification:
            lines += ["", "Verification:"]
            lines += [f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}" for c in result.verification.checks]
        self._set_text(self.log, "\n".join(lines))
        self.tabs.select(self.tab_frames["Export"])
        passed = result.verification is None or result.verification.passed
        if passed:
            self._set_status(f"Exported {os.path.basename(result.output_path)}. All checks passed.", "Exported", "success")
        else:
            self._set_status("Exported, but some verification checks failed. See the Export tab.", "Check failed", "danger")
        (messagebox.showinfo if passed else messagebox.showwarning)(
            "Export", "Fixed IFC written and verified." if passed
            else "Written, but some verification checks failed. See the Export tab.")


def run() -> None:
    root = tk.Tk()
    root.title("IFCBuildingStorey Repair Tool")
    root.geometry("1360x860")
    root.minsize(1100, 680)
    theme.apply(root)
    theme.window_icon(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    run()
