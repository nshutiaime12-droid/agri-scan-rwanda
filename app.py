import os
import json
import logging
from functools import lru_cache
from datetime import date, timedelta

import ee
import folium
import pandas as pd
import streamlit as st
import africastalking
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
# CONSTANTS & PATHS
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

_SECTOR_GEOM_PATH = os.path.join(os.path.dirname(__file__), "rwanda_sectors.json")
_CELL_GEOM_PATH   = os.path.join(os.path.dirname(__file__), "rwanda_cells.json")

@lru_cache(maxsize=1)
def _load_sector_geometries() -> dict:
    try:
        with open(_SECTOR_GEOM_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

@lru_cache(maxsize=1)
def _load_cell_geometries() -> dict:
    try:
        with open(_CELL_GEOM_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

SEVERE_STRESS_ZSCORE = -1.0 
SOC_CONVERSION_FACTOR  = 10.0
MAX_CLOUD_PCT         = 30
WET_MONTHS            = {3, 4, 5, 10, 11, 12}
WORLDCOVER_CROPLAND    = 40

HIGH_ALERT_PCT     = 15.0
MODERATE_ALERT_PCT =  5.0

# ─────────────────────────────────────────────────────────────────────────────
# CACHED RESOURCES & HELPERS
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

def send_sms_alert(
    supabase_client,
    district: str,
    sector: str,
    stress_pct: float,
    stress_km2: float,
    season_label: str,
    alert_label: str,
) -> tuple[int, str]:
    """
    Fetch active contacts for the district/sector from Supabase
    and send an SMS alert via Africa's Talking Sandbox.
    Returns (number_sent, status_message).
    """
    try:
        at_username = st.secrets.get("AT_USERNAME", "sandbox")
        at_api_key  = st.secrets.get(
            "AT_API_KEY",
            "atsk_a851e00bec541799c7b1bd372a2c58cfea6317409b096bf6d3651d2655da7c267d6d9ca3"
        )
        sender_id   = st.secrets.get("AT_SENDER_ID", "AgriScan")

        if not at_api_key:
            return 0, "Africa's Talking API key not configured."

        # Fetch contacts for this district
        query = supabase_client.table("contacts").select("*").eq("district", district).eq("active", True)
        if sector and sector != "All Sectors":
            # Get sector-specific contacts OR district-wide contacts
            resp = supabase_client.table("contacts").select("*").eq("district", district).eq("active", True).execute()
        else:
            resp = query.execute()

        contacts = resp.data if resp.data else []
        if not contacts:
            return 0, f"No active contacts found for {district}."

        phones = [c["phone"] for c in contacts]

        # Build SMS message
        location = sector if (sector and sector != "All Sectors") else district
        msg = (
            f"AGRI-SCAN ALERT [{location}] {season_label}\n"
            f"{alert_label}: {stress_pct}% of cropland ({stress_km2} km2) under severe stress.\n"
            f"Immediate field inspection recommended.\n"
            f"- Agri-Scan Rwanda"
        )

        # Send via Africa's Talking direct HTTP API
        import urllib.request, urllib.parse
        payload = urllib.parse.urlencode({
            "username": at_username,
            "to":       ",".join(phones),
            "message":  msg,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.sandbox.africastalking.com/version1/messaging",
            data=payload,
            headers={
                "apiKey": at_api_key,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())

        recipients = result.get("SMSMessageData", {}).get("Recipients", [])
        sent = len([r for r in recipients if r.get("status") == "Success"])
        return sent, f"✅ Alert sent to {sent} contact(s) in {location}."

    except Exception as exc:
        return 0, f"SMS error: {exc}"


def add_ee_layer(fmap: folium.Map, ee_image: ee.Image, vis_params: dict, name: str) -> None:
    map_id_dict = ee.Image(ee_image).getMapId(vis_params)
    folium.TileLayer(
        tiles=map_id_dict["tile_fetcher"].url_format,
        attr="Google Earth Engine",
        name=name,
        overlay=True,
        control=True,
    ).add_to(fmap)

def build_roi(district: str, sector: str, cell: str | None = None) -> tuple[ee.Geometry, bool]:
    gaul = ee.FeatureCollection("FAO/GAUL/2015/level2")
    district_geom = gaul.filter(
        ee.Filter.And(
            ee.Filter.eq("ADM0_NAME", "Rwanda"),
            ee.Filter.eq("ADM2_NAME", district),
        )
    ).geometry()

    if cell and cell != "All Cells":
        cell_geoms = _load_cell_geometries()
        if cell in cell_geoms:
            candidate = ee.Geometry(cell_geoms[cell])
            return district_geom.intersection(candidate, maxError=1), True

    if sector and sector != "All Sectors":
        sector_geoms = _load_sector_geometries()
        if sector in sector_geoms:
            candidate = ee.Geometry(sector_geoms[sector])
        else:
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

    masked_baseline = baseline_ic.map(lambda img: img.updateMask(combined_mask))
    mean_img = masked_baseline.mean()
    std_img  = masked_baseline.reduce(ee.Reducer.stdDev()).rename("NDVI")

    baseline_stats = mean_img.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=roi, scale=100, maxPixels=1e8,
    )
    baseline_mean = float(
        ee.Number(baseline_stats.get("NDVI", ee.Number(0))).getInfo() or 0.0
    )

    current = s2_ndvi.select("NDVI").filterDate(str(start), str(end)).median().updateMask(combined_mask)
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

def compute_rain_stats(roi: ee.Geometry, start: date, end: date) -> tuple[float, str]:
    """
    Mean rainfall anomaly % over the ROI.
    Returns (anomaly_pct, status_label).
    """
    try:
        chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterBounds(roi)
        current_sum    = chirps.filterDate(str(start), str(end)).select("precipitation").sum()
        historical_avg = chirps.filterDate("2020-01-01","2024-12-31").select("precipitation").sum().divide(5)
        anomaly_img    = current_sum.subtract(historical_avg).divide(historical_avg).multiply(100).clip(roi)
        stats = anomaly_img.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=roi, scale=5000, maxPixels=1e8
        )
        val = ee.Number(stats.get("precipitation", ee.Number(0))).getInfo()
        pct = round(float(val or 0), 1)
        if pct < -20:
            label = "🔴 Rainfall Deficit"
        elif pct < -10:
            label = "🟡 Below Normal"
        elif pct > 20:
            label = "🔵 Above Normal"
        else:
            label = "🟢 Near Normal"
        return pct, label
    except Exception:
        return 0.0, "—"


def compute_soc_stats(roi: ee.Geometry) -> tuple[float, str]:
    """
    Mean soil organic carbon (g/kg) over cropland in the ROI.
    FAO thresholds: <20 = Low, 20-40 = Medium, >40 = High
    """
    try:
        crop_mask = _get_cropland_mask(roi)
        soc = (
            ee.Image("projects/soilgrids-isric/soc_mean")
            .select("soc_0-5cm_mean")
            .divide(SOC_CONVERSION_FACTOR)
            .updateMask(crop_mask)
            .clip(roi)
        )
        stats = soc.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=roi, scale=250, maxPixels=1e8
        )
        val = ee.Number(stats.get("soc_0-5cm_mean", ee.Number(0))).getInfo()
        soc_val = round(float(val or 0), 1)
        if soc_val < 20:
            label = "🔴 Low SOC (<20 g/kg)"
        elif soc_val < 40:
            label = "🟡 Medium SOC (20-40 g/kg)"
        else:
            label = "🟢 High SOC (>40 g/kg)"
        return soc_val, label
    except Exception:
        return 0.0, "—"


@st.cache_data(show_spinner=False, ttl=86400)
def get_total_cropland_km2(district: str, sector: str = "All Sectors", cell: str | None = None) -> float:
    roi, _ = build_roi(district, sector, cell)
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

    baseline_ic = s2.filterDate("2019-01-01", "2024-12-31").filter(
        ee.Filter.Or(
            ee.Filter.calendarRange(3, 5, "month"),
            ee.Filter.calendarRange(10, 12, "month"),
        )
    ) if wet else s2.filterDate("2019-01-01", "2024-12-31").filter(
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
# MAP LEGEND HTML
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
# MAIN APP
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    st.title("🌾 Agri-Scan Rwanda: Crop Health & Climate Intelligence")
    st.markdown(
        "Real-time Earth Observation & Food Security Monitoring — "
        "Sentinel-2 Z-Score · CHIRPS · SoilGrids · ESA WorldCover"
    )

    if not init_earth_engine():
        st.error("⚠️ Earth Engine could not be initialised. Ensure credentials are correct.")
        st.stop()

    supabase = get_supabase()

    # Sidebar Inputs
    st.sidebar.header("📍 Location & Search")
    district = st.sidebar.selectbox("District", list(DISTRICT_SECTORS.keys()))
    sector   = st.sidebar.selectbox("Sector (Umurenge)", ["All Sectors"] + DISTRICT_SECTORS[district])
    
    cell_geoms = _load_cell_geometries()
    cell_options = ["All Cells"] + sorted(list(cell_geoms.keys()))
    selected_cell = st.sidebar.selectbox("Cell (Akagari)", cell_options)
    cell = None if selected_cell == "All Cells" else selected_cell

    st.sidebar.markdown("---")
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

    roi, is_sector_mode = build_roi(district, sector, cell)
    location_label = cell if (cell and cell != 'All Cells') else (sector if is_sector_mode else district)
    wet            = _is_wet_season(start_date, end_date)
    season_label   = "🌧️ Wet Season" if wet else "☀️ Dry Season"

    with st.spinner("Computing geospatial indicators..."):
        ndvi_anomaly, stress_km2, baseline_mean = compute_ndvi_anomaly(roi, start_date, end_date)
        total_cropland_km2 = get_total_cropland_km2(district, sector, cell)
        stress_pct = round((stress_km2 / total_cropland_km2 * 100), 1) if total_cropland_km2 > 0 else 0.0
        rain_anomaly = compute_rain_anomaly(roi, start_date, end_date)
        soc_layer = get_soc_layer(roi)
        rain_pct, rain_label = compute_rain_stats(roi, start_date, end_date) if show_rain else (0.0, "—")
        soc_val, soc_label   = compute_soc_stats(roi) if show_soc else (0.0, "—")

    # UPI / Parcel Lookup
    map_center = [-1.9403, 29.8739]
    zoom_level = 11
    parcel_data = None
    upi = upi_input.strip()

    if upi and supabase:
        try:
            resp = supabase.table("parcels").select("*").eq("upi", upi).execute()
            if resp.data:
                parcel_data = resp.data[0]
                map_center  = [parcel_data["latitude"], parcel_data["longitude"]]
                zoom_level  = 18
                st.sidebar.success(f"🎯 {parcel_data.get('village','')} ({parcel_data.get('sector','')})")
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

    # KPI Row
    if stress_pct > HIGH_ALERT_PCT:
        alert_label, alert_color = "🔴 High Alert", "inverse"
    elif stress_pct > MODERATE_ALERT_PCT:
        alert_label, alert_color = "🟡 Moderate", "off"
    else:
        alert_label, alert_color = "🟢 Normal", "normal"

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Selected Area", location_label)
    k2.metric("Season", season_label)
    k3.metric("Cropland Stress", f"{stress_km2} km²  ({stress_pct}%)", delta=alert_label, delta_color=alert_color)
    k4.metric("Total Cropland Area", f"{total_cropland_km2} km²", delta=f"Baseline NDVI {round(baseline_mean, 3)}" if baseline_mean else "—")

    if show_rain or show_soc:
        kr1, kr2 = st.columns(2)
        if show_rain:
            kr1.metric("Rainfall Anomaly", f"{rain_pct}%", delta=rain_label,
                       delta_color="inverse" if rain_pct < -20 else ("off" if rain_pct < -10 else "normal"))
        if show_soc:
            kr2.metric("Soil Organic Carbon", f"{soc_val} g/kg", delta=soc_label,
                       delta_color="inverse" if soc_val < 20 else ("off" if soc_val < 40 else "normal"))

    st.markdown("---")

    # Map Creation
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
        add_ee_layer(fmap, ndvi_anomaly, {"min": -2.0, "max": 2.0, "palette": ["d7191c", "fdae61", "ffffbf", "abd9e9", "2c7bb6"]}, "Crop Vigor Z-Score Anomaly")

    if show_rain:
        add_ee_layer(fmap, rain_anomaly, {"min": -50, "max": 50, "palette": ["a6611a", "dfc27d", "f5f5f5", "80cdc1", "018571"]}, "Rainfall Anomaly %")

    if show_soc:
        add_ee_layer(fmap, soc_layer, {"min": 5, "max": 60, "palette": ["f5f5f5", "c7e9c0", "74c476", "238b45", "00441b"]}, "Soil Organic Carbon (g/kg)")

    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.get_root().html.add_child(folium.Element(NDVI_LEGEND_HTML))

    Draw(
        export=False,
        position="topleft",
        draw_options={"polyline": False, "rectangle": True, "polygon": True, "circle": False, "marker": True, "circlemarker": False},
    ).add_to(fmap)

    map_data = st_folium(fmap, use_container_width=True, height=540, key="farm_map")

    # Location Details Section
    st.markdown("### 📍 Farm Location Details")
    lc1, lc2, lc3, lc4 = st.columns(4)
    lc1.metric("Sector (Umurenge)", location_label)
    lc2.metric("Cell (Akagari)", cell or "Not specified")
    lc3.metric("Village (Umudugudu)", village or "Not specified")
    lc4.metric("UPI Parcel ID", upi or "Draw on map")

    # Plot analytics based on drawn geometry
    plot_z_val: float | None = None
    if map_data and map_data.get("last_active_drawing"):
        drawing = map_data["last_active_drawing"]
        try:
            geom_type = drawing["geometry"]["type"]
            coords    = drawing["geometry"]["coordinates"]
            farm_geom = ee.Geometry.Polygon(coords) if geom_type == "Polygon" else ee.Geometry.Point(coords).buffer(50)

            if ndvi_anomaly is not None:
                plot_area_ha = farm_geom.area(maxError=1).divide(10_000).getInfo()
                plot_stats   = ndvi_anomaly.reduceRegion(reducer=ee.Reducer.mean(), geometry=farm_geom, scale=10, maxPixels=1e8).getInfo()
                plot_z_val   = plot_stats.get("z_score")

                st.markdown("### 🎯 Specific Plot Analytics")
                pc1, pc2, pc3 = st.columns(3)
                pc1.metric("Plot Area", f"{round(plot_area_ha, 2)} ha")
                pc2.metric("Z-Score Anomaly", f"{round(plot_z_val, 2)} σ" if plot_z_val is not None else "N/A")
                if plot_z_val is not None:
                    if plot_z_val < SEVERE_STRESS_ZSCORE:
                        pc3.metric("Condition", "Severe Stress 🔴", delta="Action needed", delta_color="inverse")
                    elif plot_z_val < -0.5:
                        pc3.metric("Condition", "Mild Stress 🟡", delta="Monitor", delta_color="off")
                    else:
                        pc3.metric("Condition", "Healthy 🟢", delta="Good vigor")
        except Exception as exc:
            st.warning(f"Plot metrics error: {exc}")

    st.markdown("---")

    # Time-Series + Agronomic Advisory
    chart_col, advisory_col = st.columns([2, 1])

    with chart_col:
        st.subheader(f"📈 Cropland NDVI — {location_label}  ({season_label})")
        try:
            with st.spinner("Fetching time-series…"):
                ts_df, ts_baseline = get_ndvi_timeseries(district, sector, str(start_date), str(end_date))

            if ts_df.empty:
                st.info("No cloud-free observations in the selected range.")
            else:
                st.line_chart(ts_df[["NDVI", "Seasonal Baseline"]], y_label="NDVI")
                st.caption(f"Cropland NDVI · Baseline = {season_label} median 2019–2024 ({round(ts_baseline, 3)}).")
        except Exception as exc:
            st.warning(f"Time-series error: {exc}")

    with advisory_col:
        st.subheader("💡 Agronomic Advisory")
        if stress_pct > HIGH_ALERT_PCT:
            st.warning(f"**🔴 High Alert:** {stress_km2} km² (**{stress_pct}% of cropland**) under severe stress.")
            st.markdown("- **Field inspection:** Dispatch extension agents.\n- **Irrigation:** Prioritise deficit zones.\n- **Fertiliser:** Check nitrogen in stressed parcels.")
        elif stress_pct > MODERATE_ALERT_PCT:
            st.info(f"**🟡 Moderate Alert:** {stress_km2} km² ({stress_pct}% of cropland) showing stress.")
            st.markdown("- **Spot checks:** Visit highlighted cells within the week.\n- **Cross-check:** Compare with CHIRPS rainfall layer.")
        else:
            st.success(f"**🟢 Stable:** {stress_pct}% cropland stressed — within normal variation.")
            st.markdown("- **Routine monitoring:** Continue bi-weekly tracking.")

    # Farmer Summary & Export Options
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
            sms += f"🔴 {stress_pct}% of cropland under severe Z-score stress. Contact agronomist."
        elif stress_pct > MODERATE_ALERT_PCT:
            sms += f"🟡 {stress_pct}% of cropland showing stress. Monitor closely."
        else:
            sms += "✅ Conditions stable. Routine monitoring advised."
        st.info(sms)

    # ── SMS Alert ─────────────────────────────────────────────────────────────
    st.sidebar.markdown("---")
    st.sidebar.header("📨 SMS Alert")
    if stress_pct > MODERATE_ALERT_PCT and supabase:
        btn_label = f"📱 Send {alert_label} to Extension Officers"
        if st.sidebar.button(btn_label, type="primary"):
            with st.spinner("Sending SMS alert..."):
                n_sent, sms_status = send_sms_alert(
                    supabase, district, sector,
                    stress_pct, stress_km2,
                    season_label, alert_label
                )
            if n_sent > 0:
                st.sidebar.success(sms_status)
            else:
                st.sidebar.warning(sms_status)
    elif stress_pct <= MODERATE_ALERT_PCT:
        st.sidebar.info("✅ No alert needed — conditions stable.")
    else:
        st.sidebar.warning("Supabase not configured.")

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
        "District":              district,
        "Sector":                sector,
        "Cell":                  cell or "All",
        "Season":                "Wet" if wet else "Dry",
        "Start Date":            start_date,
        "End Date":              end_date,
        "Total Cropland km2":    total_cropland_km2,
        "Stressed km2":          stress_km2,
        "Stressed %":            stress_pct,
        "Alert Level":           alert_label,
        "Baseline NDVI":         round(baseline_mean, 3),
        "Rainfall Anomaly %":    rain_pct,
        "SOC g/kg":              soc_val,
    }])
    st.sidebar.download_button(
        "⬇ Summary (CSV)",
        data=summary.to_csv(index=False).encode("utf-8"),
        file_name="agri_scan_summary.csv",
        mime="text/csv",
    )

    st.sidebar.markdown("---")
    st.sidebar.caption(
        "Agri-Scan Rwanda v4.0 · Nshuti Aimé · IUSS Pavia\n\n"
        "Sentinel-2 Z-Score · CHIRPS · SoilGrids · ESA WorldCover · geoBoundaries ADM3"
    )


if __name__ == "__main__":
    main()
