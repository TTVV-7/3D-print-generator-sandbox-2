"""The solid-building and file-writing half of the print sandbox's cards.py,
which is all a topo map needs of it.

A part is a dict: `groups` (its pieces, each with a colour `slot`), `slots`
(those gathered by colour), and `mesh` (all of it).  The plate, the STL and
both 3MF writers take a list of them.  Copied from TTVV-7/3D-print-sandbox
rather than imported, so this repository builds on its own.
"""
import trimesh
from shapely.geometry import Polygon
from shapely.ops import unary_union

# The face layer the other generators inlay their colours in.  A map has no
# face, but its info reports one, as theirs do.
FACE = 0.6

# The four colours, in the order the 3MF's materials and the viewer use them.
# For a map: land, water, pins, and one spare.
SLOTS = ("body", "pattern", "primary", "secondary")
COLOURS = ("#f5f2ec", "#5aa9e6", "#ff5b1f", "#ff5b1f")


def boolean(op, meshes):
    return getattr(trimesh.boolean, op)(meshes, engine="manifold")


# How far a hole is nudged to unpick a ring that touches itself.  A nanometre:
# small enough that it is nothing -- on a stencil plate it moves the cut by
# about a part in ten million of its area -- and large enough that the two
# sides of the touch stop sharing a vertex.


# How far a hole is nudged to unpick a ring that touches itself.  A nanometre:
# small enough that it is nothing -- on a stencil plate it moves the cut by
# about a part in ten million of its area -- and large enough that the two
# sides of the touch stop sharing a vertex.
UNPINCH = 1e-6


def unpinch(poly, eps=UNPINCH):
    """The same shape with any hole that touches itself at a point unpicked.

    A hole whose ring comes back to the same point -- the waist of a figure-8
    counter, which the ornate faces are full of -- is a perfectly valid
    polygon and extrudes to a broken mesh: the wall at the touch is one edge
    with four faces on it rather than two, so the solid is not manifold and
    the boolean engine downstream is entitled to refuse it.  Growing every
    hole by a nanometre and re-cutting them separates the touch into two
    edges.  Nothing else about the shape moves; the area changes in the
    seventh significant figure.
    """
    if not poly.interiors:
        return poly
    holes = unary_union([Polygon(r).buffer(eps, join_style=2) for r in poly.interiors])
    return Polygon(poly.exterior).difference(holes)


def prisms(polys, z0, thickness):
    """One solid per polygon; the caller hands them all to a single boolean."""
    out = []
    for i, poly in enumerate(polys):
        # A micron of tolerance: enough to drop the doubled vertex a clip can
        # leave behind, which earcut turns into an open mesh; nothing else
        # here is drawn that finely.
        # The repair is only built if the plain extrusion comes out open, which
        # on everything but the ornate faces it never does.
        for attempt in (lambda: poly, lambda: unpinch(poly)):
            shape = attempt()
            solids = [shape] if shape.geom_type == "Polygon" else list(shape.geoms)
            meshes = [trimesh.creation.extrude_polygon(g.simplify(0.001), thickness)
                      for g in solids if g.area > 0]
            if meshes and all(m.is_watertight for m in meshes):
                break
        else:
            raise ValueError(f"shape {i} did not extrude to a closed solid")
        mesh = meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
        mesh.apply_translation((0.0, 0.0, z0))
        out.append(mesh)
    return out


def union(meshes):
    return meshes[0] if len(meshes) == 1 else boolean("union", meshes)


def arrange(bounds, gap=6.0, row_w=None):
    """Shifts that lay boxes side by side along x on z = 0, wrapping into
    rows no wider than `row_w`.  `bounds` are (lo, hi) corner pairs."""
    out, x, y, row_h = [], 0.0, 0.0, 0.0
    for lo, hi in bounds:
        pw, ph = hi[0] - lo[0], hi[1] - lo[1]
        if row_w and x > 0 and x + pw > row_w:
            x, y, row_h = 0.0, y - row_h - gap, 0.0
        out.append((x - lo[0], y - hi[1], -lo[2]))
        x += pw + gap
        row_h = max(row_h, ph)
    return out


def layout(parts, gap=6.0, row_w=None):
    """[(part, (dx, dy, dz)), ...]: the parts side by side along x on z = 0,
    wrapping into rows no wider than `row_w` -- a batch on one plate."""
    shifts = arrange([tuple(part["mesh"].bounds) for part in parts], gap, row_w)
    return list(zip(parts, shifts))


