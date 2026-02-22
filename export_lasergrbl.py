#!/usr/bin/env python
#
# Export to LaserGRBL for laser cutting a document, with basic control of
# speed/power/passes for different colored lines. eg,
# - red is engraved at high speed/low power,
# - green is cut at slow speed/multiple passes
#
# Copyright (C) 2024 Nikki Smith, https://Climbers.net

import configparser
import re
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import gi
import inkex

gi.require_version('Gdk', '3.0')
gi.require_version('Gtk', '3.0')
import warnings

from gi.repository import Gdk, Gtk
from inkex.transforms import Vector2d

warnings.filterwarnings('ignore')  # Suppress all annoying warnings

STEPDOWN_RE = re.compile(r"stepdown\s*[=:]\s*([\d.]+)", re.IGNORECASE)


def errorbox(s):
    dialog = Gtk.MessageDialog(title="Error", flags=0, message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE, text=s)
    dialog.run()
    dialog.destroy()

def infobox(s):
    dialog = Gtk.MessageDialog(title="Info", flags=0, message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.YES_NO, text=s)
    result = dialog.run()
    dialog.destroy()
    return result == Gtk.ResponseType.YES


# launch LaserGRBL with saved Gcode file (cross-platform)
def openfile(filename):
    if sys.platform == "win32":
        os.startfile(filename)
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.call([opener, filename])


def secs2str(secs):
    if secs < 60:
        return str(secs) + "s"
    elif secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    else:
        return f"{secs // 3600}h {(secs // 60) % 60:02d}m"


def xmlget(tree, tag):
    node = tree.find('.//{*}' + tag)
    return "" if node is None else node.text


# convert Gdk.RGBA color into a 24-bit hex string for use in set_markup()
# (has very limited color/styles supports)
def rgba2hex(rgba):
    return f"#{round(rgba.red * 255):02x}{round(rgba.green * 255):02x}{round(rgba.blue * 255):02x}"


# convert Gdk.RGBA (0.0-1.0) into inkex.colors.Color object (0-255)
# TODO alpha is always 1.0 anyway?
def rgba2color(rgba):
    return inkex.colors.Color([round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255), rgba.alpha], space='rgba')


# append color c to palette of unique RGBA colors
def palette_merge(colors, c):
    rgba = Gdk.RGBA(c.red / 255, c.green / 255, c.blue / 255, 1)
    for col in colors:
        if col.equal(rgba):
            return
    colors.append(rgba)


# return first+last points on SVG inkex path iterator
def pathends(pts):
    return Vector2d(pts[0][1]), Vector2d(pts[-1][1])
    # first = next(pts)
    # for p in pts:
    #     last = p
    # return first, last


# simplified GTK functions:
# attach() child to grid with 1x1 cell
def grid_attach1(self, child, col, row):
    self.attach(child, col, row, 1, 1)  # width=1, height=1


# create pulldown using Gtk.ComboBox()
def combo_pulldown(items, on_changed):
    store = Gtk.ListStore(str)
    for item in items:
        store.append([item])
    combo = Gtk.ComboBox.new_with_model(store)
    combo.connect("changed", on_changed)
    renderer_text = Gtk.CellRendererText()
    combo.pack_start(renderer_text, True)
    combo.add_attribute(renderer_text, "text", 0)
    return combo


# probably NOT how you are supposed to extend Python classes??
Gtk.Grid.attach1 = grid_attach1
Gtk.ComboBox.new_pulldown = combo_pulldown


def sort_path(path, height):
    # Use nearest neighbor heuristic to sort paths for minimal travel distance
    lastpt = Vector2d(0, height)
    subpaths = path.copy()
    path[:] = []
    while subpaths:
        min_dist = float('inf')
        min_idx = 0
        min_rev = False
        for i, sp in enumerate(subpaths):
            first, last = pathends(sp)
            dist_first = inkex.bezier.pointdistance(lastpt, first)
            dist_last = inkex.bezier.pointdistance(lastpt, last)
            if dist_last < dist_first:
                dist = dist_last
                rev = True
            else:
                dist = dist_first
                rev = False
            if dist < min_dist:
                min_dist = dist
                min_idx = i
                min_rev = rev
        closest_path = subpaths.pop(min_idx)
        if min_rev:
            closest_path.reverse()
        path.append(closest_path)
        lastpt = pathends(closest_path)[1]


