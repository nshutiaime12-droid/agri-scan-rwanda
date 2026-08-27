
_CELL_GEOM_PATH = os.path.join(os.path.dirname(__file__), "rwanda_cells.json")
@lru_cache(maxsize=1)
def _load_cell_geometries() -> dict:
    try:
        with open(_CELL_GEOM_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
"""
Agri-Scan Rwanda — v3.0 (Z-Score Framework)
Changes from v2:
  - Percentage-based stress thresholds (>15% = High Alert, 5-15% = Moderate, <5% = Stable)
  - Statistical Z-Score Anomaly Framework (< -1.0 std dev from historical baseline)
  - Dynamic total cropland area computed per district via ESA WorldCover
  - KPI shows both km² and % of total cropland
  - Chart y-axis fixed to show only relevant NDVI range (no wasted space)
  - All advisory text uses percentage thresholds
"""

import os
import json
import logging
from functools import lru_cache
from datetime import date, timedelta

import ee
import folium
import pandas as pd
import streamlit as st
from folium.plugins import Draw
from streamlit_folium import st_folium
from supabase import create_client, Client

# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Agri-Scan Rwanda",
    page_icon="🌾",
    layout="wide",
    initial_sidebar_state="expanded",
)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://neskcsbhdzrtdkintqgg.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_XUoeoM27WFCugu3pRLx4Wg_WTled...")
EE_PROJECT   = os.environ.get("EE_PROJECT",   "proven-record-503516-h2")

DISTRICT_SECTORS: dict[str, list[str]] = {
    "Rubavu":    ["Gisenyi", "Rugerero", "Rubavu", "Kanama", "Nyamyumba", "Cyanzarwe", "Bugeshi"],
    "Rutsiro": ["Gihango", "Kigeyo", "Kivumu", "Murunda", "Musasa", "Ruhango", "Boneza"],
    "Nyabihu": ["Bigogwe", "Jenda", "Mukamira", "Rambura", "Rugera", "Shyira"],
    "Musanze": ["Muhoza", "Cyuve", "Gacaca", "Gashaki", "Gataraga", "Kinigi", "Shingiro"],
    "Kigali":    ["Nyarugenge", "Kicukiro", "Gasabo"],
    "Kayonza":   ["Mukarange", "Ruramira", "Nyamirama", "Kabare", "Gahini", "Murama", "Rukara"],
    "Nyagatare": ["Nyagatare", "Tabagwe", "Karama", "Matimba", "Rwempasha", "Musheri", "Mimuli"],
    "Kirehe":    ["Kirehe", "Gahara", "Nyamugari", "Mahama", "Mpanga", "Musaza", "Kigarama"]
}

# Real sector boundaries loaded from geoBoundaries ADM3 GeoJSON
# Source: geoBoundaries / HDX (CC-BY 4.0) — 416 Rwanda sectors
_SECTOR_GEOM_PATH = os.path.join(os.path.dirname(__file__), "rwanda_sectors.json")

