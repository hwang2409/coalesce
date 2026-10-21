from __future__ import annotations

from pathlib import Path


def _result(check, status, **extra):
    return {"kind": check["kind"], "status": status, **extra}


def _step_metrics(paths):
    from build123d import GeomType, import_step
    data = []
    for path in paths:
        try:
            shape = import_step(path)
            solids = list(shape.solids())
            box = shape.bounding_box()
            cylinders = []
            for face in shape.faces():
                if face.geom_type is GeomType.CYLINDER:
                    # A cylindrical face's center is on its surface, not its axis.
                    face_box = face.bounding_box()
                    cylinders.append([(face_box.min.X + face_box.max.X) / 2,
                                      (face_box.min.Y + face_box.max.Y) / 2, face.radius])
            data.append({"path": str(path), "valid": bool(solids), "solid_count": len(solids),
                         "bbox": [box.max.X-box.min.X, box.max.Y-box.min.Y, box.max.Z-box.min.Z],
                         "volume": shape.volume, "cylinders": cylinders})
        except Exception as error:  # noqa: BLE001 - a failed measurement is unknown
            data.append({"path": str(path), "valid": False, "error": str(error)})
    return data


def check_task(task: dict, document: Path, steps: Path) -> list[dict]:
    output = []
    step_paths = sorted(steps.rglob("*.step")) if steps.exists() else []
    metrics = None
    for check in task["checks"]:
        kind = check["kind"]
        try:
            if kind == "step_validity":
                metrics = metrics or _step_metrics(step_paths)
                output.append(_result(check, "pass" if metrics and all(x["valid"] for x in metrics) else "fail", evidence=metrics)); continue
            if kind == "bbox":
                metrics = metrics or _step_metrics(step_paths)
                if not metrics: output.append(_result(check, "unknown")); continue
                actual = metrics[0]["bbox"]; expected = check["size"]; tol = check.get("tolerance", .1)
                output.append(_result(check, "pass" if all(abs(a-e)<=tol for a,e in zip(actual, expected)) else "fail", actual=actual)); continue
            if kind == "no_overlapping_parts":
                from build123d import import_step
                shapes = [import_step(path) for path in step_paths]
                if len(shapes) < 2: output.append(_result(check, "unknown")); continue
                overlap = any(a.intersect(b).volume > 1e-7 for i, a in enumerate(shapes) for b in shapes[i+1:])
                output.append(_result(check, "fail" if overlap else "pass")); continue
            if kind == "hole_positions":
                metrics = metrics or _step_metrics(step_paths)
                points = [(item[0], item[1]) for metric in metrics for item in metric["cylinders"] if item[2] <= 2]
                expected = check["positions"]; tol = check.get("tolerance", .1)
                ok = len(points) >= len(expected) and all(any(abs(x-ex) <= tol and abs(y-ey) <= tol for x,y in points) for ex,ey in expected)
                output.append(_result(check, "pass" if ok else "fail", actual=points)); continue
            from coalesce.graph import PcbBase
            from coalesce.service import DocumentService
            service = DocumentService(document, warmup=False); service._ensure_geometry()
            graph = service._document.graph
            base = next((node.id for node in graph.ordered() if isinstance(node, PcbBase)), None)
            if not base: output.append(_result(check, "unknown")); continue
            inspected = service.query({"select": {"kind": "enclosure", "base": base}})
            if inspected.get("ok") is False: output.append(_result(check, "unknown", reason=inspected)); continue
            if kind == "wall_thickness": value = inspected["base"]["minimumWallThickness"].get("value")
            elif kind == "board_clearance": value = min(x["value"] for x in inspected["cavity"]["boardClearance"].values())
            elif kind == "port_opening_clears_connector": value = not any(p.get("blockedByLid") for p in inspected.get("ports", []))
            elif kind == "lid_fit_gap": value = inspected.get("lids", [{}])[0].get("fitGap", {}).get("value")
            elif kind == "part_count_role":
                value = len(inspected.get("terminalParts", [])); output.append(_result(check, "pass" if value >= check["minimum"] else "fail", actual=value)); continue
            elif kind == "references_resolve": value = not inspected.get("warnings")
            else: output.append(_result(check, "unknown")); continue
            if value is None or (isinstance(value, dict) and value.get("status") == "unknown"):
                output.append(_result(check, "unknown")); continue
            ok = bool(value) if kind in {"port_opening_clears_connector", "references_resolve"} else check.get("minimum", float("-inf")) <= value <= check.get("maximum", float("inf"))
            output.append(_result(check, "pass" if ok else "fail", actual=value))
        except Exception as error:  # noqa: BLE001 - a failed check is unknown
            output.append(_result(check, "unknown", reason=str(error)))
    return output
