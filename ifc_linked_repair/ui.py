"""Tkinter desktop UI. All IFC logic lives in detection / repair / verify."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .detection import analyze
from .footprint import cached_plan_data
from .plan_view import PlanView
from .model import MODE_BRANCH, MODE_STOREY, DetectionReport, RepairOptions
from .repair import OutputSafetyError, check_output_path, default_output_path, repair_file
from .reporting import format_detection

AUTO = "Auto-detect blocks"


class App(ttk.Frame):
    def __init__(self, root: tk.Tk):
        super().__init__(root, padding=8)
        self.root = root
        self.report: DetectionReport | None = None
        self.include_ids: set[int] | None = None
        self.jobs: queue.Queue = queue.Queue()
        self.busy = False
        self.tab_frames: dict[str, ttk.Frame] = {}

        self.path_var = tk.StringVar()
        self.master_var = tk.StringVar(value=AUTO)
        self.mode_var = tk.StringVar(value=MODE_STOREY)
        self.tol_var = tk.StringVar(value="0.3")
        self.remove_var = tk.BooleanVar(value=True)
        self.rebase_var = tk.BooleanVar(value=True)
        self.show_empty_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Load an IFC file to begin.")

        self._build()
        self.pack(fill="both", expand=True)
        self.root.after(100, self._poll)

    # ------------------------------------------------------------ layout

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="IFC file:").pack(side="left")
        ttk.Entry(top, textvariable=self.path_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="Browse...", command=self._browse).pack(side="left")
        self.analyze_btn = ttk.Button(top, text="Analyze", command=self._analyze)
        self.analyze_btn.pack(side="left", padx=(4, 0))

        opts = ttk.LabelFrame(self, text="Options", padding=6)
        opts.pack(fill="x", pady=6)
        ttk.Label(opts, text="Master building:").grid(row=0, column=0, sticky="w")
        self.master_combo = ttk.Combobox(opts, textvariable=self.master_var, values=[AUTO], state="readonly", width=42)
        self.master_combo.grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(opts, text="Match by:").grid(row=0, column=2, sticky="w", padx=(12, 0))
        ttk.Radiobutton(opts, text="each linked storey Z", value=MODE_STOREY, variable=self.mode_var).grid(row=0, column=3, sticky="w")
        ttk.Radiobutton(opts, text="linked building Z", value=MODE_BRANCH, variable=self.mode_var).grid(row=0, column=4, sticky="w")
        ttk.Label(opts, text="Tolerance (m):").grid(row=1, column=0, sticky="w")
        ttk.Entry(opts, textvariable=self.tol_var, width=8).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Checkbutton(opts, text="Remove empty linked branches", variable=self.remove_var).grid(row=1, column=2, columnspan=2, sticky="w", padx=(12, 0))
        ttk.Checkbutton(opts, text="Rebase placements to master storey", variable=self.rebase_var).grid(row=1, column=4, sticky="w")

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        self.summary = self._text_tab("Detection report")
        self.plan_view = PlanView(self.tabs, on_apply=self._apply_boundaries)
        self.tabs.add(self.plan_view, text="Block plan")
        self.buildings = self._tree_tab(
            "Buildings",
            [("role", 70), ("include", 70), ("id", 70), ("guid", 190), ("block", 190), ("z", 90),
             ("storeys", 70), ("elements", 80), ("reason", 420)],
            hint="Double-click a linked building to include / exclude it, then the plan is recalculated.",
        )
        self.buildings.bind("<Double-1>", self._toggle_include)
        self.mapping = self._tree_tab(
            "Proposed changes",
            [("building", 170), ("source", 220), ("source_z", 90), ("block", 190), ("target", 160),
             ("target_z", 90), ("delta", 70), ("elements", 80), ("status", 80)],
            hint="Source labels are display names only; matching uses absolute Z.",
        )
        ttk.Checkbutton(
            self.tab_frames["Proposed changes"], text="Show empty containers",
            variable=self.show_empty_var, command=self._fill_mapping,
        ).pack(anchor="w")
        self.elements = self._tree_tab(
            "Element preview",
            [("id", 70), ("guid", 190), ("class", 170), ("name", 260), ("relation", 80), ("from", 170),
             ("block", 190), ("to", 140)],
        )
        self.log = self._text_tab("Export log")

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", pady=(6, 0))
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left")
        self.export_btn = ttk.Button(bottom, text="Export fixed IFC...", command=self._export, state="disabled")
        self.export_btn.pack(side="right")

    def _text_tab(self, title: str) -> tk.Text:
        frame = ttk.Frame(self.tabs)
        text = tk.Text(frame, wrap="none", font=("Consolas", 10))
        ys = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set, state="disabled")
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        text.pack(fill="both", expand=True)
        self.tabs.add(frame, text=title)
        self.tab_frames[title] = frame
        return text

    def _tree_tab(self, title: str, columns, hint: str = "") -> ttk.Treeview:
        frame = ttk.Frame(self.tabs)
        if hint:
            ttk.Label(frame, text=hint, foreground="#555").pack(anchor="w")
        inner = ttk.Frame(frame)
        inner.pack(fill="both", expand=True)
        tree = ttk.Treeview(inner, columns=[c for c, _ in columns], show="headings")
        for col, width in columns:
            tree.heading(col, text=col.replace("_", " ").title())
            tree.column(col, width=width, anchor="w", stretch=col in ("name", "reason", "source"))
        ys = ttk.Scrollbar(inner, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        tree.tag_configure("warn", foreground="#b00020")
        tree.tag_configure("master", foreground="#0b5394")
        tree.tag_configure("muted", foreground="#888")
        self.tabs.add(frame, text=title)
        self.tab_frames[title] = frame
        return tree

    @staticmethod
    def _set_text(widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

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

    def _run(self, label: str, func, on_done) -> None:
        if self.busy:
            return
        self.busy = True
        self.status_var.set(label)
        self.analyze_btn.configure(state="disabled")
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
                self.busy = False
                self.analyze_btn.configure(state="normal")
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
        self._run(f"Analyzing {os.path.basename(path)} (reading geometry for block boundaries)...", lambda: analyze(path, options), self._analyzed)

    def _analyzed(self, report: DetectionReport | None, error) -> None:
        if error is not None:
            self.status_var.set("Analysis failed.")
            messagebox.showerror("Analysis failed", str(error))
            return
        self.report = report
        self.master_combo.configure(
            values=[AUTO] + [f"#{b.step_id} {b.global_id} ({len(b.storeys)} storeys) - single block"
                             for b in report.buildings]
        )
        self._set_text(self.summary, format_detection(report))
        self._fill_buildings()
        self._fill_mapping()
        self._fill_elements()
        self.plan_view.show(report, cached_plan_data(report.path))
        can_export = report.repair_needed or (self.remove_var.get() and bool(report.empty_linked_branches))
        self.export_btn.configure(state="normal" if can_export else "disabled")
        if report.repair_needed:
            self.status_var.set(f"Repair needed: {len(report.moves)} element(s) to reassign. Review, then export.")
        elif can_export:
            self.status_var.set("No elements to move; empty linked branches can be removed.")
        else:
            self.status_var.set("No repair needed.")
        has_plan = bool(getattr(report, "plan_products", None)) and cached_plan_data(report.path) is not None
        self.tabs.select(self.plan_view if has_plan and len(report.buildings) > 1 else self.tab_frames["Detection report"])

    def _fill_buildings(self) -> None:
        tree, r = self.buildings, self.report
        tree.delete(*tree.get_children())
        u = r.length_unit
        for b in r.buildings:
            tag = "master" if b.role == "master" else ("" if b.included else "muted")
            include = "-" if b.role == "master" else ("yes" if b.included else "no")
            block = self._block_label(b.block_id)
            reason = b.reason if b.role == "master" else f"{b.block_reason}; {b.reason}"
            tree.insert("", "end", iid=str(b.step_id), tags=(tag,), values=(
                b.role, include, b.step_id, b.global_id, block,
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
        for m in r.mappings:
            if not m.element_count and not self.show_empty_var.get():
                continue
            ok = m.within_tolerance or not m.element_count
            tree.insert("", "end", tags=("" if ok else "warn",), values=(
                f"#{m.building_id} {m.building_name}", f"{m.source_kind} #{m.source_id} '{m.source_name}'",
                f"{m.source_z:,.1f}", self._block_label(m.target_building_id),
                f"#{m.target_id} {m.target_name}", f"{m.target_z:,.1f}",
                f"{m.delta:,.1f}", m.element_count, "OK" if ok else "CHECK"))

    def _fill_elements(self) -> None:
        tree = self.elements
        tree.delete(*tree.get_children())
        for m in self.report.moves:
            tree.insert("", "end", values=(m.step_id, m.global_id, m.ifc_class, m.name, m.relation,
                                           f"#{m.source_id} {m.source_name}",
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
        self._run("Repairing and verifying...",
                  lambda: repair_file(src, out, options, overwrite_output=True), self._exported)

    def _exported(self, result, error) -> None:
        if error is not None:
            self.status_var.set("Export failed.")
            messagebox.showerror("Export failed", str(error))
            self.export_btn.configure(state="normal")
            return
        lines = [f"Input:  {result.input_path}", f"Output: {result.output_path or '(not written)'}"]
        if result.output_path:
            lines += [f"Change log: {result.changelog_csv}", f"Report: {result.report_md}",
                      f"Block plan: {result.block_plan_svg or '(no geometry)'}",
                      f"Elements reassigned: {len(result.report.moves)}",
                      f"Placements rebased: {len(result.rebased_ids)}",
                      f"Spatial elements removed: {len(result.removed_spatial)}"]
        lines += [f"Note: {n}" for n in result.notes]
        if result.verification:
            lines.append("")
            lines.append("Verification:")
            lines += [f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}" for c in result.verification.checks]
        self._set_text(self.log, "\n".join(lines))
        self.tabs.select(self.tab_frames["Export log"])
        self.export_btn.configure(state="normal")
        passed = result.verification is None or result.verification.passed
        self.status_var.set("Export complete." if passed else "Export complete with verification FAILURES.")
        (messagebox.showinfo if passed else messagebox.showwarning)(
            "Export", "Fixed IFC written and verified." if passed else "Written, but some verification checks failed. See Export log.")


def run() -> None:
    root = tk.Tk()
    root.title(f"IFC Linked Storey Repair {__version__}")
    root.geometry("1180x720")
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    run()