@lru_cache(maxsize=1)
def _load_sector_geometries() -> dict:
    try:
        with open(_SECTOR_GEOM_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

# Z-Score Thresholds (FAO / WMO Standard Framework)
SEVERE_STRESS_ZSCORE = -1.0  # Standard deviations below historical mean
SOC_CONVERSION_FACTOR  = 10.0
MAX_CLOUD_PCT          = 30
WET_MONTHS             = {3, 4, 5, 10, 11, 12}
WORLDCOVER_CROPLAND    = 40

# Alert thresholds as % of total cropland
HIGH_ALERT_PCT     = 15.0
MODERATE_ALERT_PCT =  5.0

# ─────────────────────────────────────────────────────────────────────────────
# CACHED RESOURCES
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def get_supabase() -> Client | None:
    try:
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as exc:
        logger.error("Supabase init failed: %s", exc)
        return None


@st.cache_resource(show_spinner=False)
def init_earth_engine() -> bool:
    try:
        if "gcp_service_account" in st.secrets:
            creds_dict = dict(st.secrets["gcp_service_account"])
            credentials = ee.ServiceAccountCredentials(
                creds_dict["client_email"], key_data=json.dumps(creds_dict)
            )
            ee.Initialize(credentials, project=EE_PROJECT)
        elif "EE_PRIVATE_KEY_JSON" in st.secrets:
            key_json = st.secrets.get("EE_PRIVATE_KEY_JSON")
            key_data = json.loads(key_json)
            credentials = ee.ServiceAccountCredentials(
                key_data["client_email"], key_data=key_data
            )
            ee.Initialize(credentials, project=EE_PROJECT)
        else:
            ee.Initialize(project=EE_PROJECT)
        _ = ee.Number(1).add(1).getInfo()
        return True
    except Exception as exc:
        logger.error("Earth Engine init failed: %s", exc)
        return False

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def add_ee_layer(fmap: folium.Map, ee_image: ee.Image, vis_params: dict, name: str) -> None:
    map_id_dict = ee.Image(ee_image).getMapId(vis_params)
    folium.TileLayer(
        tiles=map_id_dict["tile_fetcher"].url_format,
        attr="Google Earth Engine",
        name=name,
        overlay=True,
        control=True,
    ).add_to(fmap)


def build_roi(district: str, sector: str) -> tuple[ee.Geometry, bool]:
    gaul = ee.FeatureCollection("FAO/GAUL/2015/level2")
    district_geom = gaul.filter(
        ee.Filter.And(
            ee.Filter.eq("ADM0_NAME", "Rwanda"),
            ee.Filter.eq("ADM2_NAME", district),
        )
    ).geometry()

    if sector and sector != "All Sectors":
        sector_geoms = _load_sector_geometries()
        if sector in sector_geoms:
            # Use real geoBoundaries sector geometry, clipped to district
            candidate = ee.Geometry(sector_geoms[sector])
        else:
            # Fallback: 5 km buffer around district centroid
            candidate = district_geom.centroid(maxError=1).buffer(5_000)
        return district_geom.intersection(candidate, maxError=1), True

    return district_geom, False


def _is_wet_season(start: date, end: date) -> bool:
    mid_month = ((start.month + end.month) // 2) or start.month
    return mid_month in WET_MONTHS


def _get_cropland_mask(roi: ee.Geometry) -> ee.Image:
    return (
        ee.Image("ESA/WorldCover/v200/2021")
        .select("Map")
        .clip(roi)
        .eq(WORLDCOVER_CROPLAND)
    )


def _add_ndvi(img: ee.Image) -> ee.Image:
    ndvi = img.normalizedDifference(["B8", "B4"]).rename("NDVI")
    return img.addBands(ndvi)

# ─────────────────────────────────────────────────────────────────────────────
# EE COMPUTATIONS (Z-SCORE FRAMEWORK)
# ─────────────────────────────────────────────────────────────────────────────
def compute_ndvi_anomaly(
    roi: ee.Geometry,
    start: date,
    end: date,
) -> tuple[ee.Image | None, float, float]:
    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(roi)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", MAX_CLOUD_PCT))
    )

    if s2.size().getInfo() == 0:
        return None, 0.0, 0.0

    water_mask    = s2.first().normalizedDifference(["B3", "B8"]).lte(0.0)
    crop_mask     = _get_cropland_mask(roi)
    combined_mask = water_mask.And(crop_mask)

    s2_ndvi = s2.map(_add_ndvi)
    wet     = _is_wet_season(start, end)

    if wet:
        baseline_ic = s2_ndvi.select("NDVI").filterDate("2019-01-01", "2024-12-31").filter(
            ee.Filter.Or(
                ee.Filter.calendarRange(3, 5, "month"),
                ee.Filter.calendarRange(10, 12, "month"),
            )
        )
    else:
        baseline_ic = s2_ndvi.select("NDVI").filterDate("2019-01-01", "2024-12-31").filter(
            ee.Filter.Or(
                ee.Filter.calendarRange(1, 2, "month"),
                ee.Filter.calendarRange(6, 9, "month"),
            )
        )

    # Compute baseline mean and standard deviation for Z-Score framework
    masked_baseline = baseline_ic.map(lambda img: img.updateMask(combined_mask))
    mean_img = masked_baseline.mean()
    std_img  = masked_baseline.reduce(ee.Reducer.stdDev()).rename("NDVI").rename("NDVI")

    baseline_stats = mean_img.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=roi, scale=100, maxPixels=1e8,
    )
    baseline_mean = float(
        ee.Number(baseline_stats.get("NDVI", ee.Number(0))).getInfo() or 0.0
    )

    current = s2_ndvi.select("NDVI").filterDate(str(start), str(end)).median().updateMask(combined_mask)

    # Z-Score Image: (Current - Mean) / StdDev (clipping std deviation minimum to avoid division by zero)
    z_score = current.subtract(mean_img).divide(std_img.max(0.01)).rename('z_score').clip(roi)

    stress_mask = z_score.lt(SEVERE_STRESS_ZSCORE)
    area_dict   = (
        ee.Image.pixelArea().divide(1e6)
        .updateMask(stress_mask)
        .reduceRegion(reducer=ee.Reducer.sum(), geometry=roi, scale=20, maxPixels=1e9)
    )
    stress_km2 = round(
        float(ee.Number(area_dict.get("area", ee.Number(0))).getInfo() or 0.0), 1
    )

    return z_score, stress_km2, baseline_mean


