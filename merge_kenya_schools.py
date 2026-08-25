#!/usr/bin/env python3
"""
merge_kenya_schools.py

Download Kenyan school datasets, normalize columns, deduplicate, and export cleaned CSV + GeoJSON.

Usage:
    python merge_kenya_schools.py

Outputs:
    - kenya_schools_cleaned.csv
    - kenya_schools_cleaned.geojson

Notes:
 - Adjust SOURCE_URLS or source_priority list if you want different sources.
 - GeoJSON output requires latitude and longitude to be present.
"""
import os
import io
import sys
import math
import json
import time
import requests
import tempfile
import pandas as pd
from rapidfuzz import fuzz, process

# Optional: geopandas used for GeoJSON output (fallback to manual if not installed)
try:
    import geopandas as gpd
    from shapely.geometry import Point
    GEOPANDAS_AVAILABLE = True
except Exception:
    GEOPANDAS_AVAILABLE = False

# === Configuration: source URLs (edit if needed) ===
SOURCE_URLS = {
    "openafrica": "https://open.africa/dataset/d1273674-a93b-4484-abe4-63f199eb1710/resource/b05ed34a-f1a8-4bdd-9a8a-9aa66997beec/download/all-schools-data.csv",
    "codeamani": "https://raw.githubusercontent.com/codeAmani-Labs/kenyan-schools-org/master/web/data/schools.csv",
    # Optional placeholder for World Bank or other source; fill with an accessible CSV URL if you have one
    "worldbank": ""  # e.g. "https://example.org/worldbank_kenya_schools.csv"
}

# Preferred source order when resolving conflicts (first wins)
source_priority = ["worldbank", "openafrica", "codeamani"]

# Target columns for cleaned output
TARGET_COLS = ["school_name","level","county","subcounty","ward","latitude","longitude",
               "address","phone","email","website","gender","source","raw_id"]

# === Helpers ===
def download_csv(url, timeout=60):
    if not url:
        return None
    print(f"Downloading: {url}")
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        # Try to guess encoding
        content = r.content
        return pd.read_csv(io.BytesIO(content), low_memory=False)
    except Exception as e:
        print(f"Failed to download {url}: {e}")
        return None

def normalize_colnames(df):
    df = df.copy()
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns.astype(str)]
    return df

def pick_first_nonnull(values, priority):
    # values is dict source->value; priority is ordered list
    for s in priority:
        v = values.get(s)
        if pd.notnull(v) and v != "":
            return v
    # fallback: any non-null
    for v in values.values():
        if pd.notnull(v) and v != "":
            return v
    return None

def try_float(x):
    try:
        return float(x)
    except Exception:
        return None

def normalize_name(name):
    if pd.isna(name):
        return ""
    s = str(name).lower()
    # remove punctuation
    import re
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def haversine(lat1, lon1, lat2, lon2):
    # returns meters
    if None in (lat1, lon1, lat2, lon2):
        return float('inf')
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2*R*math.asin(math.sqrt(a))

# === Column mapping hints ===
COLUMN_HINTS = {
    "school_name": ["school_name","name","facility_name","facility","school","sch_name"],
    "level": ["level","school_level","category","type","level_of_school"],
    "county": ["county","county_name","countyname"],
    "subcounty": ["subcounty","sub_county","sub_county_name","district","subdivision"],
    "ward": ["ward","ward_name"],
    "latitude": ["latitude","lat","y","gps_lat","coord_lat"],
    "longitude": ["longitude","lon","long","x","lng","gps_lon","coord_lon"],
    "address": ["address","addr","location","physical_address"],
    "phone": ["phone","telephone","tel","phone_number"],
    "email": ["email","e_mail"],
    "website": ["website","web","url"],
    "gender": ["gender","sex"]
}

def map_columns(df, source_name):
    df = normalize_colnames(df)
    out = pd.DataFrame()
    for tgt, hints in COLUMN_HINTS.items():
        found = None
        for h in hints:
            if h in df.columns:
                found = df[h]
                break
        if found is not None:
            out[tgt] = found
        else:
            out[tgt] = None
    # keep an original id if present
    if "id" in df.columns:
        out["raw_id"] = df["id"].astype(str)
    else:
        # try other possible id columns
        for c in ["school_id","uid","facility_id","fid"]:
            if c in df.columns:
                out["raw_id"] = df[c].astype(str)
                break
        else:
            out["raw_id"] = None
    out["source"] = source_name
    return out