def get_paths(svg, jobs):
    # build 3 lists of elements, one for each job
    paths = [], [], []
    if svg.selection:
        els_all = svg.selection.get()
    else:
        els_all = svg.descendants()
    for el in els_all:
        # convert shape objects to path
        if el.TAG in ['rect', 'circle', 'ellipse']:
            el = el.to_path_element()
        elif el.TAG != 'path':
            continue
        col = el.style.get_color("stroke")
        for i, job in enumerate(jobs):
            if col == job['color']:  # Color() can compare objects
                # break curves into line segments, max error=0.1mm
                # (composed_transform() allows move/rotate/scale transforms)
                path = el.path.transform(el.composed_transform()).to_superpath()
                inkex.bezier.cspsubdiv(path, 0.05)
                paths[i].extend(path)
    for path in paths:
        sort_path(path, svg.viewbox_height)
    return paths


def export_gcode(svg, jobs, filename):
    paths = get_paths(svg, jobs)
    # TODO what if file already exists? prompt user?
    with open(filename, 'w') as fp:
        try:
            fp.write("( Inkscape export_lasergrbl )\nG21\nG90\nG92 Z0\n")  # mm units, absolute coords, set Z=0 at start
        except IOError as e:
            errorbox("Error writing to file: " + e.strerror)
            return
        for path, job in zip(paths, jobs):
            output = ""
            for subpath in path:
                lp = {}
                for p in subpath:
                    # assumes origin is bottom left
                    np = {'x': round(p[1][0], 3), 'y': round(svg.viewbox_height - p[1][1], 3)}
                    if lp:
                        # X: or Y: on single line cuts horiz or vert
                        if np['x'] != lp['x'] and np['y'] != lp['y']:
                            output += f"X{np['x']} Y{np['y']}\n"
                        elif np['x'] != lp['x']:
                            output += f"X{np['x']}\n"
                        elif np['y'] != lp['y']:
                            output += f"Y{np['y']}\n"
                    else:
                        # F: feed rate once per pass? or after every G0 command?
                        # laser power=0, rapid move
                        output += f"S0\nG0 X{np['x']} Y{np['y']}\n"
                        # laser power=0..1000, cutting move speed (mm/sec)
                        output += f"S{round(job['power'] * 10)}\nG1 F{job['speed']}\n"
                    lp = np
            # skip output if empty pass, otherwise repeat as required
            if output:
                z = 0.
                for p in range(1, job['passes'] + 1):
                    fp.write(f"( {job['label']} pass {p}/{job['passes']} )\n")  # DEBUG
                    fp.write(f"G0 Z{z:.3f}\n")
                    z -= job['stepdown']
                    fp.write("M4 ")  # laser ON, power=0
                    fp.write(output)
                    fp.write("M5 S0\n")  # laser OFF, power=0
        fp.write("M30\n")
        if infobox("Exported to:\n\n" + filename + "\n\nDo you want to open this file in LaserGRBL to cut/engrave?"):
            openfile(filename)


