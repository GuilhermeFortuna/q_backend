"""Client publishing library for Matplotlib and Plotly figures."""

from __future__ import annotations

import base64
import json
import struct
import xml.etree.ElementTree as ET
from typing import Any, Optional

from q_backend.research.provenance import collect_figure_provenance, get_script_path
from q_backend.research.transport import post_research_json


def _is_matplotlib_figure(obj: Any) -> bool:
    if obj is None:
        return False
    return any(cls.__module__.startswith("matplotlib") and cls.__name__ == "Figure" for cls in type(obj).__mro__)


def _is_plotly_figure(obj: Any) -> bool:
    if obj is None:
        return False
    return any(cls.__module__.startswith("plotly") and "Figure" in cls.__name__ for cls in type(obj).__mro__)


def _extract_title(fig: Any, target_fig: Any, kind: str) -> Optional[str]:
    if kind == "matplotlib":
        if hasattr(target_fig, "get_suptitle"):
            st = target_fig.get_suptitle()
            if st and st.strip():
                return st.strip()
        st_obj = getattr(target_fig, "_suptitle", None)
        if st_obj is not None:
            text = st_obj.get_text()
            if text and text.strip():
                return text.strip()
        if hasattr(fig, "get_title"):
            at = fig.get_title()
            if at and at.strip():
                return at.strip()
        axes = getattr(target_fig, "axes", [])
        if len(axes) == 1:
            at = axes[0].get_title()
            if at and at.strip():
                return at.strip()
        return None

    if kind == "plotly":
        layout = getattr(target_fig, "layout", None)
        if layout is not None:
            t = getattr(layout, "title", None)
            if t is not None:
                if hasattr(t, "text") and t.text and t.text.strip():
                    return t.text.strip()
                if isinstance(t, str) and t.strip():
                    return t.strip()
        return None

    return None


def publish_figure(
    fig: Any,
    *,
    title: Optional[str] = None,
    collection: Optional[str] = None,
    notes: Optional[str] = None,
    replace: bool = True,
    image_format: str = "png",
    dpi: int = 200,
    api_url: Optional[str] = None,
    client: Any = None,
) -> str:
    """Publish a Matplotlib or Plotly figure to the Research stack.

    Returns the figure id.
    """
    # 1. Figure recognition without importing either library
    if _is_matplotlib_figure(fig):
        target_fig = fig
        kind = "matplotlib"
    elif hasattr(fig, "figure") and _is_matplotlib_figure(getattr(fig, "figure", None)):
        target_fig = fig.figure
        kind = "matplotlib"
    elif _is_plotly_figure(fig):
        target_fig = fig
        kind = "plotly"
    else:
        raise TypeError(
            f"Unsupported figure type {type(fig)!r}. Accepted types: matplotlib.figure.Figure, "
            f"an object with a .figure attribute (e.g. Axes), or plotly.graph_objects.Figure."
        )

    # 2. Title default resolution
    if title is not None:
        title = title.strip()
        if not title:
            raise ValueError("Figure title is required; pass title= or set a title on the figure.")
    else:
        title = _extract_title(fig, target_fig, kind)
        if not title:
            raise ValueError("Figure title is required; pass title= or set a title on the figure.")

    # 3. Collection default resolution
    if collection is None:
        script_path = get_script_path()
        collection = script_path.stem if script_path is not None else "interactive"

    # 4. Render or serialize content
    if kind == "matplotlib":
        fmt = image_format.lower()
        if fmt not in ("png", "svg"):
            raise ValueError(f"image_format must be 'png' or 'svg', got {image_format!r}")

        import io

        buf = io.BytesIO()
        facecolor = target_fig.get_facecolor() if hasattr(target_fig, "get_facecolor") else "white"
        target_fig.savefig(buf, format=fmt, dpi=dpi, bbox_inches="tight", facecolor=facecolor)
        raw_bytes = buf.getvalue()

        if fmt == "png":
            media_type = "image/png"
            width, height = struct.unpack(">II", raw_bytes[16:24])
        else:
            media_type = "image/svg+xml"
            root = ET.fromstring(raw_bytes)
            vb = root.attrib.get("viewBox")
            if vb:
                parts = vb.split()
                width = max(1, int(round(float(parts[2]) * dpi / 72.0)))
                height = max(1, int(round(float(parts[3]) * dpi / 72.0)))
            else:
                w_attr = root.attrib.get("width", "100pt")
                h_attr = root.attrib.get("height", "100pt")
                w_pt = float(w_attr.replace("pt", "").replace("px", "").strip())
                h_pt = float(h_attr.replace("pt", "").replace("px", "").strip())
                width = max(1, int(round(w_pt * dpi / 72.0)))
                height = max(1, int(round(h_pt * dpi / 72.0)))

        content_payload = {
            "kind": "image",
            "media_type": media_type,
            "data": base64.b64encode(raw_bytes).decode("ascii"),
            "width": width,
            "height": height,
        }
    else:  # kind == "plotly"
        fig_json = target_fig.to_json()
        fig_dict = json.loads(fig_json)
        content_payload = {
            "kind": "plotly",
            "figure": fig_dict,
        }

    # 5. Provenance collection
    provenance = collect_figure_provenance()

    payload = {
        "title": title,
        "collection": collection,
        "notes": notes,
        "replace": replace,
        "provenance": provenance,
        "content": content_payload,
    }

    # 6. HTTP dispatch
    res_data = post_research_json(
        "/api/v1/research/figures",
        payload,
        noun="figure",
        endpoint="figures",
        api_url=api_url,
        client=client,
    )
    try:
        return str(res_data["figure_id"])
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Unexpected response format from figures endpoint: {res_data!r}") from exc