def compute_rain_anomaly(roi: ee.Geometry, start: date, end: date) -> ee.Image:
    chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterBounds(roi)
    current_sum     = chirps.filterDate(str(start), str(end)).select("precipitation").sum()
    historical_avg = (
        chirps.filterDate("2020-01-01", "2024-12-31").select("precipitation").sum().divide(5)
    )
    return current_sum.subtract(historical_avg).divide(historical_avg).multiply(100).clip(roi)


def get_soc_layer(roi: ee.Geometry) -> ee.Image:
    return (
        ee.Image("projects/soilgrids-isric/soc_mean")
        .select("soc_0-5cm_mean")
        .divide(SOC_CONVERSION_FACTOR)
        .clip(roi)
    )


@st.cache_data(show_spinner=False, ttl=86400)
def get_total_cropland_km2(district: str, sector: str = "All Sectors") -> float:
    """
    Total ESA WorldCover cropland area for the selected ROI in km².
    When a sector is selected, computes over the sector — not the whole district.
    Cached 24 h.
    """
    roi, _ = build_roi(district, sector)
    crop_mask = _get_cropland_mask(roi)
    area_dict = (
        ee.Image.pixelArea().divide(1e6)
        .updateMask(crop_mask)
        .reduceRegion(reducer=ee.Reducer.sum(), geometry=roi, scale=100, maxPixels=1e9)
    )
    return round(
        float(ee.Number(area_dict.get("area", ee.Number(0))).getInfo() or 0.0), 1
    )


@st.cache_data(show_spinner=False, ttl=3600)
def get_ndvi_timeseries(
    district: str, sector: str, start_str: str, end_str: str
) -> tuple[pd.DataFrame, float]:
    roi, _ = build_roi(district, sector)
    start  = date.fromisoformat(start_str)
    end    = date.fromisoformat(end_str)
    wet    = _is_wet_season(start, end)

    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(roi)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", MAX_CLOUD_PCT))
        .map(_add_ndvi)
    )

    ic = s2.filterDate(start_str, end_str)
    if ic.size().getInfo() == 0:
        return pd.DataFrame(), 0.0

    crop_mask = _get_cropland_mask(roi)

    def extract_mean(img: ee.Image) -> ee.Feature:
        masked   = img.select("NDVI").updateMask(crop_mask)
        mean_val = masked.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=roi, scale=100, maxPixels=1e8,
        )
        return ee.Feature(None, {"date": img.date().format("YYYY-MM-dd"), "NDVI": mean_val.get("NDVI")})

    fc      = ic.map(extract_mean).filter(ee.Filter.notNull(["NDVI"]))
    records = [f["properties"] for f in fc.getInfo()["features"]]
    if not records:
        return pd.DataFrame(), 0.0

    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    df = df.groupby("date")["NDVI"].mean().reset_index().sort_values("date").set_index("date")

    if wet:
        baseline_ic = s2.filterDate("2019-01-01", "2024-12-31").filter(
            ee.Filter.Or(
                ee.Filter.calendarRange(3, 5, "month"),
                ee.Filter.calendarRange(10, 12, "month"),
            )
        )
    else:
        baseline_ic = s2.filterDate("2019-01-01", "2024-12-31").filter(
            ee.Filter.Or(
                ee.Filter.calendarRange(1, 2, "month"),
                ee.Filter.calendarRange(6, 9, "month"),
            )
        )

    baseline_stats = (
        baseline_ic.select("NDVI").median().updateMask(crop_mask)
        .reduceRegion(reducer=ee.Reducer.mean(), geometry=roi, scale=100, maxPixels=1e8)
    )
    baseline_mean = float(
        ee.Number(baseline_stats.get("NDVI", ee.Number(0))).getInfo() or 0.0
    )

    df["Seasonal Baseline"] = baseline_mean
    return df, baseline_mean