def plate(parts, gap=6.0, row_w=None):
    """The parts laid out on one build plate, as a single mesh.

    They are disjoint solids, so this is a concatenation rather than a boolean
    -- every slicer reads it as a multi-part object.
    """
    out = []
    for part, shift in layout(parts, gap, row_w):
        mesh = part["mesh"].copy()
        mesh.apply_translation(shift)
        out.append(mesh)
    return trimesh.util.concatenate(out) if len(out) > 1 else out[0]


def slot_meshes(part):
    """The part's groups gathered by colour: what the 3MF's materials want.

    Groups within a slot are disjoint solids in different places on the face,
    so this is a concatenation, not a boolean.
    """
    by_slot = {}
    for g in part["groups"]:
        by_slot.setdefault(g["slot"], []).append(g["mesh"])
    return {slot: (m[0] if len(m) == 1 else trimesh.util.concatenate(m))
            for slot, m in by_slot.items()}


def export_3mf(parts, colours=COLOURS, gap=6.0, row_w=None):
    """The parts as a 3MF, each colour slot a separate component of one
    object per part, four base materials, so the slicer opens it already
    knowing which filament goes where.

    Written by hand rather than through trimesh's exporter, because what
    matters here is the structure -- one object per part, a component per
    colour, the materials named -- and that is easier to get exactly right
    in forty lines of XML than to coax out of a general-purpose scene writer.
    """
    import io
    import zipfile

    colours = tuple(colours) + tuple(COLOURS[len(colours):])

    def mesh_xml(oid, mesh, pindex, name):
        v = "".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>'
                    for x, y, z in mesh.vertices)
        t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.faces)
        return (f'<object id="{oid}" type="model" name="{name}" pid="1" pindex="{pindex}">'
                f'<mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh></object>')

    objects, items, oid = [], [], 2          # id 1 is the material list
    for part, (dx, dy, dz) in layout(parts, gap, row_w):
        label = " ".join(x for x in (part.get("label", ""), part["name"]) if x) or "card"
        ids = []
        for pindex, slot in enumerate(SLOTS):
            mesh = part["slots"].get(slot)
            if mesh is None:
                continue
            objects.append(mesh_xml(oid, mesh, pindex, f"{label} {slot}"))
            ids.append(oid)
            oid += 1
        comps = "".join(f'<component objectid="{i}"/>' for i in ids)
        objects.append(f'<object id="{oid}" type="model" name="{label}">'
                       f'<components>{comps}</components></object>')
        items.append(f'<item objectid="{oid}" '
                     f'transform="1 0 0 0 1 0 0 0 1 {dx:.4f} {dy:.4f} {dz:.4f}"/>')
        oid += 1

    bases = "".join(f'<base name="{slot.capitalize()}" displaycolor="{c}"/>'
                    for slot, c in zip(SLOTS, colours))
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<resources><basematerials id="1">{bases}</basematerials>'
        f'{"".join(objects)}</resources>'
        f'<build>{"".join(items)}</build></model>')
    types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        '</Types>')
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        '</Relationships>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)
    return buf.getvalue()


HEADS = 4       # filaments the printer holds at once: the Snapmaker U1's four heads


