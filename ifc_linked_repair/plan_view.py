"""Block plan tab: shows every element coloured by its block and lets the user
draw block boundaries when auto-detection is wrong."""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

import numpy as np

from .footprint import points_in_polygon
from .model import DetectionReport, ManualBoundary, load_boundaries, save_boundaries
from .theme import BORDER, FONTS, SURFACE

PALETTE = ["#2563EB", "#059669", "#D97706", "#7C3AED", "#DB2777", "#0891B2", "#65A30D", "#475569",
           "#EA580C", "#4F46E5", "#0D9488", "#A16207"]
EXCLUDED = "#DC2626"
ZONE = "#111827"


def _tint(hex_colour: str, amount: float) -> str:
    """Blend a colour with white; amount 0 = colour, 1 = white."""
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    r, g, b = (int(v + (255 - v) * amount) for v in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


class PlanView(ttk.Frame):
    def __init__(self, master, on_apply: Callable[[], None]):
        super().__init__(master, style="Surface.TFrame", padding=12)
        self.on_apply = on_apply
        self.report: Optional[DetectionReport] = None
        self.plan = None
        self.boundaries: list[ManualBoundary] = []
        self.drawing: Optional[list[tuple[float, float]]] = None
        self.item_building: dict[int, int] = {}
        self.a, self.b, self.c = 1.0, 0.0, 0.0  # model -> canvas: x' = a*x + b, y' = -a*y + c
        self.hint = tk.StringVar(value="Analyze a file to see its block plan.")
        self._user_view = False  # stop auto-fitting once the user zooms or pans
        self._fit_job = None
        self._fitted_size = (0, 0)
        self.hover = tk.StringVar(value="")
        self._build()

    # ------------------------------------------------------------ layout

    def _build(self) -> None:
        bar = ttk.Frame(self, style="Surface.TFrame")
        bar.pack(fill="x", pady=(0, 8))
        self.draw_btn = ttk.Button(bar, text="Draw block boundary", command=self.start_drawing)
        self.draw_btn.pack(side="left")
        ttk.Button(bar, text="Fit view", command=self._refit).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Clear drawn", command=self.clear_boundaries).pack(side="left", padx=(8, 0))
        tk.Frame(bar, background=BORDER, width=1).pack(side="left", fill="y", padx=12, pady=4)
        ttk.Button(bar, text="Load...", command=self.load).pack(side="left")
        ttk.Button(bar, text="Save...", command=self.save).pack(side="left", padx=(8, 0))
        self.apply_btn = ttk.Button(bar, text="Apply drawn boundaries", style="Primary.TButton", command=self.on_apply)
        self.apply_btn.pack(side="right")

        ttk.Label(self, textvariable=self.hint, style="Muted.Surface.TLabel").pack(anchor="w", pady=(0, 6))
        body = ttk.Frame(self, style="Surface.TFrame")
        body.pack(fill="both", expand=True)
        self.legend = ttk.Frame(body, style="Surface.TFrame", width=280)
        self.legend.pack(side="right", fill="y", padx=(12, 0))
        self.canvas = tk.Canvas(body, background="#FBFBFC", highlightthickness=1, highlightbackground=BORDER)
        self.canvas.pack(side="left", fill="both", expand=True)
        ttk.Label(self, textvariable=self.hover, style="Muted.Surface.TLabel").pack(anchor="w", pady=(6, 0))

        c = self.canvas
        c.bind("<Configure>", self._resized)
        c.bind("<MouseWheel>", self._wheel)  # Windows / macOS
        c.bind("<Button-4>", lambda e: self._zoom(e, 1.2))  # X11
        c.bind("<Button-5>", lambda e: self._zoom(e, 1 / 1.2))
        c.bind("<ButtonPress-3>", self._right_press)
        c.bind("<B3-Motion>", self._pan)
        c.bind("<ButtonPress-2>", lambda e: c.scan_mark(e.x, e.y))
        c.bind("<B2-Motion>", self._pan)
        c.bind("<ButtonPress-1>", self._left_press)
        c.bind("<B1-Motion>", self._left_drag)
        c.bind("<Double-Button-1>", self._finish_drawing)
        c.bind("<Motion>", self._motion)
        c.bind("<Escape>", lambda e: self.cancel_drawing())
        c.bind("<Return>", self._finish_drawing)
        c.bind("<BackSpace>", lambda e: self._undo_point())

    # ------------------------------------------------------------ transform

    def to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return self.a * x + self.b, -self.a * y + self.c

    def to_model(self, event) -> tuple[float, float]:
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        return (cx - self.b) / self.a, (self.c - cy) / self.a

    def _pan(self, event) -> None:
        self._user_view = True
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def _wheel(self, event) -> None:
        self._zoom(event, 1.2 if event.delta > 0 else 1 / 1.2)

    def _resized(self, event) -> None:
        if self._user_view or self.report is None or (event.width, event.height) == self._fitted_size:
            return
        if self._fit_job is not None:
            self.after_cancel(self._fit_job)
        self._fit_job = self.after(200, self.fit)

    def _zoom(self, event, factor: float) -> None:
        self._user_view = True
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        self.canvas.scale("all", cx, cy, factor, factor)
        self.a *= factor
        self.b = cx + factor * (self.b - cx)
        self.c = cy + factor * (self.c - cy)

    # ------------------------------------------------------------ drawing the model

    def show(self, report: DetectionReport, plan) -> None:
        self.report, self.plan = report, plan
        self._user_view = False
        self.boundaries = list(getattr(report, "manual_boundaries", []) or self.boundaries)
        self.fit()
        self._fill_legend()
        if plan is None or not plan.element_hulls:
            self.hint.set("No geometry available for a plan view (geometry engine unavailable or no elements).")
        else:
            self.hint.set("Wheel = zoom, right-drag = pan, hover = building info. "
                          "Linked branches are darker than their block master; red = linked branch not included.")

    def fit(self) -> None:
        self._fit_job = None
        if self.report is None or self.plan is None:
            return
        self.canvas.update_idletasks()
        w = max(self.canvas.winfo_width(), 400)
        h = max(self.canvas.winfo_height(), 300)
        self._fitted_size = (self.canvas.winfo_width(), self.canvas.winfo_height())
        hulls = [hh for ids in self.report.plan_products.values() for i in ids
                 if (hh := self.plan.element_hulls.get(i)) is not None]
        if not hulls:
            self.canvas.delete("all")
            return
        pts = np.vstack(hulls)
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        self.a = 0.95 * min(w / max(x1 - x0, 1e-9), h / max(y1 - y0, 1e-9))
        self.b = w / 2 - self.a * (x0 + x1) / 2
        self.c = h / 2 + self.a * (y0 + y1) / 2
        # the view may have been panned: place the drawing in the visible window
        self.b += self.canvas.canvasx(0)
        self.c += self.canvas.canvasy(0)
        self.redraw()

    def _refit(self) -> None:
        self._user_view = False
        self.fit()

    def block_colours(self) -> dict[int, str]:
        return {k.master_id: PALETTE[i % len(PALETTE)] for i, k in enumerate(self.report.blocks)}

    def redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        self.item_building.clear()
        colours = self.block_colours()
        ordered = sorted(self.report.buildings, key=lambda b: (b.role != "master", b.step_id))
        for b in ordered:
            base = colours.get(b.block_id, "#999999")
            if b.role == "master":
                fill = _tint(base, 0.72)
            elif b.role == "linked" and not b.included:
                fill = _tint(EXCLUDED, 0.35)
            else:
                fill = _tint(base, 0.30)
            for step_id in self.report.plan_products.get(b.step_id, ()):
                hull = self.plan.element_hulls.get(step_id)
                if hull is None or len(hull) < 3:
                    continue
                coords = []
                for x, y in hull:
                    coords.extend(self.to_canvas(x, y))
                item = c.create_polygon(coords, fill=fill, outline="", tags=("geom",))
                self.item_building[item] = b.step_id
        self._draw_boundaries()

    def _draw_boundaries(self) -> None:
        c = self.canvas
        c.delete("zone")
        for i, zone in enumerate(self.boundaries):
            coords = [v for x, y in zone.points for v in self.to_canvas(x, y)]
            c.create_polygon(coords, fill="", outline=ZONE, width=2, dash=(6, 3), tags=("zone", f"zone{i}"))
            cx = sum(coords[0::2]) / len(zone.points)
            cy = sum(coords[1::2]) / len(zone.points)
            c.create_text(cx, cy, text=zone.name or f"Boundary {i + 1}", fill=ZONE,
                          font=FONTS.get("bold", ("Segoe UI", 10, "bold")), tags=("zone", f"zone{i}"))

    def _fill_legend(self) -> None:
        for child in self.legend.winfo_children():
            child.destroy()
        ttk.Label(self.legend, text="BLOCKS", style="Section.Surface.TLabel").pack(anchor="w", pady=(0, 6))
        colours = self.block_colours()
        by_id = {b.step_id: b for b in self.report.buildings}
        for k in self.report.blocks:
            master = by_id[k.master_id]
            linked = sum(1 for i in k.member_ids if i != k.master_id)
            self._legend_row(colours[k.master_id], master.global_id,
                             f"#{k.master_id} · {len(k.storeys)} storeys · {linked} linked")
        excluded = [b for b in self.report.linked_buildings if not b.included]
        if excluded:
            self._legend_row(EXCLUDED, "Not included", f"{len(excluded)} linked branch(es) left out")
        if self.boundaries:
            ttk.Label(self.legend, text="DRAWN BOUNDARIES", style="Section.Surface.TLabel").pack(anchor="w", pady=(14, 4))
            for zone in self.boundaries:
                master = next((b for b in self.report.buildings
                               if zone.master_global_id and b.global_id == zone.master_global_id), None)
                detail = f"master #{master.step_id}" if master else "master: tallest inside"
                self._legend_row(ZONE, zone.name or "Boundary", detail, outline=True)
            ttk.Label(self.legend, text="Right-click an outline to delete it.",
                      style="Muted.Surface.TLabel").pack(anchor="w", pady=(4, 0))

    def _legend_row(self, colour: str, title: str, detail: str, outline: bool = False) -> None:
        row = ttk.Frame(self.legend, style="Surface.TFrame")
        row.pack(fill="x", pady=4)
        swatch = tk.Canvas(row, width=14, height=14, background=SURFACE, highlightthickness=0)
        if outline:
            swatch.create_rectangle(2, 2, 12, 12, outline=colour, dash=(2, 1), width=2)
        else:
            swatch.create_oval(1, 1, 13, 13, fill=colour, outline="")
        swatch.pack(side="left", anchor="n", pady=3)
        text = ttk.Frame(row, style="Surface.TFrame")
        text.pack(side="left", padx=8)
        ttk.Label(text, text=title, style="Strong.Surface.TLabel").pack(anchor="w")
        ttk.Label(text, text=detail, style="Muted.Surface.TLabel").pack(anchor="w")

    # ------------------------------------------------------------ hover / pan

    def _motion(self, event) -> None:
        c = self.canvas
        if self.drawing:
            c.delete("rubber")
            x, y = self.to_canvas(*self.drawing[-1])
            c.create_line(x, y, c.canvasx(event.x), c.canvasy(event.y), fill=ZONE, dash=(4, 2), tags=("rubber",))
            return
        if self.report is None:
            return
        items = c.find_overlapping(c.canvasx(event.x), c.canvasy(event.y),
                                   c.canvasx(event.x) + 1, c.canvasy(event.y) + 1)
        building_id = next((self.item_building[i] for i in reversed(items) if i in self.item_building), None)
        if building_id is None:
            self.hover.set("")
            return
        b = next(x for x in self.report.buildings if x.step_id == building_id)
        state = "block master" if b.role == "master" else ("included" if b.included else "NOT included")
        self.hover.set(f"#{b.step_id} {b.global_id}  {b.role} ({state})  block #{b.block_id}  "
                       f"{len(b.storeys)} storeys, {b.total_elements} elements  - {b.block_reason}")

    def _right_press(self, event) -> None:
        c = self.canvas
        hit = c.find_overlapping(c.canvasx(event.x) - 3, c.canvasy(event.y) - 3,
                                 c.canvasx(event.x) + 3, c.canvasy(event.y) + 3)
        for item in hit:
            tags = [t for t in c.gettags(item) if t.startswith("zone") and t != "zone"]
            if tags and not self.drawing:
                index = int(tags[0][4:])
                zone = self.boundaries[index]
                if messagebox.askyesno("Delete boundary", f"Delete '{zone.name or index + 1}'?"):
                    del self.boundaries[index]
                    self._draw_boundaries()
                    self._fill_legend()
                return
        c.scan_mark(event.x, event.y)

    def _left_press(self, event) -> None:
        self.canvas.focus_set()
        if self.drawing is None:
            self.canvas.scan_mark(event.x, event.y)
            return
        point = self.to_model(event)
        if self.drawing:
            px, py = self.to_canvas(*self.drawing[-1])
            if abs(px - self.canvas.canvasx(event.x)) < 3 and abs(py - self.canvas.canvasy(event.y)) < 3:
                return  # second click of a double-click
        self.drawing.append(point)
        self._draw_sketch()

    def _left_drag(self, event) -> None:
        if self.drawing is None:
            self._pan(event)

    # ------------------------------------------------------------ boundary drawing

    def start_drawing(self) -> None:
        if self.report is None:
            return
        self.drawing = []
        self.canvas.focus_set()
        self.canvas.configure(cursor="crosshair")
        self.hint.set("Drawing: click each corner of the block outline. Double-click or Enter to finish, "
                      "Backspace = undo point, Esc = cancel. Right-drag still pans, wheel zooms.")

    def cancel_drawing(self) -> None:
        self.drawing = None
        self.canvas.delete("sketch", "rubber")
        self.canvas.configure(cursor="")
        self.show_hint()

    def show_hint(self) -> None:
        self.hint.set("Wheel = zoom, right-drag = pan, hover = building info. Draw boundaries, then "
                      "'Apply drawn boundaries' to re-run detection.")

    def _undo_point(self) -> None:
        if self.drawing:
            self.drawing.pop()
            self._draw_sketch()

    def _draw_sketch(self) -> None:
        c = self.canvas
        c.delete("sketch", "rubber")
        pts = [v for p in self.drawing for v in self.to_canvas(*p)]
        if len(self.drawing) >= 2:
            c.create_line(*pts, fill=ZONE, width=2, tags=("sketch",))
        for i in range(0, len(pts), 2):
            c.create_oval(pts[i] - 3, pts[i + 1] - 3, pts[i] + 3, pts[i + 1] + 3, fill=ZONE, tags=("sketch",))

    def _finish_drawing(self, event=None) -> None:
        if self.drawing is None:
            return
        if len(self.drawing) < 3:
            messagebox.showinfo("Boundary", "A boundary needs at least 3 corners.")
            return
        points = list(self.drawing)
        self.cancel_drawing()
        zone = self._ask_master(points)
        if zone is not None:
            self.boundaries.append(zone)
            self._draw_boundaries()
            self._fill_legend()
            self.hint.set(f"{len(self.boundaries)} boundary(ies) drawn. Click 'Apply drawn boundaries' to re-run detection.")

    def buildings_inside(self, points) -> list:
        """Buildings with most of their elements inside the polygon, tallest first."""
        found = []
        for b in self.report.buildings:
            hulls = [h for i in self.report.plan_products.get(b.step_id, ())
                     if (h := self.plan.element_hulls.get(i)) is not None]
            if not hulls:
                continue
            share = points_in_polygon(np.array([h.mean(axis=0) for h in hulls]), points).mean()
            if share >= 0.6:
                found.append((b, share))
        return sorted(found, key=lambda bs: (-len(bs[0].storeys), -bs[0].total_elements))

    def _ask_master(self, points) -> Optional[ManualBoundary]:
        inside = self.buildings_inside(points)
        dialog = tk.Toplevel(self)
        dialog.title("New block boundary")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        ttk.Label(dialog, text=f"{len(inside)} building(s) have most of their elements inside this boundary.",
                  padding=6).pack(anchor="w")
        name = tk.StringVar(value=f"Block {len(self.boundaries) + 1}")
        row = ttk.Frame(dialog, padding=(6, 0))
        row.pack(fill="x")
        ttk.Label(row, text="Name:").pack(side="left")
        ttk.Entry(row, textvariable=name, width=30).pack(side="left", padx=4)
        ttk.Label(dialog, text="Block master (receives the elements):", padding=(6, 6, 6, 0)).pack(anchor="w")
        listbox = tk.Listbox(dialog, width=90, height=min(12, len(inside) + 1), exportselection=False)
        listbox.insert("end", "Auto: tallest building inside")
        for b, share in inside:
            listbox.insert("end", f"#{b.step_id} {b.global_id}  {len(b.storeys)} storeys, "
                                  f"{b.total_elements} elements, {share:.0%} inside  ({b.role})")
        listbox.selection_set(0)
        listbox.pack(fill="both", expand=True, padx=6)
        result: list[Optional[ManualBoundary]] = [None]

        def ok():
            pick = listbox.curselection()
            master = "" if not pick or pick[0] == 0 else inside[pick[0] - 1][0].global_id
            result[0] = ManualBoundary(points=points, name=name.get().strip(), master_global_id=master)
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=6)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Add boundary", command=ok).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right", padx=4)
        dialog.wait_window()
        return result[0]

    def clear_boundaries(self) -> None:
        if self.boundaries and messagebox.askyesno("Clear", "Remove all drawn boundaries?"):
            self.boundaries.clear()
            self._draw_boundaries()
            self._fill_legend()

    def save(self) -> None:
        if not self.boundaries:
            messagebox.showinfo("Save boundaries", "No drawn boundaries to save.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Boundaries", "*.json")])
        if path:
            save_boundaries(path, self.boundaries, self.report.length_unit, self.report.path)

    def load(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Boundaries", "*.json")])
        if not path:
            return
        try:
            self.boundaries = load_boundaries(path)
        except Exception as exc:
            messagebox.showerror("Load boundaries", f"Could not read {path}: {exc}")
            return
        if self.report is not None:
            self._draw_boundaries()
            self._fill_legend()
        self.hint.set(f"Loaded {len(self.boundaries)} boundary(ies). Click 'Apply drawn boundaries' to use them.")