# ─────────────────────────────────────────────────────────────────────────────
# MAP LEGEND
# ─────────────────────────────────────────────────────────────────────────────
NDVI_LEGEND_HTML = """
<div style="
    position: fixed; top: 80px; right: 10px;
    width: 230px; padding: 12px; border-radius: 6px;
    border: 2px solid #555; background: rgba(255,255,255,0.95);
    font-size: 12px; font-family: sans-serif; z-index: 9999;
    box-shadow: 2px 2px 6px rgba(0,0,0,0.3);">
  <b>Vegetation Z-Score Anomaly</b>
  <span style="color:#555;font-size:10px;"> (cropland only)</span><br><br>
  <span style="display:inline-block;width:14px;height:14px;background:#d7191c;margin-right:6px;border-radius:2px;"></span>Severe Stress (&lt;&minus;1.0 &sigma;)<br>
  <span style="display:inline-block;width:14px;height:14px;background:#fdae61;margin-right:6px;border-radius:2px;"></span>Mild Stress (&minus;1.0 to &minus;0.5 &sigma;)<br>
  <span style="display:inline-block;width:14px;height:14px;background:#ffffbf;margin-right:6px;border-radius:2px;border:1px solid #ccc;"></span>Normal (&plusmn;0.5 &sigma;)<br>
  <span style="display:inline-block;width:14px;height:14px;background:#abd9e9;margin-right:6px;border-radius:2px;"></span>Good Vigor (+0.5 to +1.0 &sigma;)<br>
  <span style="display:inline-block;width:14px;height:14px;background:#2c7bb6;margin-right:6px;border-radius:2px;"></span>High Vigor (&gt;+1.0 &sigma;)<br>
  <hr style="margin:6px 0;border-color:#ddd;">
  <span style="font-size:10px;color:#666;">Sentinel-2 Z-Score · ESA WorldCover</span>
</div>
"""

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:

    st.title("🌾 Agri-Scan Rwanda: Crop Health & Climate Intelligence")
    st.markdown(
        "Real-time Earth Observation & Food Security Monitoring — "
        "Sentinel-2 Z-Score · CHIRPS · SoilGrids · ESA WorldCover"
    )

    ee_ok = init_earth_engine()
    if not ee_ok:
        st.error(
            "⚠️ Earth Engine could not be initialised. "
            "Ensure EE_PROJECT is set, then run `earthengine authenticate`."
        )
        st.stop()

    supabase = get_supabase()

    # ── Sidebar ──────────────────────────────────────────────────────────────
    st.sidebar.header("📍 Location & Search")
    district = st.sidebar.selectbox("District", list(DISTRICT_SECTORS.keys()))
    sector   = st.sidebar.selectbox(
        "Sector (Umurenge)", ["All Sectors"] + DISTRICT_SECTORS[district]
    )
    # Dynamic cell selection from geoBoundaries ADM4
