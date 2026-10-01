"""Признаки элементов по геометрии IFC: криволинейность и радиус, наклон, отметки,
высота этажа, переменное сечение. Считаются по типу элемента (ObjectType).

CLI: python -m src.services.ifc_features [папка_сессии]   (по умолчанию — последняя)
Результат: <сессия>/ifc_features.json
"""
import os, sys, glob, json, math, logging
import numpy as np
import ifcopenshell
import ifcopenshell.util.placement as uplace
import ifcopenshell.util.unit as uunit
import ifcopenshell.util.element as uel

logger = logging.getLogger(__name__)
CLASSES = ("IfcWall", "IfcSlab", "IfcColumn", "IfcBeam", "IfcStair", "IfcStairFlight", "IfcRamp",
           "IfcRampFlight", "IfcFooting", "IfcRoof", "IfcMember", "IfcPlate", "IfcCovering",
           "IfcBuildingElementProxy", "IfcPile", "IfcRailing")


def _r3(p1, p2, p3):
    """радиус окружности по трём точкам"""
    a, b, c = (np.linalg.norm(np.subtract(p2, p3)), np.linalg.norm(np.subtract(p1, p3)),
               np.linalg.norm(np.subtract(p1, p2)))
    s = (a + b + c) / 2
    area = max(s * (s - a) * (s - b) * (s - c), 0) ** 0.5
    return (a * b * c) / (4 * area) if area > 1e-9 else None


def _is_arc_polyline(pts):
    """>=4 точек, все повороты малые и в одну сторону — это дуга, а не угол «Г»"""
    if len(pts) < 4:
        return False
    p = np.array([q[:2] for q in pts], dtype=float)
    turns = []
    for i in range(1, len(p) - 1):
        a, b = p[i] - p[i - 1], p[i + 1] - p[i]
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na < 1e-9 or nb < 1e-9:
            continue
        cr = (a[0] * b[1] - a[1] * b[0]) / (na * nb)
        ang = math.degrees(math.asin(max(-1.0, min(1.0, cr))))
        turns.append(ang)
    if len(turns) < 2:
        return False
    return all(0.2 < abs(t) < 30 for t in turns) and (all(t > 0 for t in turns) or all(t < 0 for t in turns))


