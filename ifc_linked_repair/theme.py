"""Flat, light theme for the Tkinter UI (no third-party packages)."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

BG = "#F3F4F6"          # window background
SURFACE = "#FFFFFF"     # cards, sidebar, tables
SUBTLE = "#F9FAFB"      # alternating rows, hover
BORDER = "#E5E7EB"
TEXT = "#111827"
MUTED = "#6B7280"
PRIMARY = "#2563EB"
PRIMARY_HOVER = "#1D4ED8"
PRIMARY_SOFT = "#EFF6FF"
PRIMARY_DISABLED = "#BFD3F6"
SUCCESS, SUCCESS_SOFT = "#15803D", "#DCFCE7"
WARN, WARN_SOFT = "#B45309", "#FEF3C7"
DANGER, DANGER_SOFT = "#B91C1C", "#FEE2E2"

FONTS: dict[str, tuple] = {}


def _pick(families: set[str], *names: str) -> str:
    return next((n for n in names if n in families), "TkDefaultFont")


def apply(root: tk.Tk) -> None:
    families = set(tkfont.families(root))
    ui = _pick(families, "Segoe UI Variable Text", "Segoe UI", "Helvetica Neue", "Arial")
    display = _pick(families, "Segoe UI Variable Display", "Segoe UI Semibold", ui)
    mono = _pick(families, "Cascadia Mono", "Consolas", "Menlo", "Courier New")
    FONTS.update(
        body=(ui, 10),
        small=(ui, 9),
        bold=(ui, 10, "bold"),
        section=(ui, 9, "bold"),
        title=(display, 14, "bold"),
        kpi=(display, 20, "bold"),
        mono=(mono, 10),
    )
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(name).configure(family=ui, size=10)

    root.configure(background=BG)
    style = ttk.Style(root)
    style.theme_use("clam")
    flat = dict(bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE)

    style.configure(".", background=BG, foreground=TEXT, font=FONTS["body"], focuscolor=PRIMARY_SOFT)
    style.configure("TFrame", background=BG)
    style.configure("Surface.TFrame", background=SURFACE)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Surface.TLabel", background=SURFACE)
    style.configure("Muted.Surface.TLabel", background=SURFACE, foreground=MUTED, font=FONTS["small"])
    style.configure("Muted.TLabel", foreground=MUTED, font=FONTS["small"])
    style.configure("Section.Surface.TLabel", background=SURFACE, foreground=MUTED, font=FONTS["section"])
    style.configure("Title.TLabel", font=FONTS["title"], background=SURFACE)
    style.configure("Strong.Surface.TLabel", background=SURFACE, font=FONTS["bold"])

    # buttons: drop the dotted focus ring, keep a 1 px border
    button_layout = [("Button.border", {"sticky": "nswe", "border": "1", "children": [
        ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})]
    for name in ("TButton", "Primary.TButton", "Ghost.TButton"):
        style.layout(name, button_layout)
    style.layout("Segment.Toolbutton", [("Toolbutton.border", {"sticky": "nswe", "children": [
        ("Toolbutton.padding", {"sticky": "nswe", "children": [("Toolbutton.label", {"sticky": "nswe"})]})]})])
    style.configure("TButton", padding=(12, 6), relief="solid", background=SURFACE, foreground=TEXT,
                    borderwidth=1, **flat)
    style.map("TButton", background=[("disabled", SURFACE), ("pressed", BORDER), ("active", SUBTLE)],
              foreground=[("disabled", "#9CA3AF")], bordercolor=[("focus", PRIMARY)])
    style.configure("Primary.TButton", background=PRIMARY, foreground="#FFFFFF", font=FONTS["bold"],
                    bordercolor=PRIMARY, lightcolor=PRIMARY, darkcolor=PRIMARY, padding=(16, 8))
    style.map("Primary.TButton",
              background=[("disabled", PRIMARY_DISABLED), ("pressed", PRIMARY_HOVER), ("active", PRIMARY_HOVER)],
              bordercolor=[("disabled", PRIMARY_DISABLED), ("active", PRIMARY_HOVER)],
              lightcolor=[("disabled", PRIMARY_DISABLED), ("active", PRIMARY_HOVER)],
              darkcolor=[("disabled", PRIMARY_DISABLED), ("active", PRIMARY_HOVER)],
              foreground=[("disabled", "#FFFFFF")])
    style.configure("Ghost.TButton", background=SURFACE, bordercolor=SURFACE, lightcolor=SURFACE,
                    darkcolor=SURFACE, foreground=PRIMARY, padding=(8, 4))
    style.map("Ghost.TButton", background=[("active", PRIMARY_SOFT)], bordercolor=[("active", PRIMARY_SOFT)])

    # segmented control (radio buttons drawn as toggle buttons)
    style.configure("Segment.Toolbutton", background=SURFACE, foreground=MUTED, padding=(10, 5),
                    anchor="center", borderwidth=1, relief="solid", **flat)
    style.map("Segment.Toolbutton",
              background=[("selected", PRIMARY_SOFT), ("active", SUBTLE)],
              foreground=[("selected", PRIMARY)],
              bordercolor=[("selected", PRIMARY)])

    # inputs
    for widget in ("TEntry", "TCombobox"):
        style.configure(widget, fieldbackground=SURFACE, background=SURFACE, foreground=TEXT,
                        padding=(8, 5), arrowcolor=MUTED, **flat)
        style.map(widget, bordercolor=[("focus", PRIMARY)], lightcolor=[("focus", SURFACE)],
                  fieldbackground=[("readonly", SURFACE)], selectbackground=[("readonly", SURFACE)],
                  selectforeground=[("readonly", TEXT)])
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.selectBackground", PRIMARY_SOFT)
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    root.option_add("*TCombobox*Listbox.font", FONTS["body"])
    # check boxes: custom rounded box with a tick, drawn as images
    images = _checkbox_images(root)
    try:
        style.element_create("Tick.indicator", "image", images["off"],
                             ("selected", "disabled", images["on_disabled"]), ("disabled", images["off"]),
                             ("selected", images["on"]), ("active", images["hover"]), border=0, sticky="w")
    except tk.TclError:
        pass  # theme already applied to this interpreter
    check_layout = [("Checkbutton.padding", {"sticky": "nswe", "children": [
        ("Tick.indicator", {"side": "left", "sticky": ""}),
        ("Checkbutton.label", {"side": "left", "sticky": "nswe"})]})]
    for name in ("TCheckbutton", "Surface.TCheckbutton"):
        style.layout(name, check_layout)
        style.configure(name, background=SURFACE, foreground=TEXT, padding=(0, 3), space=8)
        style.map(name, background=[("active", SURFACE)])
    style.configure("TRadiobutton", background=SURFACE)
    style.map("TRadiobutton", background=[("active", SURFACE)], indicatorcolor=[("selected", PRIMARY)])

    # tabs: flat, selected tab joins the white page
    style.configure("TNotebook", background=BG, borderwidth=1, tabmargins=(0, 0, 0, 0),
                    bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE)
    style.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(16, 8), borderwidth=0,
                    bordercolor=BG, lightcolor=BG, darkcolor=BG, font=FONTS["bold"])
    style.map("TNotebook.Tab",
              background=[("selected", SURFACE), ("active", "#E9EBEF")],
              foreground=[("selected", PRIMARY)],
              lightcolor=[("selected", SURFACE)], bordercolor=[("selected", SURFACE)])

    # tables
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT, rowheight=28,
                    borderwidth=0, relief="flat", font=FONTS["body"])
    style.map("Treeview", background=[("selected", PRIMARY_SOFT)], foreground=[("selected", TEXT)])
    style.configure("Treeview.Heading", background=SURFACE, foreground=MUTED, font=FONTS["section"],
                    relief="flat", padding=(8, 7), borderwidth=0, bordercolor=BORDER, lightcolor=SURFACE,
                    darkcolor=BORDER)
    style.map("Treeview.Heading", background=[("active", SUBTLE)])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    # scrollbars and progress
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", troughcolor=SURFACE, background="#D1D5DB", bordercolor=SURFACE,
                        lightcolor="#D1D5DB", darkcolor="#D1D5DB", arrowcolor=MUTED, gripcount=0,
                        arrowsize=12, relief="flat")
        style.map(f"{orient}.TScrollbar", background=[("active", "#9CA3AF")])
    style.configure("Thin.Horizontal.TProgressbar", troughcolor=BORDER, background=PRIMARY, bordercolor=BORDER,
                    lightcolor=PRIMARY, darkcolor=PRIMARY, thickness=4)


def _checkbox_images(root: tk.Tk) -> dict[str, tk.PhotoImage]:
    """16 px check boxes: rounded square, filled with a white tick when selected."""
    size = 16

    def box(fill: str, edge: str, tick: bool) -> tk.PhotoImage:
        image = tk.PhotoImage(master=root, width=size + 2, height=size)
        for y in range(size):
            row = []
            for x in range(size):
                corner = (x in (0, size - 1)) and (y in (0, size - 1))
                if corner:
                    row.append(SURFACE)
                elif x in (0, size - 1) or y in (0, size - 1):
                    row.append(edge)
                else:
                    row.append(fill)
            row += [SURFACE, SURFACE]
            image.put("{" + " ".join(row) + "}", to=(0, y))
        if tick:
            for x, y in ((4, 8), (5, 9), (6, 10), (7, 9), (8, 8), (9, 7), (10, 6), (11, 5)):
                image.put("#FFFFFF", to=(x, y, x + 1, y + 2))
        return image

    images = {
        "off": box(SURFACE, "#9CA3AF", False),
        "hover": box(PRIMARY_SOFT, PRIMARY, False),
        "on": box(PRIMARY, PRIMARY, True),
        "on_disabled": box(PRIMARY_DISABLED, PRIMARY_DISABLED, True),
    }
    root._checkbox_images = images  # keep references alive
    return images


def pill(parent, text: str = "", kind: str = "neutral") -> tk.Label:
    label = tk.Label(parent, text=text, font=FONTS["section"], padx=10, pady=3, bd=0)
    set_pill(label, text, kind)
    return label


def set_pill(label: tk.Label, text: str, kind: str) -> None:
    colours = {
        "neutral": (SUBTLE, MUTED),
        "info": (PRIMARY_SOFT, PRIMARY),
        "success": (SUCCESS_SOFT, SUCCESS),
        "warn": (WARN_SOFT, WARN),
        "danger": (DANGER_SOFT, DANGER),
    }[kind]
    label.configure(text=text, background=colours[0], foreground=colours[1])


def card(parent, **pack) -> tk.Frame:
    frame = tk.Frame(parent, background=SURFACE, highlightthickness=1, highlightbackground=BORDER,
                     highlightcolor=BORDER, bd=0)
    if pack:
        frame.pack(**pack)
    return frame


def app_mark(parent, size: int = 30) -> tk.Canvas:
    canvas = tk.Canvas(parent, width=size, height=size, background=SURFACE, highlightthickness=0)
    r = 7
    s = size - 1
    points = [r, 0, s - r, 0, s, 0, s, r, s, s - r, s, s, s - r, s, r, s, 0, s, 0, s - r, 0, r, 0, 0]
    canvas.create_polygon(points, smooth=True, fill=PRIMARY, outline="")
    # three stacked storeys
    for i, y in enumerate((9, 15, 21)):
        canvas.create_rectangle(8 + i, y - 2, size - 8 - i, y, fill="#FFFFFF", outline="")
    return canvas


def window_icon(root: tk.Tk) -> None:
    image = tk.PhotoImage(width=32, height=32)
    image.put(PRIMARY, to=(2, 2, 30, 30))
    for y in (10, 16, 22):
        image.put("#FFFFFF", to=(8, y, 24, y + 3))
    root.iconphoto(True, image)
    root._icon_image = image  # keep a reference