def export_3mf_tools(parts, filaments, gap=6.0, row_w=None, heads=HEADS, pauses=()):
    """The parts as a 3MF a slicer opens ready to print: one object per part,
    one volume per colour slot, and each volume already on its own filament.

    `filaments` is one dict per slot in SLOTS order -- hex, material, name --
    and every distinct (material, colour) the parts use becomes one extruder,
    numbered in slot order.  So a TPU tag with PETG lettering comes out as two
    filaments, and two slots that use the same spool share one.

    export_3mf() above writes each slot as a *component*, which a slicer is
    free to read as separate objects -- PrusaSlicer does, and drops the
    lettering on the bed beside the tag.  This writes what PrusaSlicer writes
    itself: each part is one mesh, the slots are ranges of its triangles, and
    Metadata/Slic3r_PE_model.config names each range's extruder.  The
    filaments' colours and materials go in Metadata/Slic3r_PE.config, the
    project settings, which is also where the slicer learns there is more
    than one of them.  The Orca family of slicers -- Snapmaker's among them --
    reads both files when it opens a PrusaSlicer 3MF.

    `pauses` is a list of (print_z, note): the heights at which the print
    should stop -- to drop in an NFC chip -- written the way PrusaSlicer
    files its own pauses, so the slicer adds them rather than you finding the
    layer by hand.  print_z is the top of the first layer printed *after* the
    pause.
    """
    import io
    import zipfile
    from xml.sax.saxutils import quoteattr

    used = [s for s in SLOTS if any(s in part["slots"] for part in parts)]
    tools, extruder = [], {}
    for slot in used:
        f = filaments[SLOTS.index(slot)]
        key = (f["material"], f["hex"].lower())
        if key not in [(t["material"], t["hex"].lower()) for t in tools]:
            tools.append(f)
        extruder[slot] = 1 + [(t["material"], t["hex"].lower()) for t in tools].index(key)
    if len(tools) > heads:
        raise ValueError(f"this design needs {len(tools)} filaments and the printer holds "
                         f"{heads} -- use the same spool for two of the colours")

    objects, items, configs = [], [], []
    for oid, (part, (dx, dy, dz)) in enumerate(layout(parts, gap, row_w), start=1):
        label = " ".join(x for x in (part.get("label", ""), part["name"]) if x) or "part"
        verts, tris, volumes, nv, nt = [], [], [], 0, 0
        for slot in SLOTS:
            mesh = part["slots"].get(slot)
            if mesh is None:
                continue
            verts.append("".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>'
                                 for x, y, z in mesh.vertices))
            tris.append("".join(f'<triangle v1="{a + nv}" v2="{b + nv}" v3="{c + nv}"/>'
                                for a, b, c in mesh.faces))
            f = filaments[SLOTS.index(slot)]
            name = f"{slot} - {f.get('name') or f['hex']} {f['material']}"
            volumes.append(
                f' <volume firstid="{nt}" lastid="{nt + len(mesh.faces) - 1}">\n'
                f'  <metadata type="volume" key="name" value={quoteattr(name)}/>\n'
                f'  <metadata type="volume" key="volume_type" value="ModelPart"/>\n'
                f'  <metadata type="volume" key="extruder" value="{extruder[slot]}"/>\n'
                f' </volume>\n')
            nv += len(mesh.vertices)
            nt += len(mesh.faces)
        objects.append(f'<object id="{oid}" type="model" name={quoteattr(label)}>'
                       f'<mesh><vertices>{"".join(verts)}</vertices>'
                       f'<triangles>{"".join(tris)}</triangles></mesh></object>')
        items.append(f'<item objectid="{oid}" '
                     f'transform="1 0 0 0 1 0 0 0 1 {dx:.4f} {dy:.4f} {dz:.4f}"/>')
        configs.append(f'<object id="{oid}" instances_count="1">\n'
                       f' <metadata type="object" key="name" value={quoteattr(label)}/>\n'
                       f' <metadata type="object" key="extruder" value="1"/>\n'
                       + "".join(volumes) + '</object>\n')

    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">'
        '<metadata name="slic3rpe:Version3mf">1</metadata>'
        f'<resources>{"".join(objects)}</resources>'
        f'<build>{"".join(items)}</build></model>')
    model_config = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
                    + "".join(configs) + '</config>\n')
    colours = ";".join(t["hex"].upper() for t in tools)
    project = "".join(f"; {k} = {v}\n" for k, v in (
        ("extruder_colour", colours),
        ("filament_colour", colours),
        ("filament_type", ";".join(t["material"].split()[0].upper() for t in tools)),
        ("filament_settings_id", ";".join(
            f'"{t.get("name") or t["hex"]} {t["material"]}"' for t in tools)),
        # Flat, face down, nothing overhanging: supports would only scar the
        # face they touch.
        # The purge -- a prime tower, or none on a toolchanger -- is the
        # printer profile's business, and forcing one clashes with profiles
        # that address the extruder absolutely.
        ("support_material", 0),
    ))
    types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        '<Default Extension="config" ContentType="text/plain"/>'
        '</Types>')
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        '</Relationships>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/Slic3r_PE_model.config", model_config)
        z.writestr("Metadata/Slic3r_PE.config", project)
        if pauses:
            codes = "".join(
                f'<code print_z="{z:.3f}" type="1" extruder="1" color="" '
                f'extra={quoteattr(note)} gcode="M601"/>\n' for z, note in pauses)
            mode = "MultiExtruder" if len(tools) > 1 else "SingleExtruder"
            z.writestr("Metadata/Prusa_Slicer_custom_gcode_per_print_z.xml",
                       '<?xml version="1.0" encoding="utf-8"?>\n<custom_gcodes_per_print_z>\n'
                       + codes + f'<mode value="{mode}"/>\n</custom_gcodes_per_print_z>\n')
    return buf.getvalue()