class ExportLaserGRBL(inkex.EffectExtension):

    # export as Gcode for LaserGRBL
    def export_clicked(self, widget):
        filename = self.saveas.get_text()
        export_gcode(self.svg, self.jobs, filename)
        # TODO save settings (in document?) so defaults next time it is opened
        Gtk.main_quit()

    def laser_changed(self, widget):
        laser = widget.get_model()[widget.get_active()][0]
        model = self.material_combo.get_model()
        model.clear()
        model.append([""])
        for m in self.materials[laser].keys():
            model.append([m])
        self.material_combo.set_model(model)
        self.material_combo.set_active(0)

    def material_getengrave(self, laser, material):
        for m in self.materials[laser].keys():
            db = self.materials[laser][m]
            # strip trailing " (engrave)"
            if db['cut'] == False and material.startswith(m[:-10]):
                return db
        return {'cut': False, 'power': 0.0, 'speed': 1000, 'cycles': 0, 'remarks': "", 'stepdown': 0.0}

    def material_changed(self, widget):
        index = widget.get_active()
        if index <= 0:
            return
        laser = self.laser_combo.get_model()[self.laser_combo.get_active()][0]
        material = widget.get_model()[index][0]
        if laser not in self.materials or material not in self.materials[laser]:
            self.remarks.set_text("not found!")
            return
        db = self.materials[laser][material]
        # if cut then find 'similar' engrave material, else cutting passes=0
        cut = db['cut']
        if cut:
            db2 = self.material_getengrave(laser, material)
        else:
            db2 = db
            db = {'cycles': 0, 'speed': 1000, 'power': 0, 'stepdown': 0.0}
        for i, job in enumerate(self.jobs):
            job['passes_widget'].set_value(db['cycles'] if i else db2['cycles'])
            job['speed_widget'].set_value(db['speed'] if i else db2['speed'])
            job['power_widget'].set_value(db['power'] if i else db2['power'])
            job['stepdown_widget'].set_value(db['stepdown'] if i else db2['stepdown'])
        self.remarks.set_text(db.get('remarks', ''))

    # calculate document path/travel distances for 3 jobs
    def color_changed(self, color):
        for i, job in enumerate(self.jobs):
            job['rgba'] = job['color_widget'].get_rgba()
            job['color'] = rgba2color(job['rgba'])
            job['hex'] = rgba2hex(job['rgba'])
        self.dist_cut = [0, 0, 0]
        self.dist_travel = [0, 0, 0]
        paths = get_paths(self.svg, self.jobs)
        for i, path in enumerate(paths):
            # assumes laser cutter origin is bottom left. Does this need to be configurable,
            # or read from LaserGRBL settings? (SVG Path Studio assumes top left)
            lastpt = Vector2d(0, self.svg.viewbox_height)
            slengths, self.dist_cut[i] = inkex.bezier.csplength(path)
            for sp in path:
                p1, p2 = pathends(sp)
                travel = inkex.bezier.pointdistance(lastpt, p1)
                lastpt = Vector2d(p2.x, p2.y)
                self.dist_travel[i] += travel
        factor = self.svg.unit_to_viewport(1, "mm")
        for i, d in enumerate(self.dist_cut):
            # TODO optimise travel by not going back to origin for each
            # pass/job? need to store start+end locations for each
            # job, so correctly calc total travel
            self.dist_travel[i] += inkex.bezier.pointdistance(lastpt, Vector2d(0, self.svg.viewbox_height))
            self.dist_cut[i] = round(self.dist_cut[i] * factor)
            self.dist_travel[i] = round(self.dist_travel[i] * factor)
            #logger.debug("[%d] cut=%d mm, travel=%d mm", i, self.dist_cut[i], self.dist_travel[i])
        self.value_changed(None)

    def value_changed(self, spin):
        travel_speed = self.config.getint('DEFAULT', 'maxrate')  # mm/min
        dist_total = 0  # mm total (cut + travel)
        dist_job = {}  # mm cut per job
        dist_travels = 0  # mm travel
        time_total = 0  # secs total (cut + travel)
        time_job = {}  # secs cut per job
        time_travel = 0  # secs travel
        for i, job in enumerate(self.jobs):
            job['passes'] = job['passes_widget'].get_value_as_int()
            job['speed'] = job['speed_widget'].get_value_as_int()
            job['power'] = job['power_widget'].get_value()  # float
            job['stepdown'] = job['stepdown_widget'].get_value()  # float
            dist_total += (self.dist_cut[i] + self.dist_travel[i]) * job['passes']
            dist_job[i] = (self.dist_cut[i] + self.dist_travel[i]) * job['passes']
            dist_travels += self.dist_travel[i] * job['passes']
            tt = (self.dist_travel[i] * job['passes'] * 60) // travel_speed
            time_travel += tt
            time_job[i] = (self.dist_cut[i] * job['passes'] * 60) // job['speed'] + tt
            time_total += time_job[i]
        self.dist_total.set_text(str(dist_total) + " mm")
        self.export_button.set_sensitive(True if dist_total else False)
        s = f"( <span foreground=\"{self.jobs[0]['hex']}\">&#8658;</span> {dist_job[0]} mm  "\
            f"<span foreground=\"{self.jobs[1]['hex']}\">&#8658;</span> {dist_job[1]} mm  "\
            f"<span foreground=\"{self.jobs[2]['hex']}\">&#8658;</span> {dist_job[2]} mm)"
        self.dist_jobs.set_markup(s)
        self.time_total.set_text(secs2str(time_total))
        s = f"( <span foreground=\"{self.jobs[0]['hex']}\">&#8658;</span>{secs2str(time_job[0])}  "\
            f"<span foreground=\"{self.jobs[1]['hex']}\">&#8658;</span>{secs2str(time_job[1])}  "\
            f"<span foreground=\"{self.jobs[2]['hex']}\">&#8658;</span>{secs2str(time_job[2])})"
        self.time_jobs.set_markup(s)

    def grid_head(self, grid, sa):
        for s in sa:
            l = Gtk.Label(xalign=0)
            l.set_markup(s)
            grid.add(l)

    def grid_rows(self, grid, jobs, color_changed, value_changed):
        for i, job in enumerate(jobs):
            grid.attach1(Gtk.Label(label=job['label'], xalign=0), 0, i + 1)
            job['color_widget'] = Gtk.ColorButton(rgba=job['rgba'])
            grid.attach1(job['color_widget'], 1, i + 1)
            job['color_widget'].connect('color-set', color_changed)
            job['color_widget'].add_palette(Gtk.Orientation.HORIZONTAL, 9, self.palette)
            job['passes_widget'] = Gtk.SpinButton(
                numeric=True,
                max_length=2,
                adjustment=Gtk.Adjustment(lower=0, upper=50, step_increment=1, page_increment=5),
                value=job['passes']
            )
            grid.attach1(job['passes_widget'], 2, i + 1)
            job['passes_widget'].connect("value-changed", value_changed)
            job['speed_widget'] = Gtk.SpinButton(
                numeric=True,
                max_length=4,
                adjustment=Gtk.Adjustment(lower=50, upper=5000, step_increment=50, page_increment=250),
                value=job['speed']
            )
            grid.attach1(job['speed_widget'], 3, i + 1)
            job['speed_widget'].connect("value-changed", value_changed)
            job['power_widget'] = Gtk.SpinButton(
                numeric=True,
                max_length=5,
                digits=1,
                adjustment=Gtk.Adjustment(lower=0.0, upper=100.0, step_increment=1.0, page_increment=5.0),
                value=job['power']
            )
            grid.attach1(job['power_widget'], 4, i + 1)
            job['power_widget'].connect("value-changed", value_changed)
            job['stepdown_widget'] = Gtk.SpinButton(
                numeric=True,
                max_length=5,
                digits=2,
                adjustment=Gtk.Adjustment(lower=0.0, upper=10.0, step_increment=0.1, page_increment=0.5),
                value=job['stepdown']
            )
            grid.attach1(job['stepdown_widget'], 5, i + 1)
            job['stepdown_widget'].connect("value-changed", value_changed)

    # parse local copy of materials db:
    # copied from %APPDATA%\LaserGRBL\UserMaterials.xml
    def materials_load(self):
        self.materials = {}  # dict of [laser model][material]
        try:
            document = ET.parse("UserMaterials.psh").getroot()
        except FileNotFoundError as e:
            errorbox("Error reading materials file 'UserMaterials.psh': " + e.strerror)
            return
        except ET.ParseError:
            errorbox("Error parsing materials file 'UserMaterials.psh'")
            return
        for node in document.findall('.//{*}Materials'):
            # TODO skip if 'Material' or 'Model' is missing
            if xmlget(node, 'Visible') != "true":
                continue
            thickness = xmlget(node, 'Thickness')
            cut = xmlget(node, 'Action') != "Engrave" and thickness
            material = xmlget(node, 'Material') + " "
            material += thickness if cut else "(engrave)"
            model = xmlget(node, 'Model')
            if model not in self.materials:
                self.materials[model] = {}
            # TODO better error handling if numbers are invalid
            self.materials[model][material] = {
                'cut': cut,
                'power': float(xmlget(node, 'Power') or '0'),
                'speed': int(xmlget(node, 'Speed') or '50'),
                'cycles': int(xmlget(node, 'Cycles') or '1'),
                'remarks': xmlget(node, 'Remarks'),
                'stepdown': 0.0
            }
            if m := STEPDOWN_RE.search(xmlget(node, 'Remarks')):
                self.materials[model][material]['stepdown'] = float(m.group(1))

    # load settings from .ini file, with fallback defaults
    def config(self):
        self.config = configparser.ConfigParser()
        self.config['DEFAULT'] = {
            'laser': '',
            'material': '',
            'colors': '#ff0000,#008000,#0000ff',
            'maxrate': '1200',
            'darktheme': '1'
        }
        # TODO Inkscape v1.5+ sets $GTK_THEME if theme.darkTheme=1 ?
        self.config.read("export_lasergrbl.ini")
        hexcols = self.config.get('DEFAULT', 'colors').split(',')
        self.gdkcols = []
        for i in range(3):
            gdk = Gdk.RGBA()  # defaults to white
            if i < len(hexcols):
                gdk.parse(hexcols[i])
            self.gdkcols.append(gdk)
        # build palette of unique colors used in doc (not just selection)
        # c_old used to skip 'expensive' merge if elements repeat color
        self.palette = self.gdkcols.copy()
        c_old = inkex.colors.Color()
        els = self.svg.descendants()
        for el in els:
            if el.TAG in ['path', 'rect', 'circle', 'ellipse']:
                c = el.style.get_color("stroke")
                if c != c_old:  # Color() can compare objects
                    palette_merge(self.palette, c)
                    c_old = c

    # if options set laser model/material, use them!
    def config_pulldowns(self):
        laser = self.config.get('DEFAULT', 'laser')
        if laser and laser in self.materials:
            i = list(self.materials).index(laser)
            self.laser_combo.set_active(i)  # triggers _changed()
            material = self.config.get('DEFAULT', 'material')
            if material and material in self.materials[laser]:
                j = list(self.materials[laser]).index(material)
                self.material_combo.set_active(j + 1)  # triggers _changed()

    def effect(self):
        s = "selection " if self.svg.selection else ""
        window = Gtk.Window(title="Export " + s + "to LaserGRBL")
        # TODO use Gtk.HeaderBar to set icon & remove min/max controls?
        window.set_border_width(15)
        window.connect("delete-event", Gtk.main_quit)
        vbox = Gtk.Box(spacing=12, orientation=Gtk.Orientation.VERTICAL)
        self.config()
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-application-prefer-dark-theme", self.config.getboolean('DEFAULT', 'darktheme'))

        # grid (laser model, material pulldown lists)
        grid = Gtk.Grid(row_spacing=3, column_spacing=12)
        grid.add(Gtk.Label(label="Laser model", xalign=0))
        self.materials_load()
        self.laser_combo = Gtk.ComboBox.new_pulldown(self.materials.keys(), self.laser_changed)
        grid.add(self.laser_combo)
        grid.attach1(Gtk.Label(label="Material", xalign=0), 0, 1)
        self.material_combo = Gtk.ComboBox.new_pulldown([], self.material_changed)
        grid.attach1(self.material_combo, 1, 1)
        self.remarks = Gtk.Label(xalign=0)
        grid.attach1(self.remarks, 2, 1)
        vbox.add(grid)

        # grid (job, passes, speed, power)
        grid = Gtk.Grid(row_spacing=3, column_spacing=12)
        self.grid_head(grid, ["<b>Color</b>", "", "<b>Passes</b>", "<b>Speed</b> mm/min", "<b>Power</b> %", "<b>Stepdown</b> mm"])
        self.jobs = [{
            'label': "Engrave",
            'rgba': self.gdkcols[0],
            'passes': 1,
            'speed': 900,
            'power': 40.0,
            'stepdown': 0.0
        }, {
            'label': "Inner Cut",
            'rgba': self.gdkcols[1],
            'passes': 2,
            'speed': 175,
            'power': 100.0,
            'stepdown': 0.5
        }, {
            'label': "Outer Cut",
            'rgba': self.gdkcols[2],
            'passes': 2,
            'speed': 175,
            'power': 100.0,
            'stepdown': 0.5
        }]
        self.grid_rows(grid, self.jobs, self.color_changed, self.value_changed)
        vbox.add(grid)

        # show summary of path length + approx laser time, split by job
        grid = Gtk.Grid(row_spacing=3, column_spacing=20)
        grid.add(Gtk.Label(label="Distance", xalign=0))
        self.dist_total = Gtk.Label(label="0 mm", xalign=0)
        grid.add(self.dist_total)
        self.dist_jobs = Gtk.Label(xalign=0)
        grid.add(self.dist_jobs)
        grid.attach1(Gtk.Label(label="Time", xalign=0), 0, 1)
        self.time_total = Gtk.Label(label="0s", xalign=0)
        grid.attach1(self.time_total, 1, 1)
        self.time_jobs = Gtk.Label(xalign=0)
        grid.attach1(self.time_jobs, 2, 1)
        vbox.add(grid)

        # save as filename selector
        # text entry since Gtk.FileChooserButton() doesn't support SAVE
        box = Gtk.Box(spacing=25)
        box.add(Gtk.Label(label="Save as", xalign=0))
        self.saveas = Gtk.Entry()
        name = self.document_path()
        name = os.path.splitext(name)[0] + ".nc" if name else "gcode.nc"
        self.saveas.set_text(name)
        box.pack_start(self.saveas, True, True, 0)
        vbox.add(box)

        # action buttons
        box = Gtk.Box(spacing=5, halign=Gtk.Align.END)
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", Gtk.main_quit)
        box.add(close_button)
        s = "Selection" if self.svg.selection else "All"
        self.export_button = Gtk.Button(label="Export " + s)
        self.export_button.connect("clicked", self.export_clicked)
        box.add(self.export_button)
        vbox.add(box)

        self.config_pulldowns()
        self.color_changed(None)

        window.add(vbox)
        window.show_all()
        Gtk.main()


#logger = logging.getLogger(__name__)
if __name__ == '__main__':
    #logging.basicConfig(filename='error.log', level=logging.DEBUG)
    ExportLaserGRBL().run()
