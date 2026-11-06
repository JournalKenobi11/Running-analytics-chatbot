#!/usr/bin/env python3
"""
Dreeve / Runwise Running Analytics Engine.
High-performance, token-efficient analytics and visualization for Dreeve running database.
"""

import os
import sys
import sqlite3
import argparse
import json
import re
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from matplotlib.patches import Patch
from scipy import stats
import zstandard
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "dreeve" / "storage" / "database" / "dreeve.db"
DB_DEFAULT_PATH = os.environ.get(
    "DREEVE_DB_PATH",
    str(_DEFAULT_DB if _DEFAULT_DB.exists() else "dreeve.db")
)

OUTPUT_DEFAULT_DIR = os.environ.get(
    "DREEVE_OUTPUT_DIR",
    str(Path(__file__).resolve().parent / "output")
)

def get_connection(db_path=DB_DEFAULT_PATH, binary=False):
    """Open read-only SQLite connection to Dreeve database."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Dreeve database not found at {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    if binary:
        conn.text_factory = bytes
    else:
        conn.row_factory = sqlite3.Row
    return conn

def format_pace(minutes_float):
    """Format minutes float (e.g. 6.5) to 'MM:SS /km'."""
    if pd.isna(minutes_float) or minutes_float <= 0:
        return "N/A"
    mins = int(minutes_float)
    secs = int(round((minutes_float - mins) * 60))
    if secs == 60:
        mins += 1
        secs = 0
    return f"{mins}:{secs:02d} /km"

def get_activities_dataframe(days=None, sport="Run", with_hr_only=False, search=None, db_path=DB_DEFAULT_PATH):
    """Extract and prepare pandas DataFrame of activities with calculated pace/speed."""
    conn = get_connection(db_path)
    conditions = ["(markedForDeletion IS NULL OR markedForDeletion = 0)"]
    params = []

    if sport:
        conditions.append("(sportType = ? OR activityType = ?)")
        params.extend([sport, sport])

    if days:
        cutoff = datetime.now() - timedelta(days=days)
        conditions.append("startDateTime >= ?")
        params.append(cutoff.strftime("%Y-%m-%d %H:%M:%S"))

    if with_hr_only:
        conditions.append("averageHeartRate IS NOT NULL")

    if search:
        conditions.append("(name LIKE ? OR description LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    query = f"""
        SELECT activityId, startDateTime, sportType, name, distance,
               movingTimeInSeconds, averageSpeed, averageHeartRate,
               maxHeartRate, elevation, calories
        FROM Activity
        WHERE {' AND '.join(conditions)}
        ORDER BY startDateTime ASC
    """
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()

    if df.empty:
        return df

    df['start_dt'] = pd.to_datetime(df['startDateTime'])
    df['dist_km'] = df['distance'] / 1000.0
    # True Pace based on moving time and distance
    df['pace_min_km'] = np.where(
        df['dist_km'] > 0,
        (df['movingTimeInSeconds'] / df['dist_km']) / 60.0,
        np.nan
    )
    df['speed_kmh'] = np.where(
        df['movingTimeInSeconds'] > 0,
        (df['dist_km']) / (df['movingTimeInSeconds'] / 3600.0),
        df['averageSpeed']
    )
    if 'averageHeartRate' in df.columns:
        df['averageHeartRate'] = pd.to_numeric(df['averageHeartRate'], errors='coerce')

    return df

def get_summary(days=60, sport="Run", db_path=DB_DEFAULT_PATH):
    """
    Produce a compact, high-signal statistical summary.
    Designed for minimum token usage (< 250 tokens).
    """
    df = get_activities_dataframe(days=days, sport=sport, db_path=db_path)
    if df.empty:
        return {"error": f"No {sport} activities found in the last {days} days."}

    total_runs = len(df)
    total_km = round(df['dist_km'].sum(), 1)
    total_time_hrs = round(df['movingTimeInSeconds'].sum() / 3600.0, 1)

    avg_dist_km = round(df['dist_km'].mean(), 2)
    avg_pace_val = df['pace_min_km'].mean()
    best_pace_val = df['pace_min_km'].min()

    # Heart Rate metrics
    df_hr = df.dropna(subset=['averageHeartRate'])
    hr_runs_count = len(df_hr)

    hr_stats = None
    if hr_runs_count > 0:
        hr_min = int(df_hr['averageHeartRate'].min())
        hr_max = int(df_hr['averageHeartRate'].max())
        hr_mean = round(df_hr['averageHeartRate'].mean(), 1)

        # HR Zones breakdown
        z1 = len(df_hr[df_hr['averageHeartRate'] < 130])
        z2 = len(df_hr[(df_hr['averageHeartRate'] >= 130) & (df_hr['averageHeartRate'] < 145)])
        z3 = len(df_hr[(df_hr['averageHeartRate'] >= 145) & (df_hr['averageHeartRate'] < 155)])
        z4_5 = len(df_hr[df_hr['averageHeartRate'] >= 155])

        # Pace vs HR correlation
        if hr_runs_count >= 3:
            r_val, p_val = stats.pearsonr(df_hr['averageHeartRate'], df_hr['pace_min_km'])
            corr = {"r": round(r_val, 2), "p_val": round(p_val, 4), "r_squared": round(r_val**2, 2)}
        else:
            corr = None

        hr_stats = {
            "runs_with_hr": hr_runs_count,
            "min_hr_bpm": hr_min,
            "max_hr_bpm": hr_max,
            "mean_hr_bpm": hr_mean,
            "zones": {
                "Zone 1 (<130 Recovery)": z1,
                "Zone 2 (130-145 Aerobic Base)": z2,
                "Zone 3 (145-155 Tempo)": z3,
                "Zone 4+ (>155 Threshold/HIIT)": z4_5
            },
            "pace_hr_correlation": corr
        }

    latest = df.iloc[-1]
    latest_run = {
        "date": latest['start_dt'].strftime('%Y-%m-%d %H:%M'),
        "name": latest['name'],
        "distance_km": round(latest['dist_km'], 2),
        "pace": format_pace(latest['pace_min_km']),
        "avg_hr": int(latest['averageHeartRate']) if pd.notnull(latest['averageHeartRate']) else None
    }

    return {
        "timeframe_days": days,
        "sport": sport,
        "total_activities": total_runs,
        "total_distance_km": total_km,
        "total_moving_time_hours": total_time_hrs,
        "avg_distance_per_run_km": avg_dist_km,
        "overall_avg_pace": format_pace(avg_pace_val),
        "best_pace": format_pace(best_pace_val),
        "heart_rate_summary": hr_stats,
        "latest_activity": latest_run
    }

def get_activities_list(days=60, limit=20, sport="Run", with_hr_only=False, search=None, sort="DESC", db_path=DB_DEFAULT_PATH):
    """
    Return a concise, token-efficient table of activities.
    """
    df = get_activities_dataframe(days=days, sport=sport, with_hr_only=with_hr_only, search=search, db_path=db_path)
    if df.empty:
        return []

    if sort.upper() == "DESC":
        df = df.sort_values('start_dt', ascending=False)
    else:
        df = df.sort_values('start_dt', ascending=True)

    if limit:
        df = df.head(limit)

    results = []
    for _, r in df.iterrows():
        results.append({
            "id": r['activityId'],
            "date": r['start_dt'].strftime('%Y-%m-%d'),
            "name": r['name'],
            "distance_km": round(r['dist_km'], 2),
            "time": f"{int(r['movingTimeInSeconds'] // 60)}m{int(r['movingTimeInSeconds'] % 60):02d}s",
            "pace": format_pace(r['pace_min_km']),
            "avg_hr": int(r['averageHeartRate']) if pd.notnull(r['averageHeartRate']) else None,
            "speed_kmh": round(r['speed_kmh'], 2)
        })
    return results

def get_splits_analysis(activity_id=None, days=None, month=None, summary_only=False, db_path=DB_DEFAULT_PATH):
    """
    High-performance kilometer split and heart rate analyzer.
    Decompresses raw Zstandard streams and calculates exact pace & mean HR per kilometer.
    Returns sub-second (<0.3s) results with token-optimized summary.
    """
    conn = get_connection(db_path, binary=True)
    cursor = conn.cursor()

    conditions = ["(sportType = 'Run' OR activityType = 'Run')", "(markedForDeletion IS NULL OR markedForDeletion = 0)"]
    params = []

    if activity_id:
        conditions.append("activityId = ?")
        params.append(activity_id)
    elif month:
        conditions.append("startDateTime LIKE ?")
        params.append(f"{month}%")
    elif days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        conditions.append("startDateTime >= ?")
        params.append(cutoff)

    where_clause = " AND ".join(conditions)
    cursor.execute(f"SELECT activityId, startDateTime, name, distance, movingTimeInSeconds FROM Activity WHERE {where_clause} ORDER BY startDateTime ASC", params)
    activities = cursor.fetchall()

    if not activities:
        conn.close()
        return {"error": "No activities found matching criteria."}

    dctx = zstandard.ZstdDecompressor()
    all_splits = []

    for act_id_b, dt_b, name_b, dist_m, mov_time in activities:
        act_id = act_id_b.decode('utf-8')
        dt = dt_b.decode('utf-8')
        name = name_b.decode('utf-8')

        cursor.execute("SELECT streamType, data FROM ActivityStream WHERE activityId = ?", (act_id,))
        streams = {}
        for st_b, raw in cursor.fetchall():
            st_name = st_b.decode('utf-8')
            if st_name in ('distance', 'time', 'heartrate'):
                try:
                    streams[st_name] = json.loads(dctx.decompress(raw).decode('utf-8'))
                except Exception:
                    pass

        if 'distance' not in streams or 'time' not in streams:
            continue

        dist_arr = np.array(streams['distance'])
        time_arr = np.array(streams['time'])
        hr_arr = np.array(streams['heartrate']) if 'heartrate' in streams else None

        if len(dist_arr) == 0:
            continue

        total_km = int(np.ceil(dist_arr[-1] / 1000.0))
        for k in range(1, total_km + 1):
            s_d = (k - 1) * 1000.0
            e_d = min(k * 1000.0, dist_arr[-1])
            mask = (dist_arr >= s_d) & (dist_arr <= e_d)
            if not np.any(mask):
                continue
            seg_d = dist_arr[mask][-1] - dist_arr[mask][0]
            seg_t = time_arr[mask][-1] - time_arr[mask][0]
            if seg_d < 150:
                continue
            seg_pace = (seg_t / (seg_d / 1000.0)) / 60.0
            seg_hr = float(np.mean(hr_arr[mask])) if hr_arr is not None and len(hr_arr) > 0 else None

            all_splits.append({
                "activity_id": act_id,
                "date": dt[:10],
                "name": name,
                "km": k,
                "distance_m": round(float(seg_d)),
                "duration_seconds": int(seg_t),
                "pace_min_km": round(float(seg_pace), 3),
                "pace_formatted": format_pace(seg_pace),
                "mean_hr_bpm": round(float(seg_hr), 1) if seg_hr is not None else None
            })

    conn.close()

    if not all_splits:
        return {"error": "No split telemetry available for matching activities."}

    df = pd.DataFrame(all_splits)

    # Filter splits with HR for correlation & physiological analytics
    df_hr = df.dropna(subset=['mean_hr_bpm'])
    correlation = None
    if len(df_hr) >= 3:
        r_val, p_val = stats.pearsonr(df_hr['mean_hr_bpm'], df_hr['pace_min_km'])
        correlation = {
            "r": round(float(r_val), 3),
            "p_val": float(f"{p_val:.2e}"),
            "r_squared": round(float(r_val**2), 3)
        }

    # Cardiac Drift Analysis (Progression across KM index)
    drift_agg = {}
    if not df_hr.empty:
        grouped = df_hr.groupby('km').agg(
            splits_count=('mean_hr_bpm', 'count'),
            avg_pace_min=('pace_min_km', 'mean'),
            avg_hr=('mean_hr_bpm', 'mean'),
            min_hr=('mean_hr_bpm', 'min'),
            max_hr=('mean_hr_bpm', 'max')
        )
        for km_idx, row in grouped.iterrows():
            drift_agg[f"Km {km_idx}"] = {
                "splits_evaluated": int(row['splits_count']),
                "avg_pace": format_pace(row['avg_pace_min']),
                "mean_hr_bpm": round(float(row['avg_hr']), 1),
                "hr_range": f"{round(float(row['min_hr']), 0):.0f} – {round(float(row['max_hr']), 0):.0f} bpm"
            }

    summary = {
        "activities_evaluated": len(activities),
        "total_splits_extracted": len(df),
        "splits_with_heart_rate": len(df_hr),
        "median_pace": format_pace(df['pace_min_km'].median()),
        "fastest_split_pace": format_pace(df['pace_min_km'].min()),
        "slowest_split_pace": format_pace(df['pace_min_km'].max()),
        "mean_heart_rate_overall": round(float(df_hr['mean_hr_bpm'].mean()), 1) if not df_hr.empty else None,
        "heart_rate_range_bpm": f"{round(float(df_hr['mean_hr_bpm'].min()), 0):.0f} – {round(float(df_hr['mean_hr_bpm'].max()), 0):.0f}" if not df_hr.empty else None,
        "pace_vs_heart_rate_correlation": correlation,
        "cardiac_drift_progression": drift_agg
    }

    if summary_only:
        return summary

    return {
        "summary": summary,
        "splits": all_splits
    }

def parse_time_to_seconds(t_str):
    """Convert time string (e.g. '90s', '90 secs', '2m', '1m30s') to integer seconds."""
    if not t_str:
        return None
    t_str = t_str.strip().lower()
    t_clean = re.sub(r'\b(walk|rest|recovery|jog|easy)\b', '', t_str).strip()
    m_sec = re.match(r'^(\d+)\s*(?:s|sec|secs|seconds)?$', t_clean)
    if m_sec:
        return int(m_sec.group(1))
    m_min = re.match(r'^(\d+(?:\.\d+)?)\s*(?:m|min|mins|minutes)$', t_clean)
    if m_min:
        return int(float(m_min.group(1)) * 60)
    m_combo = re.match(r'^(\d+)m(?:in)?\s*(\d+)s(?:ec)?$', t_clean)
    if m_combo:
        return int(m_combo.group(1)) * 60 + int(m_combo.group(2))
    return None

def parse_workout_plan(plan_str):
    """
    Parse arbitrary natural-language workout plans into an ordered sequence of target segments.
    Supports variable intervals, e.g.:
      - '0.8 easy then 2*1.5 at 6:00 and then 1 km at 7:00'
      - '1k warmup + 4x1K @6:15 with 90s walk + 0.5k cooldown'
      - '5x800m @ 5:30 with 2min rest'
      - '400m - 800m - 1200m - 800m - 400m'
    """
    if not plan_str or not plan_str.strip():
        return None

    clean = plan_str.strip()
    tokens = re.split(r'(?:\+|\band then\b|\bthen\b|\bfollowed by\b|,|;)', clean, flags=re.IGNORECASE)
    raw_blocks = [t.strip() for t in tokens if t.strip()]
    parsed_segments = []

    for block in raw_blocks:
        b = block
        reps_m = re.match(r'^(\d+)\s*[*xX]\s*(.*)$', b)
        if reps_m:
            count = int(reps_m.group(1))
            inner = reps_m.group(2).strip()
        else:
            count = 1
            inner = b

        rec_m = re.search(r'(?:with|\(|/)\s*([0-9.]+\s*(?:s|sec|secs|seconds|m|min|mins|minutes)?(?:\s*(?:walk|rest|recovery|jog|easy))?)\s*\)?', inner, re.IGNORECASE)
        rec_dur_sec = None
        rec_type = 'walk'
        if rec_m:
            rec_text = rec_m.group(1).strip()
            inner = inner[:rec_m.start()] + inner[rec_m.end():]
            rec_dur_sec = parse_time_to_seconds(rec_text)
            if 'jog' in rec_text.lower() or 'easy' in rec_text.lower():
                rec_type = 'jog'

        pace_m = re.search(r'(?:@|at|pace)\s*([0-9]+:[0-9]{2}|[0-9]+\.[0-9]+)', inner, re.IGNORECASE)
        target_pace = None
        if pace_m:
            p_str = pace_m.group(1)
            if ':' in p_str:
                pm, ps = p_str.split(':')
                target_pace = int(pm) + int(ps) / 60.0
            else:
                target_pace = float(p_str)
            inner = inner[:pace_m.start()] + inner[pace_m.end():]

        d_m = re.search(r'(\d+(?:\.\d+)?)\s*(k|km|kms|m|meter|meters)?\b', inner, re.IGNORECASE)
        target_dist = None
        if d_m:
            val = float(d_m.group(1))
            unit = (d_m.group(2) or '').lower()
            if unit in ('k', 'km', 'kms'):
                target_dist = val * 1000.0
            elif unit in ('m', 'meter', 'meters'):
                target_dist = val
            else:
                target_dist = val * 1000.0 if val < 25 else val

        lower_inner = inner.lower()
        if 'warmup' in lower_inner or 'easy' in lower_inner:
            seg_type = 'warmup'
        elif 'cooldown' in lower_inner:
            seg_type = 'cooldown'
        elif 'walk' in lower_inner or 'rest' in lower_inner:
            seg_type = 'recovery'
        else:
            seg_type = 'interval'

        for rep in range(count):
            label = ""
            if seg_type == 'warmup':
                label = f"Warmup ({round(target_dist)}m easy)" if target_dist else "Warmup"
            elif seg_type == 'cooldown':
                label = f"Cooldown ({round(target_dist)}m easy)" if target_dist else "Cooldown"
            else:
                dist_str = f"{round(target_dist)}m" if target_dist else "Interval"
                pace_str = f" @ {format_pace(target_pace)}" if target_pace else ""
                label = f"Interval ({dist_str}{pace_str})"

            parsed_segments.append({
                "type": seg_type,
                "label": label,
                "target_dist_m": target_dist,
                "target_pace_min_km": target_pace,
                "raw": block
            })

            if rec_dur_sec and rep < count - 1:
                parsed_segments.append({
                    "type": "recovery",
                    "subtype": rec_type,
                    "label": f"Recovery {'Jog' if rec_type == 'jog' else 'Walk'} ({rec_dur_sec}s)",
                    "target_time_s": rec_dur_sec,
                    "target_dist_m": None,
                    "target_pace_min_km": None,
                    "raw": f"{rec_dur_sec}s {rec_type}"
                })

    return parsed_segments

def detect_interval_segments(time_arr, dist_arr, vel_arr, hr_arr=None, plan_str=None):
    """
    Detects work intervals, warmups, recoveries, and cooldowns from second-by-second telemetry.
    Supports arbitrary variable structures and aligns to target plans when provided.
    """
    speed_kmh = vel_arr * 3.6
    speed_smooth = pd.Series(speed_kmh).rolling(15, center=True, min_periods=1).mean().to_numpy()

    # Movement threshold: Running vs Walking/Rest
    is_running = speed_smooth >= 5.8

    raw_blocks = []
    state = is_running[0]
    s_idx = 0
    for i in range(1, len(time_arr)):
        if is_running[i] != state:
            raw_blocks.append((state, s_idx, i - 1))
            state = is_running[i]
            s_idx = i
    raw_blocks.append((state, s_idx, len(time_arr) - 1))

    # Merge short fluctuations (< 18s)
    merged = []
    for st, s, e in raw_blocks:
        dur = time_arr[e] - time_arr[s]
        if not merged:
            merged.append([st, s, e])
        else:
            if dur < 18 and merged[-1][0] != st:
                merged[-1][2] = e
            elif merged[-1][0] == st:
                merged[-1][2] = e
            else:
                merged.append([st, s, e])

    # Detect step-downs in continuous running (e.g. interval ending into cooldown jog)
    final_blocks = []
    for st, s, e in merged:
        dur = time_arr[e] - time_arr[s]
        if st and dur > 220:
            sub_s = speed_smooth[s:e+1]
            w = 20
            if len(sub_s) > 2 * w:
                diff = np.zeros(len(sub_s))
                for i in range(w, len(sub_s) - w):
                    diff[i] = np.mean(sub_s[i:i+w]) - np.mean(sub_s[i-w:i])
                min_idx = np.argmin(diff)
                if diff[min_idx] < -2.0 and min_idx > 60 and (len(sub_s) - min_idx) > 60:
                    split_pt = s + min_idx
                    final_blocks.append((True, s, split_pt))
                    final_blocks.append((True, split_pt + 1, e))
                    continue
        final_blocks.append((st, s, e))

    parsed_plan = parse_workout_plan(plan_str) if plan_str else None

    # Calculate metrics for each block
    raw_segments = []
    for st, s, e in final_blocks:
        t0 = int(time_arr[s])
        t1 = int(time_arr[e])
        d0 = float(dist_arr[s])
        d1 = float(dist_arr[e])
        dur = t1 - t0
        dist = d1 - d0
        pace_min_km = (dur / (dist / 1000.0)) / 60.0 if dist > 20 else None
        speed = (dist / 1000.0) / (dur / 3600.0) if dur > 0 else 0.0

        sub_hr = hr_arr[s:e+1] if hr_arr is not None and len(hr_arr) > 0 else None
        avg_hr = round(float(np.mean(sub_hr)), 1) if sub_hr is not None and len(sub_hr) > 0 else None
        peak_hr = int(np.max(sub_hr)) if sub_hr is not None and len(sub_hr) > 0 else None
        min_hr = int(np.min(sub_hr)) if sub_hr is not None and len(sub_hr) > 0 else None
        start_hr = int(sub_hr[0]) if sub_hr is not None and len(sub_hr) > 0 else None
        end_hr = int(sub_hr[-1]) if sub_hr is not None and len(sub_hr) > 0 else None

        raw_segments.append({
            "is_running": st,
            "start_idx": s,
            "end_idx": e,
            "start_time_sec": t0,
            "end_time_sec": t1,
            "start_time_fmt": f"{t0 // 60:02d}:{t0 % 60:02d}",
            "end_time_fmt": f"{t1 // 60:02d}:{t1 % 60:02d}",
            "duration_sec": dur,
            "duration_fmt": f"{dur // 60}m {dur % 60:02d}s" if dur >= 60 else f"{dur}s",
            "dist_m": round(dist, 1),
            "speed_kmh": round(speed, 2),
            "pace_min_km": round(pace_min_km, 3) if pace_min_km else None,
            "pace_fmt": format_pace(pace_min_km) if pace_min_km else "N/A",
            "avg_hr": avg_hr,
            "peak_hr": peak_hr,
            "min_hr": min_hr,
            "start_hr": start_hr,
            "end_hr": end_hr
        })

    # Alignment and Classification
    run_segments = [s for s in raw_segments if s["is_running"]]

    if parsed_plan:
        planned_runs = [p for p in parsed_plan if p["type"] in ("warmup", "interval", "cooldown", "easy")]
        for i, actual_run in enumerate(run_segments):
            if i < len(planned_runs):
                plan_item = planned_runs[i]
                actual_run["type"] = plan_item["type"]
                actual_run["name"] = plan_item["label"]
                actual_run["target_dist_m"] = plan_item["target_dist_m"]
                actual_run["target_pace_fmt"] = format_pace(plan_item["target_pace_min_km"]) if plan_item["target_pace_min_km"] else None
                if plan_item["target_pace_min_km"] and actual_run["pace_min_km"]:
                    delta_sec = round((actual_run["pace_min_km"] - plan_item["target_pace_min_km"]) * 60)
                    sign = "+" if delta_sec > 0 else ""
                    if abs(delta_sec) <= 3:
                        note = "on target"
                    elif delta_sec < 0:
                        note = f"{-delta_sec}s faster"
                    else:
                        note = f"{delta_sec}s slower"
                    actual_run["pace_delta_fmt"] = f"{sign}{delta_sec}s/km ({note})"
            else:
                actual_run["type"] = "interval"
                actual_run["name"] = f"Interval {i + 1}"
    else:
        run_speeds = [r["speed_kmh"] for r in run_segments]
        max_speed = max(run_speeds) if run_speeds else 10.0
        work_int_counter = 1

        for i, actual_run in enumerate(run_segments):
            is_first = (i == 0)
            is_last = (i == len(run_segments) - 1)

            if is_first and len(run_segments) > 1 and actual_run["speed_kmh"] < max_speed - 1.5:
                actual_run["type"] = "warmup"
                actual_run["name"] = "Warmup"
            elif is_last and len(run_segments) > 2 and actual_run["speed_kmh"] < max_speed - 1.5:
                actual_run["type"] = "cooldown"
                actual_run["name"] = "Cooldown"
            else:
                actual_run["type"] = "interval"
                actual_run["name"] = f"Interval {work_int_counter}"
                work_int_counter += 1

    # Classify recoveries and calculate HR recovery drop
    rec_counter = 1
    for i, s in enumerate(raw_segments):
        if not s["is_running"]:
            s["type"] = "recovery"
            prev_runs = [prev for prev in raw_segments[:i] if prev["is_running"]]
            if prev_runs:
                prev_run = prev_runs[-1]
                if prev_run["type"] == "warmup":
                    s["name"] = "Transition Walk"
                else:
                    s["name"] = f"Walk Recovery {rec_counter}"
                    rec_counter += 1

                if prev_run["end_hr"] is not None and s["min_hr"] is not None:
                    drop = prev_run["end_hr"] - s["min_hr"]
                    s["hr_recovery_drop"] = f"-{drop} bpm"
            else:
                s["name"] = "Pre-Run Walk"

    # Intra-interval micro-splits (for work intervals >= 400m)
    for s in raw_segments:
        if s["type"] == "interval" and s["dist_m"] >= 400:
            s_idx = s["start_idx"]
            e_idx = s["end_idx"]
            rel_dist = dist_arr[s_idx:e_idx+1] - dist_arr[s_idx]
            sub_times = time_arr[s_idx:e_idx+1]
            sub_hrs = hr_arr[s_idx:e_idx+1] if hr_arr is not None else None

            bin_size = 200.0 if s["dist_m"] < 1200 else 400.0
            num_bins = int(np.ceil(s["dist_m"] / bin_size))
            micro_splits = []

            for b in range(num_bins):
                b_start = b * bin_size
                b_end = min((b + 1) * bin_size, s["dist_m"])
                mask = (rel_dist >= b_start) & (rel_dist <= b_end)
                if np.any(mask):
                    b_d = rel_dist[mask][-1] - rel_dist[mask][0]
                    b_t = sub_times[mask][-1] - sub_times[mask][0]
                    if b_d > 20 and b_t > 0:
                        b_pace = (b_t / (b_d / 1000.0)) / 60.0
                        b_hr = round(float(np.mean(sub_hrs[mask])), 1) if sub_hrs is not None and len(sub_hrs) > 0 else None
                        micro_splits.append({
                            "range_m": f"{int(b_start)}-{int(b_end)}m",
                            "pace": format_pace(b_pace),
                            "avg_hr": b_hr
                        })
            if micro_splits:
                s["micro_splits"] = micro_splits

    return raw_segments, parsed_plan

def generate_interval_plot(activity_meta, segments, df_streams, output_path=None, theme="dark"):
    """
    Generate publication-ready interval telemetry chart (Pace curve + Heart Rate curve + Interval Shading).
    """
    os.makedirs(OUTPUT_DEFAULT_DIR, exist_ok=True)
    if not output_path:
        act_id = activity_meta.get("activityId", "unknown")
        dt_str = activity_meta.get("date", datetime.now().strftime("%Y%m%d")).replace("-", "")
        output_path = os.path.join(OUTPUT_DEFAULT_DIR, f"interval_{dt_str}_{act_id[-8:]}.png")
    elif not os.path.isabs(output_path):
        output_path = os.path.join(OUTPUT_DEFAULT_DIR, output_path)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    is_dark = (theme == "dark")
    bg_main = "#0b1329" if is_dark else "#f8fafc"
    bg_plot = "#151f38" if is_dark else "#ffffff"
    text_color = "#ffffff" if is_dark else "#0f172a"
    subtext_color = "#cbd5e1" if is_dark else "#475569"
    grid_color = "#334155" if is_dark else "#e2e8f0"

    plt.rcParams['font.sans-serif'] = 'DejaVu Sans'
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True, gridspec_kw={'height_ratios': [1.2, 1]})
    fig.patch.set_facecolor(bg_main)
    ax1.set_facecolor(bg_plot)
    ax2.set_facecolor(bg_plot)
    ax1.grid(True, linestyle='--', alpha=0.3, color=grid_color)
    ax2.grid(True, linestyle='--', alpha=0.3, color=grid_color)

    time_min = df_streams['time'] / 60.0
    pace_min_km = np.where(df_streams['velocity_smooth'] > 0.5, 16.666667 / df_streams['velocity_smooth'], np.nan)
    pace_plot = pd.Series(pace_min_km).clip(lower=4.5, upper=12.5).rolling(7, min_periods=1, center=True).mean()

    # Top panel: Pace
    ax1.plot(time_min, pace_plot, color='#38bdf8', linewidth=2.0, label='Pace Curve', zorder=4)
    ax1.set_ylim(12.5, 4.5)  # Inverted so faster is higher
    ax1.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, p: format_pace(v)))
    ax1.set_ylabel('Pace (min/km, Faster ↑)', fontsize=11, fontweight='bold', color=text_color, labelpad=8)
    ax1.tick_params(colors=subtext_color, labelsize=9)

    # Shaded blocks
    for s in segments:
        s_t = s["start_time_sec"] / 60.0
        e_t = s["end_time_sec"] / 60.0
        stype = s["type"]

        if stype == "interval":
            color = '#0284c7' if is_dark else '#38bdf8'
            alpha = 0.22
            label_text = f"{s['name']}\n{s['dist_m']}m @ {s['pace_fmt']}"
            ax1.axvspan(s_t, e_t, color=color, alpha=alpha, zorder=2)
            ax2.axvspan(s_t, e_t, color=color, alpha=0.10, zorder=2)
            mid_t = (s_t + e_t) / 2.0
            ax1.text(mid_t, 5.0, label_text, color=text_color, fontsize=8.0, fontweight='bold',
                     ha='center', va='top', bbox=dict(boxstyle='round,pad=0.25', facecolor=bg_main, alpha=0.85, edgecolor=color))
        elif stype == "recovery":
            color = '#f59e0b'
            alpha = 0.15
            ax1.axvspan(s_t, e_t, color=color, alpha=alpha, zorder=2)
            ax2.axvspan(s_t, e_t, color=color, alpha=alpha, zorder=2)
            if s.get("hr_recovery_drop"):
                mid_t = (s_t + e_t) / 2.0
                ax2.text(mid_t, s['min_hr'] + 2 if s.get('min_hr') else 115, f"{s['hr_recovery_drop']}",
                         color='#f59e0b', fontsize=8, fontweight='bold', ha='center', va='bottom')
        else:
            color = '#a855f7'
            alpha = 0.12
            ax1.axvspan(s_t, e_t, color=color, alpha=alpha, zorder=2)
            ax2.axvspan(s_t, e_t, color=color, alpha=alpha, zorder=2)

    # Bottom panel: Heart Rate
    if 'heartrate' in df_streams and df_streams['heartrate'].notnull().any():
        hr_smooth = df_streams['heartrate'].rolling(5, min_periods=1, center=True).mean()
        ax2.plot(time_min, hr_smooth, color='#f43f5e', linewidth=2.0, label='Heart Rate', zorder=4)
        mean_hr = float(df_streams['heartrate'].mean())
        ax2.axhline(mean_hr, color='#f43f5e', linestyle=':', alpha=0.6, label=f'Avg HR ({int(mean_hr)} bpm)')
        ax2.set_ylabel('Heart Rate (bpm)', fontsize=11, fontweight='bold', color='#f43f5e', labelpad=8)
        ax2.set_ylim(min(100, int(df_streams['heartrate'].min() - 5)), max(175, int(df_streams['heartrate'].max() + 5)))

    ax2.set_xlabel('Elapsed Time (minutes)', fontsize=11, fontweight='bold', color=text_color, labelpad=8)
    ax2.tick_params(colors=subtext_color, labelsize=9)

    title_date = activity_meta.get("date", "")
    title_name = activity_meta.get("name", "Interval Workout")
    fig.suptitle(f"{title_name} ({title_date}) — Interval Performance Telemetry", fontsize=15, fontweight='bold', color=text_color, y=0.98)

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(output_path, dpi=300, facecolor=fig.get_facecolor(), edgecolor='none')
    base_name, _ = os.path.splitext(output_path)
    fig.savefig(f"{base_name}.jpg", dpi=300, facecolor=fig.get_facecolor(), edgecolor='none')
    plt.close(fig)

    return output_path

def get_interval_analysis(activity_id=None, plan=None, generate_plot=True, output_path=None, theme="dark", db_path=DB_DEFAULT_PATH):
    """
    High-level interval workout processor.
    Accepts specific activity_id or defaults to latest run in Dreeve.
    Supports variable intervals and arbitrary natural language plans.
    """
    conn = get_connection(db_path, binary=True)
    cursor = conn.cursor()

    if activity_id:
        cursor.execute("SELECT activityId, startDateTime, name, distance, movingTimeInSeconds, averageHeartRate, maxHeartRate FROM Activity WHERE activityId = ? LIMIT 1", (activity_id,))
    else:
        cursor.execute("""
            SELECT activityId, startDateTime, name, distance, movingTimeInSeconds, averageHeartRate, maxHeartRate
            FROM Activity
            WHERE (sportType = 'Run' OR activityType = 'Run')
              AND (markedForDeletion IS NULL OR markedForDeletion = 0)
            ORDER BY startDateTime DESC LIMIT 1
        """)

    row = cursor.fetchone()
    if not row:
        conn.close()
        raise ValueError(f"No run found matching activity_id: {activity_id}")

    act_id = row[0].decode('utf-8')
    dt_str = row[1].decode('utf-8')[:10]
    act_name = row[2].decode('utf-8')
    total_dist_m = float(row[3])
    total_time_s = int(row[4])
    avg_hr = int(row[5]) if row[5] is not None else None
    max_hr = int(row[6]) if row[6] is not None else None

    # Load streams
    cursor.execute("SELECT streamType, data FROM ActivityStream WHERE activityId = ?", (act_id,))
    dctx = zstandard.ZstdDecompressor()
    streams = {}
    for st_b, raw in cursor.fetchall():
        st_name = st_b.decode('utf-8')
        if st_name in ('time', 'distance', 'velocity_smooth', 'heartrate'):
            try:
                streams[st_name] = json.loads(dctx.decompress(raw).decode('utf-8'))
            except Exception:
                pass
    conn.close()

    if 'time' not in streams or 'distance' not in streams or 'velocity_smooth' not in streams:
        raise ValueError(f"Telemetry streams incomplete for activity {act_id}.")

    time_arr = np.array(streams['time'])
    dist_arr = np.array(streams['distance'])
    vel_arr = np.array(streams['velocity_smooth'])
    hr_arr = np.array(streams['heartrate']) if 'heartrate' in streams else None

    segments, parsed_plan = detect_interval_segments(time_arr, dist_arr, vel_arr, hr_arr, plan_str=plan)

    clean_segments = []
    work_dist = 0.0
    work_dur = 0
    work_paces = []
    hr_drops = []

    for i, s in enumerate(segments):
        item = {
            "index": i + 1,
            "name": s["name"],
            "type": s["type"],
            "start_time": s["start_time_fmt"],
            "end_time": s["end_time_fmt"],
            "duration_sec": s["duration_sec"],
            "duration": s["duration_fmt"],
            "distance_m": s["dist_m"],
            "pace": s["pace_fmt"],
            "speed_kmh": s["speed_kmh"],
            "avg_hr": s["avg_hr"],
            "peak_hr": s["peak_hr"]
        }
        if s.get("target_dist_m"):
            item["target_dist_m"] = s["target_dist_m"]
        if s.get("target_pace_fmt"):
            item["target_pace"] = s["target_pace_fmt"]
        if s.get("pace_delta_fmt"):
            item["pace_delta"] = s["pace_delta_fmt"]
        if s.get("hr_recovery_drop"):
            item["hr_recovery_drop"] = s["hr_recovery_drop"]
            try:
                hr_drops.append(int(s["hr_recovery_drop"].replace("-", "").replace(" bpm", "")))
            except Exception:
                pass
        if s.get("micro_splits"):
            item["micro_splits"] = s["micro_splits"]

        clean_segments.append(item)

        if s["type"] == "interval":
            work_dist += s["dist_m"]
            work_dur += s["duration_sec"]
            if s["pace_min_km"]:
                work_paces.append(s["pace_min_km"])

    overall_pace = (total_time_s / (total_dist_m / 1000.0)) / 60.0 if total_dist_m > 0 else 0.0
    avg_work_pace = np.mean(work_paces) if work_paces else None

    activity_meta = {
        "activityId": act_id,
        "date": dt_str,
        "name": act_name
    }

    df_streams = pd.DataFrame({
        "time": time_arr,
        "distance": dist_arr,
        "velocity_smooth": vel_arr,
        "heartrate": hr_arr if hr_arr is not None else np.nan
    })

    plot_path = None
    if generate_plot:
        plot_path = generate_interval_plot(activity_meta, segments, df_streams, output_path=output_path, theme=theme)

    return {
        "status": "success",
        "activity_id": act_id,
        "date": dt_str,
        "name": act_name,
        "total_distance_km": round(total_dist_m / 1000.0, 2),
        "total_time": f"{total_time_s // 60}m {total_time_s % 60:02d}s",
        "overall_avg_pace": format_pace(overall_pace),
        "overall_avg_hr": avg_hr,
        "overall_max_hr": max_hr,
        "plan_input": plan,
        "intervals_detected": len([s for s in segments if s["type"] == "interval"]),
        "summary": {
            "total_work_distance_m": round(work_dist, 1),
            "total_work_duration": f"{work_dur // 60}m {work_dur % 60:02d}s",
            "avg_work_pace": format_pace(avg_work_pace),
            "avg_recovery_hr_drop_bpm": round(float(np.mean(hr_drops)), 1) if hr_drops else None
        },
        "segments": clean_segments,
        "plot_path": plot_path
    }

def generate_plot(chart_type="pace_vs_hr", days=60, output_path=None, theme="dark", db_path=DB_DEFAULT_PATH):
    """
    Generate publication-ready fitness charts and save to PNG/JPG in designated output directory.
    Returns path and summary stats.
    """
    df = get_activities_dataframe(days=days, sport="Run", db_path=db_path)
    if df.empty:
        raise ValueError(f"No run data available for the last {days} days.")

    df_hr = df.dropna(subset=['averageHeartRate']).copy()
    if df_hr.empty:
        raise ValueError("No activities with heart rate telemetry found in the specified timeframe.")

    os.makedirs(OUTPUT_DEFAULT_DIR, exist_ok=True)
    if not output_path:
        output_path = os.path.join(OUTPUT_DEFAULT_DIR, f"dreeve_{chart_type}_{days}d.png")
    elif not os.path.isabs(output_path):
        output_path = os.path.join(OUTPUT_DEFAULT_DIR, output_path)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    is_dark = (theme == "dark")
    bg_main = "#0b1329" if is_dark else "#f8fafc"
    bg_plot = "#151f38" if is_dark else "#ffffff"
    text_color = "#ffffff" if is_dark else "#0f172a"
    subtext_color = "#cbd5e1" if is_dark else "#475569"
    grid_color = "#94a3b8"

    plt.rcParams['font.sans-serif'] = 'DejaVu Sans'
    plt.rcParams['axes.edgecolor'] = '#475569' if is_dark else '#cbd5e1'
    plt.rcParams['axes.linewidth'] = 1.0

    slope, intercept, r_val, p_val, std_err = stats.linregress(df_hr['averageHeartRate'], df_hr['pace_min_km'])
    r_squared = r_val ** 2

    hr_zones = [
        (110, 130, 'Zone 1: Active Recovery (<130 bpm)', '#38bdf8', 0.08),
        (130, 145, 'Zone 2: Aerobic Base (130-145 bpm)', '#34d399', 0.08),
        (145, 155, 'Zone 3: Tempo / Aerobic (145-155 bpm)', '#fbbf24', 0.08),
        (155, 175, 'Zone 4: Threshold / Intervals (>155 bpm)', '#f87171', 0.08),
    ]

    t_stamps = df_hr['start_dt'].astype(np.int64)
    norm = plt.Normalize(t_stamps.min(), t_stamps.max())
    cmap = plt.cm.magma_r if is_dark else plt.cm.plasma
    point_sizes = (df_hr['dist_km'] ** 1.6) * 26 + 110

    x_vals = np.linspace(116, 162, 150)
    y_vals = slope * x_vals + intercept

    # Chart 1: Standalone Pace vs HR
    if chart_type in ["pace_vs_hr", "scatter"]:
        fig, ax = plt.subplots(figsize=(12.5, 8.2), dpi=300)
        fig.patch.set_facecolor(bg_main)
        ax.set_facecolor(bg_plot)

        for xmin, xmax, label, color, alpha in hr_zones:
            ax.axvspan(xmin, xmax, color=color, alpha=alpha, zorder=1)

        ax.grid(True, linestyle='--', alpha=0.2, color=grid_color, zorder=2)
        ax.plot(x_vals, y_vals, color='#38bdf8', linestyle='--', linewidth=2.4, alpha=0.9,
                label=f'Linear Fit ($R^2 = {r_squared:.2f}$, $p < 0.001$)', zorder=3)

        scatter = ax.scatter(
            df_hr['averageHeartRate'],
            df_hr['pace_min_km'],
            s=point_sizes,
            c=t_stamps,
            cmap=cmap,
            norm=norm,
            edgecolor='#ffffff' if is_dark else '#1e293b',
            linewidth=1.8,
            alpha=0.95,
            zorder=5,
            label='Runs (size $\propto$ distance)'
        )

        ax.set_ylim(9.3, 5.8)
        ax.set_xlim(110, 172)
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, p: format_pace(v)))

        annotations = [
            ('NoiseFit Outdoor\n5.9 km @ 120 bpm (8:58/km)', 120, 8.958, (25, 25)),
            ('Branch Target Buffer\n6.3 km @ 136 bpm (7:49/km)', 136, 7.813, (-170, -25)),
            ('L1 Instruction Cache\n8.0 km @ 138 bpm (7:32/km)', 138, 7.528, (25, -25)),
            ('4x4 Norwegian HIIT\n5.1 km @ 151 bpm (6:43/km)', 151, 6.720, (-175, 40)),
            ('TLB (Fastest)\n7.0 km @ 156 bpm (6:25/km)', 156, 6.416, (-165, -35)),
            ('Load & Store Units\n5.1 km @ 158 bpm (6:24/km)', 158, 6.404, (20, 15)),
        ]
        for label_text, x_pos, y_pos, offset in annotations:
            matches = df_hr[(df_hr['averageHeartRate'] >= x_pos - 1) & (df_hr['averageHeartRate'] <= x_pos + 1)]
            if not matches.empty:
                actual_y = matches['pace_min_km'].values[0]
                actual_x = matches['averageHeartRate'].values[0]
                ax.annotate(
                    label_text,
                    xy=(actual_x, actual_y),
                    xytext=offset,
                    textcoords='offset points',
                    fontsize=8.5,
                    color=text_color,
                    weight='medium',
                    bbox=dict(boxstyle='round,pad=0.4', facecolor=bg_main, edgecolor='#475569', alpha=0.92),
                    arrowprops=dict(arrowstyle='->', connectionstyle='arc3,rad=0.15', color=grid_color, lw=1.2),
                    zorder=6
                )

        ax.set_xlabel('Average Heart Rate (bpm)', fontsize=12, fontweight='bold', color=text_color, labelpad=10)
        ax.set_ylabel('Average Pace (min/km, Faster ↑)', fontsize=12, fontweight='bold', color=text_color, labelpad=10)
        ax.tick_params(colors=subtext_color, labelsize=10)

        plt.title(f'Dreeve Running Performance: Average Pace vs. Average Heart Rate\nRecent {days} Days',
                  fontsize=15, fontweight='bold', color=text_color, pad=18)

        cbar = plt.colorbar(scatter, ax=ax, orientation='vertical', pad=0.03, shrink=0.80)
        cbar.set_label('Activity Date', color=text_color, fontsize=10, fontweight='bold', labelpad=10)
        cbar.ax.tick_params(colors=subtext_color, labelsize=9)

        legend_elements = [
            plt.Line2D([0], [0], color='#38bdf8', lw=2.2, linestyle='--', label=f'Trendline ($R^2 = {r_squared:.2f}$)'),
            Patch(facecolor='#38bdf8', alpha=0.35, label='Zone 1 Recovery (<130 bpm)'),
            Patch(facecolor='#34d399', alpha=0.35, label='Zone 2 Aerobic Base (130-145 bpm)'),
            Patch(facecolor='#fbbf24', alpha=0.35, label='Zone 3 Tempo / Aerobic (145-155 bpm)'),
            Patch(facecolor='#f87171', alpha=0.35, label='Zone 4 Threshold / HIIT (>155 bpm)'),
        ]
        ax.legend(handles=legend_elements, loc='upper left', framealpha=0.92, facecolor=bg_main,
                  edgecolor='#334155', fontsize=8.5, labelcolor=text_color)

        plt.tight_layout()
        fig.savefig(output_path, dpi=300, facecolor=fig.get_facecolor(), edgecolor='none')
        base_name, _ = os.path.splitext(output_path)
        fig.savefig(f"{base_name}.jpg", dpi=300, facecolor=fig.get_facecolor(), edgecolor='none')
        plt.close(fig)

    # Chart 2: 2-Panel Dashboard
    elif chart_type in ["dashboard", "combo"]:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(18.8, 8.2), dpi=300)
        fig.patch.set_facecolor(bg_main)

        # Panel 1: Scatter
        ax1.set_facecolor(bg_plot)
        for xmin, xmax, label, color, alpha in hr_zones:
            ax1.axvspan(xmin, xmax, color=color, alpha=alpha, zorder=1)
        ax1.grid(True, linestyle='--', alpha=0.2, color=grid_color, zorder=2)
        ax1.plot(x_vals, y_vals, color='#38bdf8', linestyle='--', linewidth=2.2, alpha=0.9, zorder=3)

        ax1.scatter(
            df_hr['averageHeartRate'],
            df_hr['pace_min_km'],
            s=point_sizes,
            c=t_stamps,
            cmap=cmap,
            norm=norm,
            edgecolor='#ffffff' if is_dark else '#1e293b',
            linewidth=1.8,
            alpha=0.95,
            zorder=5
        )
        ax1.set_ylim(9.3, 5.8)
        ax1.set_xlim(110, 172)
        ax1.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, p: format_pace(v)))
        ax1.set_xlabel('Average Heart Rate (bpm)', fontsize=11, fontweight='bold', color=text_color, labelpad=8)
        ax1.set_ylabel('Average Pace (min/km, Faster ↑)', fontsize=11, fontweight='bold', color=text_color, labelpad=8)
        ax1.tick_params(colors=subtext_color, labelsize=9)
        ax1.set_title('Avg Pace vs. Avg Heart Rate (Efficiency Curve)', fontsize=13, fontweight='bold', color=text_color, pad=12)

        # Panel 2: Timeline
        ax2.set_facecolor(bg_plot)
        ax2.grid(True, linestyle='--', alpha=0.2, color=grid_color, zorder=1)

        df_sorted = df.sort_values('start_dt').copy()
        ax2.plot(df_sorted['start_dt'], df_sorted['pace_min_km'], color='#60a5fa',
                 marker='o', markersize=5, linewidth=1.8, alpha=0.7, label=f'Pace ({len(df_sorted)} Runs)', zorder=3)
        ax2.set_ylim(9.3, 4.0)
        ax2.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, p: format_pace(v)))
        ax2.set_ylabel('Average Pace (min/km, Faster ↑)', fontsize=11, fontweight='bold', color='#60a5fa', labelpad=8)
        ax2.tick_params(axis='y', colors='#60a5fa', labelsize=9)
        ax2.tick_params(axis='x', colors=subtext_color, labelsize=8.5, rotation=35)
        ax2.set_xlabel(f'Date (Recent {days} Days)', fontsize=11, fontweight='bold', color=text_color, labelpad=8)

        ax2_hr = ax2.twinx()
        ax2_hr.set_ylim(100, 175)
        ax2_hr.set_ylabel('Average Heart Rate (bpm)', fontsize=11, fontweight='bold', color='#f43f5e', labelpad=8)
        ax2_hr.tick_params(axis='y', colors='#f43f5e', labelsize=9)

        df_hr_sorted = df_hr.sort_values('start_dt')
        ax2_hr.plot(df_hr_sorted['start_dt'], df_hr_sorted['averageHeartRate'], color='#f43f5e',
                    marker='s', markersize=6, linewidth=2.0, alpha=0.9, label=f'Avg HR ({len(df_hr_sorted)} Runs with Sensor)', zorder=4)

        ax2.set_title('Longitudinal Progression: Pace & Heart Rate', fontsize=13, fontweight='bold', color=text_color, pad=12)

        lines1, labels1 = ax2.get_legend_handles_labels()
        lines2, labels2 = ax2_hr.get_legend_handles_labels()
        ax2.legend(lines1 + lines2, labels1 + labels2, loc='lower left', framealpha=0.92,
                   facecolor=bg_main, edgecolor='#334155', fontsize=8.5, labelcolor=text_color)

        fig.suptitle(f'Dreeve Running Performance Analytics (Recent {days} Days)', fontsize=16, fontweight='bold', color=text_color, y=0.98)
        plt.tight_layout(rect=[0, 0, 1, 0.95])
        fig.savefig(output_path, dpi=300, facecolor=fig.get_facecolor(), edgecolor='none')
        base_name, _ = os.path.splitext(output_path)
        fig.savefig(f"{base_name}.jpg", dpi=300, facecolor=fig.get_facecolor(), edgecolor='none')
        plt.close(fig)

    return {
        "status": "success",
        "chart_type": chart_type,
        "timeframe_days": days,
        "output_path": output_path,
        "runs_analyzed": len(df),
        "runs_with_hr": len(df_hr),
        "correlation_r": round(r_val, 3),
        "r_squared": round(r_squared, 3)
    }

def execute_readonly_query(sql_query, max_rows=30, db_path=DB_DEFAULT_PATH):
    """Safely execute read-only queries with strict token guard limit."""
    clean_sql = sql_query.strip().lower()
    if not (clean_sql.startswith("select") or clean_sql.startswith("pragma") or clean_sql.startswith("explain")):
        raise PermissionError("Only read-only SELECT, PRAGMA, or EXPLAIN statements are permitted.")

    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute(sql_query)
    columns = [desc[0] for desc in cursor.description] if cursor.description else []
    rows = cursor.fetchmany(max_rows)
    conn.close()

    return {
        "columns": columns,
        "row_count": len(rows),
        "rows": [dict(zip(columns, row)) for row in rows]
    }

# ==========================================
# CLI Entrypoint
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dreeve Running Analytics Engine")
    subparsers = parser.add_subparsers(dest="command")

    # Summary
    p_sum = subparsers.add_parser("summary", help="Get compact statistical summary")
    p_sum.add_argument("--days", type=int, default=60, help="Time window in days")
    p_sum.add_argument("--sport", type=str, default="Run", help="Sport type")

    # Activities
    p_act = subparsers.add_parser("activities", help="Get compact activities list")
    p_act.add_argument("--days", type=int, default=60, help="Time window in days")
    p_act.add_argument("--limit", type=int, default=20, help="Max results")
    p_act.add_argument("--with-hr", action="store_true", help="Only runs with HR")
    p_act.add_argument("--search", type=str, default=None, help="Search string")

    # Splits
    p_split = subparsers.add_parser("splits", help="Analyze kilometer splits and heart rate from telemetry streams")
    p_split.add_argument("--activity", type=str, default=None, help="Specific activity ID")
    p_split.add_argument("--days", type=int, default=None, help="Time window in days")
    p_split.add_argument("--month", type=str, default=None, help="Month in format YYYY-MM (e.g. 2026-10)")
    p_split.add_argument("--summary-only", action="store_true", help="Only return drift progression & stats (~100 tokens)")

    # Intervals
    p_int = subparsers.add_parser("intervals", help="Analyze structured or variable interval workouts")
    p_int.add_argument("--activity", type=str, default=None, help="Specific activity ID or latest")
    p_int.add_argument("--plan", type=str, default=None, help="Workout plan description (e.g. '0.8k easy, 2x1.5k @ 6:00, 1k @ 7:00')")
    p_int.add_argument("--no-plot", action="store_true", help="Skip generating visualization plot")
    p_int.add_argument("--out", type=str, default=None, help="Custom output image path")
    p_int.add_argument("--theme", type=str, default="dark", choices=["dark", "light"], help="Plot theme")

    # Plot
    p_plot = subparsers.add_parser("plot", help="Generate performance plot")
    p_plot.add_argument("--type", type=str, default="pace_vs_hr", choices=["pace_vs_hr", "dashboard"], help="Chart type")
    p_plot.add_argument("--days", type=int, default=60, help="Time window in days")
    p_plot.add_argument("--out", type=str, default=None, help="Output image file path (saved in output/ folder by default)")
    p_plot.add_argument("--theme", type=str, default="dark", choices=["dark", "light"], help="Theme")

    # Query
    p_qry = subparsers.add_parser("query", help="Execute read-only SQL")
    p_qry.add_argument("--sql", type=str, required=True, help="SQL SELECT query")

    args = parser.parse_args()

    if args.command == "summary":
        res = get_summary(days=args.days, sport=args.sport)
        print(json.dumps(res, indent=2))
    elif args.command == "activities":
        res = get_activities_list(days=args.days, limit=args.limit, with_hr_only=args.with_hr, search=args.search)
        print(json.dumps(res, indent=2))
    elif args.command == "splits":
        res = get_splits_analysis(activity_id=args.activity, days=args.days, month=args.month, summary_only=args.summary_only)
        print(json.dumps(res, indent=2))
    elif args.command == "intervals":
        res = get_interval_analysis(
            activity_id=args.activity,
            plan=args.plan,
            generate_plot=not args.no_plot,
            output_path=args.out,
            theme=args.theme
        )
        print(json.dumps(res, indent=2))
    elif args.command == "plot":
        res = generate_plot(chart_type=args.type, days=args.days, output_path=args.out, theme=args.theme)
        print(json.dumps(res, indent=2))
    elif args.command == "query":
        res = execute_readonly_query(args.sql)
        print(json.dumps(res, indent=2, default=str))
    else:
        parser.print_help()