# === Main pipeline ===
def main():
    # Step 1: download sources
    dfs = []
    for name, url in SOURCE_URLS.items():
        df = download_csv(url) if url else None
        if df is None:
            print(f"No data for source {name} (url blank or failed).")
            continue
        mapped = map_columns(df, name)
        # ensure lat/lon floats
        mapped["latitude"] = mapped["latitude"].apply(try_float)
        mapped["longitude"] = mapped["longitude"].apply(try_float)
        # normalize school_name
        mapped["school_name_norm"] = mapped["school_name"].apply(normalize_name)
        # fill small subset of nulls
        dfs.append(mapped)
        print(f"Loaded {len(mapped)} rows from {name}")
        time.sleep(0.5)

    if not dfs:
        print("No datasets downloaded. Edit SOURCE_URLS to add valid CSV URLs.")
        sys.exit(1)

    # Step 2: concatenate
    all_df = pd.concat(dfs, ignore_index=True, sort=False)
    print(f"Total rows before dedupe: {len(all_df)}")

    # Step 3: dedupe heuristics
    # Strategy: group by county, then for each county build clusters by fuzzy name + geo proximity.
    results = []
    used = set()
    idx_series = all_df.index.to_series()

    for county, group in all_df.groupby(all_df["county"].fillna("")):
        # build list of indices
        indices = group.index.tolist()
        # we will cluster by iterating unassigned indices
        unassigned = set(indices)
        while unassigned:
            i = unassigned.pop()
            row_i = all_df.loc[i]
            cluster = [i]
            name_i = row_i["school_name_norm"]
            lat_i = row_i["latitude"]
            lon_i = row_i["longitude"]
            # compare with others
            to_remove = []
            for j in list(unassigned):
                row_j = all_df.loc[j]
                name_j = row_j["school_name_norm"]
                lat_j = row_j["latitude"]
                lon_j = row_j["longitude"]
                # fuzzy similarity score
                score = 0
                if name_i and name_j:
                    score = fuzz.token_sort_ratio(name_i, name_j)
                # geo distance in meters
                dist = haversine(lat_i, lon_i, lat_j, lon_j) if None not in (lat_i, lon_i, lat_j, lon_j) else float('inf')
                # decide threshold: match if name similarity > 88 or distance < 100 meters and name similarity > 70
                if score >= 88 or (dist < 100 and score >= 70):
                    cluster.append(j)
                    to_remove.append(j)
            for j in to_remove:
                unassigned.remove(j)
            # Merge cluster rows preferring source priority
            merged = {}
            merged_raws = {}
            # For each target column, collect values by source and choose by priority
            for col in ["school_name","level","county","subcounty","ward","latitude","longitude","address","phone","email","website","gender","raw_id"]:
                values_by_source = {}
                for idx in cluster:
                    src = all_df.at[idx, "source"]
                    values_by_source.setdefault(src, None)
                    v = all_df.at[idx, col] if col in all_df.columns else None
                    # prefer non-empty
                    if pd.notnull(v) and v != "":
                        values_by_source[src] = v
                chosen = pick_first_nonnull(values_by_source, source_priority)
                merged[col] = chosen
                merged_raws[col] = values_by_source
            merged["source"] = ",".join(sorted(set(all_df.loc[cluster, "source"].dropna().astype(str))))
            merged["cluster_size"] = len(cluster)
            results.append(merged)

    clean_df = pd.DataFrame(results, columns=[c for c in TARGET_COLS] + ["cluster_size"])
    # ensure lat/lon numeric
    clean_df["latitude"] = clean_df["latitude"].apply(try_float)
    clean_df["longitude"] = clean_df["longitude"].apply(try_float)

    print(f"Total rows after dedupe: {len(clean_df)}")

    # Reorder and keep only target columns + cluster_size
    out_cols = TARGET_COLS + ["cluster_size"]
    for c in out_cols:
        if c not in clean_df.columns:
            clean_df[c] = None
    clean_df = clean_df[out_cols]

    # Save CSV
    csv_path = "kenya_schools_cleaned.csv"
    clean_df.to_csv(csv_path, index=False)
    print(f"Wrote cleaned CSV: {csv_path}")

    # Save GeoJSON
    geojson_path = "kenya_schools_cleaned.geojson"
    if GEOPANDAS_AVAILABLE:
        # create GeoDataFrame
        clean_df_geo = clean_df.dropna(subset=["latitude","longitude"]).copy()
        geometries = [Point(xy) for xy in zip(clean_df_geo["longitude"], clean_df_geo["latitude"])]
        gdf = gpd.GeoDataFrame(clean_df_geo, geometry=geometries, crs="EPSG:4326")
        gdf.to_file(geojson_path, driver="GeoJSON")
        print(f"Wrote GeoJSON (geopandas): {geojson_path}")
    else:
        # fallback: write basic GeoJSON manually
        features = []
        for _, r in clean_df.dropna(subset=["latitude","longitude"]).iterrows():
            try:
                lat = float(r["latitude"])
                lon = float(r["longitude"])
            except Exception:
                continue
            props = {c: (r[c] if pd.notnull(r[c]) else None) for c in clean_df.columns if c not in ("latitude","longitude")}
            feat = {"type":"Feature", "geometry":{"type":"Point","coordinates":[lon, lat]}, "properties":props}
            features.append(feat)
        gj = {"type":"FeatureCollection", "features":features}
        with open(geojson_path, "w", encoding="utf-8") as f:
            json.dump(gj, f, ensure_ascii=False, indent=2)
        print(f"Wrote GeoJSON (fallback): {geojson_path}")

    print("Done.")

if __name__ == "__main__":
    main()