cell_geoms = _load_cell_geometries()
cell_options = ["All Cells"] + sorted(list(cell_geoms.keys()))
selected_cell = st.sidebar.selectbox("Cell (Akagari)", cell_options)
cell = None if selected_cell == "All Cells" else selected_cell
    village = st.sidebar.text_input("Village (Umudugudu)", placeholder="e.g., Ubumwe")

    st.sidebar.markdown("---")
    upi_input = st.sidebar.text_input("🆔 UPI (Parcel ID)", placeholder="e.g., 3/03/04/01/123")

    st.sidebar.markdown("---")
    st.sidebar.header("📅 Date Range")
    default_end   = date.today() - timedelta(days=1)
    default_start = default_end.replace(month=1, day=1)
    start_date = st.sidebar.date_input("Start", default_start)
    end_date   = st.sidebar.date_input("End",   default_end)

    if start_date >= end_date:
        st.sidebar.error("Start date must be before end date.")
        st.stop()

    st.sidebar.markdown("---")
    st.sidebar.header("🛰️ Map Layers")
    show_ndvi = st.sidebar.checkbox("Crop Vigor Z-Score Anomaly (Sentinel-2)", value=True)
    show_rain = st.sidebar.checkbox("Rainfall Anomaly % (CHIRPS)",    value=False)
    show_soc  = st.sidebar.checkbox("Soil Organic Carbon (SoilGrids)", value=False)

    # ── ROI ──────────────────────────────────────────────────────────────────
    with st.spinner("Loading region geometry…"):
        roi, is_sector_mode = build_roi(district, sector)

    location_label = sector if is_sector_mode else district
    wet            = _is_wet_season(start_date, end_date)
    season_label   = "🌧️ Wet Season" if wet else "☀️ Dry Season"

    # ── EE Computations ──────────────────────────────────────────────────────
    with st.spinner("Computing cropland Z-Score anomaly…"):
        ndvi_anomaly, stress_km2, baseline_mean = compute_ndvi_anomaly(roi, start_date, end_date)

    with st.spinner("Computing total cropland area…"):
        total_cropland_km2 = get_total_cropland_km2(district, sector)

    stress_pct   = round((stress_km2 / total_cropland_km2 * 100), 1) if total_cropland_km2 > 0 else 0.0
    rain_anomaly = compute_rain_anomaly(roi, start_date, end_date)
    soc_layer    = get_soc_layer(roi)

    # ── UPI Lookup ────────────────────────────────────────────────────────────
    map_center  = [-1.9403, 29.8739]
    zoom_level  = 11
    parcel_data = None
    upi         = upi_input.strip()

    if upi and supabase:
        try:
            resp = supabase.table("parcels").select("*").eq("upi", upi).execute()
            if resp.data:
                parcel_data = resp.data[0]
                map_center  = [parcel_data["latitude"], parcel_data["longitude"]]
                zoom_level  = 18
                st.sidebar.success(
                    f"🎯 {parcel_data.get('village','')} ({parcel_data.get('sector','')})"
                )
            else:
                st.sidebar.warning(f"UPI '{upi}' not found.")
        except Exception as exc:
            st.sidebar.error(f"Supabase: {exc}")

    if parcel_data is None:
        try:
            coords     = roi.centroid(maxError=1).coordinates().getInfo()
            map_center = [coords[1], coords[0]]
            zoom_level = 14 if is_sector_mode else 11
        except Exception:
            pass

    # ── KPI Row ───────────────────────────────────────────────────────────────
    if stress_pct > HIGH_ALERT_PCT:
        alert_label = "🔴 High Alert"
        alert_color = "inverse"
    elif stress_pct > MODERATE_ALERT_PCT:
        alert_label = "🟡 Moderate"
        alert_color = "off"
    else:
        alert_label = "🟢 Normal"
        alert_color = "normal"

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Selected Area",        location_label)
    k2.metric("Season",                season_label)
    k3.metric(
        "Cropland Stress",
        f"{stress_km2} km²  ({stress_pct}%)",
        delta=alert_label,
        delta_color=alert_color,
    )
    k4.metric(
        "Total Cropland Area",
        f"{total_cropland_km2} km²",
        delta=f"Baseline NDVI {round(baseline_mean, 3)}" if baseline_mean else "—",
    )

    st.markdown("---")

    # ── Map ───────────────────────────────────────────────────────────────────
    fmap = folium.Map(location=map_center, zoom_start=zoom_level, tiles="OpenStreetMap")

    folium.TileLayer(
        tiles="https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
        attr="Google",
        name="Google Satellite Hybrid",
    ).add_to(fmap)

    try:
        folium.GeoJson(
            roi.getInfo(),
            name=f"{location_label} Boundary",
            style_function=lambda _: {
                "fillColor":   "#ffd700" if is_sector_mode else "#2c7bb6",
                "color":       "#ff8c00" if is_sector_mode else "#1d4ed8",
                "weight":      3,
                "fillOpacity": 0.15,
            },
            tooltip=f"{'Sector' if is_sector_mode else 'District'}: {location_label}",
        ).add_to(fmap)
    except Exception as exc:
        logger.warning("ROI boundary error: %s", exc)

    if parcel_data:
        folium.Marker(
            location=map_center,
            popup=f"<b>UPI:</b> {upi}<br><b>Village:</b> {parcel_data.get('village','—')}",
            icon=folium.Icon(color="red", icon="info-sign"),
        ).add_to(fmap)

    if show_ndvi and ndvi_anomaly is not None:
        add_ee_layer(
            fmap, ndvi_anomaly,
            {"min": -2.0, "max": 2.0,
             "palette": ["d7191c", "fdae61", "ffffbf", "abd9e9", "2c7bb6"]},
            "Crop Vigor Z-Score Anomaly",
        )

    if show_rain:
        add_ee_layer(
            fmap, rain_anomaly,
            {"min": -50, "max": 50,
             "palette": ["a6611a", "dfc27d", "f5f5f5", "80cdc1", "018571"]},
            "Rainfall Anomaly %",
        )

    if show_soc:
        add_ee_layer(
            fmap, soc_layer,
            {"min": 5, "max": 60,
             "palette": ["f5f5f5", "c7e9c0", "74c476", "238b45", "00441b"]},
            "Soil Organic Carbon (g/kg)",
        )

    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.get_root().html.add_child(folium.Element(NDVI_LEGEND_HTML))

    Draw(
        export=False,
        position="topleft",
        draw_options={
            "polyline": False, "rectangle": True, "polygon": True,
            "circle": False, "marker": True, "circlemarker": False,
        },
    ).add_to(fmap)

    map_data = st_folium(fmap, use_container_width=True, height=540, key="farm_map")

    # ── Location Details ──────────────────────────────────────────────────────
    st.markdown("### 📍 Farm Location Details")
    lc1, lc2, lc3, lc4 = st.columns(4)
    lc1.metric("Sector (Umurenge)",   location_label)
    lc2.metric("Cell (Akagari)",      cell    or "Not specified")
    lc3.metric("Village (Umudugudu)", village or "Not specified")
    lc4.metric("UPI Parcel ID",       upi     or "Draw on map")

    # ── Plot analytics ────────────────────────────────────────────────────────
    plot_z_val: float | None = None

    if map_data and map_data.get("last_active_drawing"):
        st.sidebar.success("✅ Custom farm boundary active")
        drawing = map_data["last_active_drawing"]
        try:
            geom_type = drawing["geometry"]["type"]
            coords    = drawing["geometry"]["coordinates"]

            if geom_type == "Polygon":
                farm_geom = ee.Geometry.Polygon(coords)
            elif geom_type == "Point":
                farm_geom = ee.Geometry.Point(coords).buffer(50)
            else:
                raise ValueError(f"Unsupported geometry: {geom_type}")

            if ndvi_anomaly is None:
                st.warning("No cloud-free imagery for the selected period.")
            else:
                plot_area_ha  = farm_geom.area(maxError=1).divide(10_000).getInfo()
                plot_stats    = ndvi_anomaly.reduceRegion(
                    reducer=ee.Reducer.mean(), geometry=farm_geom,
                    scale=10, maxPixels=1e8,
                ).getInfo()
                plot_z_val = plot_stats.get("z_score")

                st.markdown("### 🎯 Specific Plot Analytics")
                pc1, pc2, pc3 = st.columns(3)
                pc1.metric("Plot Area", f"{round(plot_area_ha, 2)} ha")
                pc2.metric(
                    "Z-Score Anomaly",
                    f"{round(plot_z_val, 2)} σ" if plot_z_val is not None else "N/A",
                )
                if plot_z_val is not None:
                    if plot_z_val < SEVERE_STRESS_ZSCORE:
                        pc3.metric("Condition", "Severe Stress 🔴", delta="Action needed", delta_color="inverse")
                    elif plot_z_val < -0.5:
                        pc3.metric("Condition", "Mild Stress 🟡",   delta="Monitor",      delta_color="off")
                    else:
                        pc3.metric("Condition", "Healthy 🟢",        delta="Good vigor")
        except Exception as exc:
            st.warning(f"Plot metrics error: {exc}")

    st.markdown("---")

    # ── Time-Series + Advisory ────────────────────────────────────────────────
    chart_col, advisory_col = st.columns([2, 1])

    with chart_col:
        st.subheader(f"📈 Cropland NDVI — {location_label}  ({season_label})")
        try:
            with st.spinner("Fetching time-series…"):
                ts_df, ts_baseline = get_ndvi_timeseries(
                    district, sector, str(start_date), str(end_date)
                )

            if ts_df.empty:
                st.info("No cloud-free observations in the selected range.")
            else:
                st.line_chart(
                    ts_df[["NDVI", "Seasonal Baseline"]],
                    y_label="NDVI",
                )
                st.caption(
                    f"Cropland NDVI (ESA WorldCover masked) · Sentinel-2 SR · "
                    f"Baseline = {season_label} median 2022–2024 ({round(ts_baseline, 3)}). "
                    f"Gaps = cloudy periods."
                )
        except Exception as exc:
            st.warning(f"Time-series error: {exc}")

    with advisory_col:
        st.subheader("💡 Agronomic Advisory")
        if stress_pct > HIGH_ALERT_PCT:
            st.warning(
                f"**🔴 High Alert:** {stress_km2} km² "
                f"(**{stress_pct}% of cropland**) under severe stress."
            )
            st.markdown(
                "- **Field inspection:** Dispatch extension agents.\n"
                "- **Irrigation:** Prioritise deficit zones.\n"
                "- **Fertiliser:** Check nitrogen in stressed parcels.\n"
                "- **Context:** FAO Z-score threshold triggered (< -1.0σ)."
            )
        elif stress_pct > MODERATE_ALERT_PCT:
            st.info(
                f"**🟡 Moderate Alert:** {stress_km2} km² "
                f"({stress_pct}% of cropland) showing stress."
            )
            st.markdown(
                "- **Spot checks:** Visit highlighted cells within the week.\n"
                "- **Cross-check:** Compare with CHIRPS rainfall layer."
            )
        else:
            st.success(
                f"**🟢 Stable:** {stress_pct}% cropland stressed — "
                "within normal seasonal variation."
            )
            st.markdown(
                "- **Routine monitoring:** Continue bi-weekly tracking.\n"
                "- **Soil health:** Maintain organic carbon practices."
            )

    # ── Farmer Summary ────────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("🌾 Farmer-Friendly Summary (Amakuru y'Ubuhinzi)")
    fc1, fc2 = st.columns(2)

    with fc1:
        st.markdown("#### 🚦 Simple Field Status")
        if plot_z_val is not None:
            if plot_z_val < SEVERE_STRESS_ZSCORE:
                st.error("🔴 **Urgent:** Heavy crop stress. Check moisture or pests immediately.")
            elif plot_z_val < -0.5:
                st.warning("🟡 **Caution:** Growth slowing. Check soil nutrients or water.")
            else:
                st.success("🟢 **Thriving:** Crops healthy. Keep up regular management!")
        else:
            st.info("💡 Draw your farm boundary on the map for a personalised health check.")

    with fc2:
        st.markdown("#### 📱 Extension Alert")
        sms = f"Agri-Scan [{location_label}] {season_label}: "
        if stress_pct > HIGH_ALERT_PCT:
            sms += (
                f"🔴 {stress_pct}% of cropland ({stress_km2} km²) under severe Z-score stress. "
                "Contact your Umurenge agronomist immediately."
            )
        elif stress_pct > MODERATE_ALERT_PCT:
            sms += f"🟡 {stress_pct}% of cropland showing stress. Monitor closely."
        else:
            sms += "✅ Conditions stable. Routine monitoring advised."
        st.info(sms)

    # ── Exports ───────────────────────────────────────────────────────────────
    st.sidebar.markdown("---")
    st.sidebar.header("📥 Export")

    try:
        st.sidebar.download_button(
            "⬇ Boundary (GeoJSON)",
            data=json.dumps(roi.getInfo()),
            file_name=f"boundary_{location_label.lower().replace(' ','_')}.geojson",
            mime="application/json",
        )
    except Exception as exc:
        st.sidebar.warning(f"GeoJSON unavailable: {exc}")

    summary = pd.DataFrame([{
        "District":                 district,
        "Sector":                   sector,
        "Season":                   "Wet" if wet else "Dry",
        "Start Date":               start_date,
        "End Date":                 end_date,
        "Total Cropland km²":       total_cropland_km2,
        "Stressed Cropland km²":    stress_km2,
        "Stressed Cropland %":      stress_pct,
        "Alert Level":              alert_label,
        "Baseline NDVI":            round(baseline_mean, 3),
    }])
    st.sidebar.download_button(
        "⬇ Summary (CSV)",
        data=summary.to_csv(index=False).encode("utf-8"),
        file_name="agri_scan_summary.csv",
        mime="text/csv",
    )

    st.sidebar.markdown("---")
    st.sidebar.caption(
        "Agri-Scan Rwanda v3.0 (Z-Score) · Nshuti Aimé · IUSS Pavia\n\n"
        "Sentinel-2 Z-Score · UCSB CHIRPS · ISRIC SoilGrids 2.0 · ESA WorldCover 10m"
    )


if __name__ == "__main__":
    main()