def _curve_arcs(c, scale, out, allow_poly=False):
    """ищем дуги в кривой: радиусы (м) складываем в out"""
    if c is None:
        return
    t = c.is_a()
    if t == "IfcCircle":
        out.append(c.Radius * scale)
    elif t == "IfcEllipse":
        out.append(min(c.SemiAxis1, c.SemiAxis2) * scale)
    elif t == "IfcTrimmedCurve":
        _curve_arcs(c.BasisCurve, scale, out, allow_poly)
    elif t == "IfcCompositeCurve":
        for seg in c.Segments or []:
            _curve_arcs(getattr(seg, "ParentCurve", None), scale, out, allow_poly)
    elif t == "IfcIndexedPolyCurve":
        pts = c.Points.CoordList
        if allow_poly and not c.Segments and _is_arc_polyline(pts):
            # дуга, разбитая на отрезки (ось стены из Revit)
            r = _r3(pts[0], pts[len(pts) // 2], pts[-1])
            if r and r * scale < 500:
                out.append(r * scale)
        for seg in c.Segments or []:
            if seg.is_a("IfcArcIndex"):
                i = [x - 1 for x in seg.wrappedValue]
                r = _r3(pts[i[0]], pts[i[1]], pts[i[2]])
                if r:
                    out.append(r * scale)


def _walk_items(items, scale, arcs_axis, arcs_body, dirs, tapered, where):
    for it in items or []:
        t = it.is_a()
        if t == "IfcMappedItem":
            _walk_items(it.MappingSource.MappedRepresentation.Items, scale, arcs_axis, arcs_body, dirs, tapered, where)
        elif t in ("IfcBooleanResult", "IfcBooleanClippingResult"):
            _walk_items([it.FirstOperand], scale, arcs_axis, arcs_body, dirs, tapered, where)
        elif t.startswith("IfcExtrudedAreaSolid"):
            if t == "IfcExtrudedAreaSolidTapered":
                tapered.append(True)
            prof = it.SweptArea
            if prof.is_a("IfcArbitraryClosedProfileDef"):
                _curve_arcs(prof.OuterCurve, scale, arcs_body)
            elif prof.is_a("IfcCircleProfileDef"):
                arcs_body.append(prof.Radius * scale)
            try:
                m = uplace.get_axis2placement(it.Position) if it.Position else np.eye(4)
                d = np.array(it.ExtrudedDirection.DirectionRatios, dtype=float)
                dirs.append(m[:3, :3] @ d)
            except Exception:
                pass
        elif t.endswith("Curve") or t in ("IfcCircle", "IfcPolyline"):
            _curve_arcs(it, scale, arcs_axis if where == "Axis" else arcs_body, allow_poly=(where == "Axis"))


def element_features(el, scale):
    f = {}
    try:
        M = uplace.get_local_placement(el.ObjectPlacement)
    except Exception:
        M = np.eye(4)
    f["z"] = float(M[2][3]) * scale
    arcs_axis, arcs_body, dirs, tapered = [], [], [], []
    rep = el.Representation
    for r in (rep.Representations if rep else []):
        _walk_items(r.Items, scale, arcs_axis, arcs_body, dirs, tapered, r.RepresentationIdentifier)
    if arcs_axis:
        f["curved"] = True
        f["radius"] = round(min(arcs_axis), 2)
    elif arcs_body and el.is_a("IfcWall"):
        f["curved_contour"] = True
        f["radius"] = round(min(arcs_body), 2)
    elif arcs_body and el.is_a() in ("IfcSlab", "IfcColumn", "IfcBeam", "IfcFooting"):
        f["curved_contour"] = True
    for d in dirs:
        wd = M[:3, :3] @ d
        n = np.linalg.norm(wd)
        if n > 0:
            ang = math.degrees(math.acos(min(1.0, abs(wd[2]) / n)))
            if ang > 1.0 and ang < 89.0:
                f["tilt_deg"] = round(max(f.get("tilt_deg", 0), ang), 1)
    if tapered:
        f["tapered"] = True
    try:
        pa = (uel.get_psets(el).get("Pset_SlabCommon") or {}).get("PitchAngle")
        if pa:
            f["tilt_deg"] = round(max(f.get("tilt_deg", 0), float(pa)), 1)
    except Exception:
        pass
    return f


def storey_heights(ifc, scale):
    st = sorted({round(float(s.Elevation or 0) * scale, 3) for s in ifc.by_type("IfcBuildingStorey")})
    out = {}
    for s in ifc.by_type("IfcBuildingStorey"):
        e = round(float(s.Elevation or 0) * scale, 3)
        nxt = [x for x in st if x > e + 0.5]
        out[s.id()] = round(min(nxt) - e, 2) if nxt else None
    return out


def build(session_dir):
    ifcs = glob.glob(os.path.join(session_dir, "original", "*.ifc")) + glob.glob(os.path.join(session_dir, "*.ifc"))
    if not ifcs:
        return {}
    ifc = ifcopenshell.open(ifcs[0])
    scale = uunit.calculate_unit_scale(ifc)
    z0 = 0.0      # отметка ±0.000 здания (в IFC часто абсолютные отметки — от уровня моря)
    try:
        b = ifc.by_type("IfcBuilding")[0]
        z0 = float(uplace.get_local_placement(b.ObjectPlacement)[2][3]) * scale
    except Exception:
        pass
    sh = storey_heights(ifc, scale)
    types = {}
    for cls in CLASSES:
        try:
            els = ifc.by_type(cls)
        except Exception:
            continue
        for el in els:
            key = el.ObjectType or el.Name or cls
            try:
                f = element_features(el, scale)
            except Exception as e:
                logger.debug(f"признаки {el.GlobalId}: {e}")
                continue
            st = uel.get_container(el)
            h = sh.get(st.id()) if st is not None and st.is_a("IfcBuildingStorey") else None
            t = types.setdefault(key, {"n": 0, "curved": 0, "curved_contour": 0, "radius_min": None,
                                       "tilt_max": 0.0, "tapered": 0, "z_min": None, "z_max": None,
                                       "floor_h_min": None, "floor_h_max": None, "ifc_class": el.is_a()})
            t["n"] += 1
            for k in ("curved", "curved_contour", "tapered"):
                if f.get(k):
                    t[k] += 1
            if f.get("radius"):
                t["radius_min"] = f["radius"] if t["radius_min"] is None else min(t["radius_min"], f["radius"])
            t["tilt_max"] = max(t["tilt_max"], f.get("tilt_deg", 0.0))
            z = f["z"] - z0
            t["z_min"] = z if t["z_min"] is None else min(t["z_min"], z)
            t["z_max"] = z if t["z_max"] is None else max(t["z_max"], z)
            if h:
                t["floor_h_min"] = h if t["floor_h_min"] is None else min(t["floor_h_min"], h)
                t["floor_h_max"] = h if t["floor_h_max"] is None else max(t["floor_h_max"], h)
    for t in types.values():
        for k in ("z_min", "z_max"):
            if t[k] is not None:
                t[k] = round(t[k], 2)
    json.dump(types, open(os.path.join(session_dir, "ifc_features.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return types


def load(session_dir, compute=True):
    fp = os.path.join(session_dir, "ifc_features.json")
    if os.path.isfile(fp):
        try:
            return json.load(open(fp, encoding="utf-8"))
        except Exception:
            pass
    return build(session_dir) if compute else {}


def facts_for(name, feats):
    """Признаки типа простыми словами — для qwen и для справки."""
    t = feats.get(name)
    if t is None:
        base = str(name).rsplit(":", 1)[0]
        t = feats.get(base)
    if not t:
        return {}
    out = {}
    if t["curved"]:
        out["криволинейный в плане"] = f"да, {t['curved']} из {t['n']} шт." + (f", радиус от {t['radius_min']} м" if t["radius_min"] else "")
    elif t["curved_contour"]:
        out["криволинейный контур"] = f"да, {t['curved_contour']} из {t['n']} шт." + (f", радиус от {t['radius_min']} м" if t["radius_min"] else "")
    else:
        out["криволинейный"] = "нет (прямолинейный)"
    out["наклон"] = f"{t['tilt_max']}°" if t["tilt_max"] else "нет (вертикальный/горизонтальный)"
    if t["tapered"]:
        out["переменное сечение"] = f"да, {t['tapered']} шт."
    if t["z_min"] is not None:
        out["отметка низа элементов, м (от 0.000)"] = f"от {t['z_min']} до {t['z_max']}"
    if t["floor_h_min"]:
        out["высота этажа, м"] = (f"{t['floor_h_min']}" if t["floor_h_min"] == t["floor_h_max"]
                                  else f"от {t['floor_h_min']} до {t['floor_h_max']}")
    return out


if __name__ == "__main__":
    sd = sys.argv[1] if len(sys.argv) > 1 else max(glob.glob("/app/outputs/*/"), key=os.path.getmtime)
    fp = os.path.join(sd, "ifc_features.json")
    if os.path.isfile(fp):
        os.remove(fp)
    types = build(sd)
    print(f"сессия {sd}: типов {len(types)}")
    cur = [k for k, t in types.items() if t["curved"] or t["curved_contour"]]
    tilt = [k for k, t in types.items() if t["tilt_max"]]
    tap = [k for k, t in types.items() if t["tapered"]]
    print(f"криволинейные: {len(cur)}  наклонные: {len(tilt)}  переменное сечение: {len(tap)}")
    for k in (cur + tilt + tap)[:12]:
        print("  ", k[:60], facts_for(k, types))
    zs = [t["z_min"] for t in types.values() if t["z_min"] is not None]
    hs = sorted({t["floor_h_min"] for t in types.values() if t["floor_h_min"]})
    print("отметки от", min(zs), "до", max(t["z_max"] for t in types.values() if t["z_max"] is not None), "м; высоты этажей:", hs[:8])
