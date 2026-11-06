#!/usr/bin/env bash
# Standalone Strava to SQLite Database Sync Utility for Running Chatbot
import os
import sys
import json
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime
import zstandard

DB_PATH = os.environ.get("RUNNING_DB_PATH", os.environ.get("DREEVE_DB_PATH", "running.db"))
CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID")
CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")
REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN")

def init_db(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS Activity (
        activityId VARCHAR(255) PRIMARY KEY,
        startDateTime DATETIME NOT NULL,
        sportType VARCHAR(255) NOT NULL,
        name VARCHAR(255) NOT NULL,
        distance INTEGER NOT NULL,
        movingTimeInSeconds INTEGER NOT NULL,
        averageSpeed DOUBLE PRECISION NOT NULL,
        maxSpeed DOUBLE PRECISION DEFAULT 0.0,
        averageHeartRate INTEGER DEFAULT NULL,
        maxHeartRate INTEGER DEFAULT NULL,
        elevation INTEGER DEFAULT 0,
        calories INTEGER DEFAULT NULL,
        markedForDeletion BOOLEAN DEFAULT 0
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ActivityStream (
        activityId VARCHAR(255) NOT NULL,
        streamType VARCHAR(255) NOT NULL,
        createdOn DATETIME NOT NULL,
        dataSize INTEGER DEFAULT 0,
        data BLOB DEFAULT NULL,
        PRIMARY KEY (activityId, streamType)
    )
    """)
    conn.commit()
    conn.close()

def get_access_token(client_id, client_secret, refresh_token):
    url = "https://www.strava.com/oauth/token"
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }).encode("utf-8")
    
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        return res["access_token"]

def fetch_strava_activities(access_token, per_page=30):
    url = f"https://www.strava.com/api/v3/athlete/activities?per_page={per_page}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))

def fetch_activity_streams(access_token, activity_id):
    types = "time,distance,heartrate,cadence,altitude"
    url = f"https://www.strava.com/api/v3/activities/{activity_id}/streams?keys={types}&key_by_type=true"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}

def sync(limit=30):
    if not CLIENT_ID or not CLIENT_SECRET or not REFRESH_TOKEN:
        print("❌ Error: Missing STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, or STRAVA_REFRESH_TOKEN.")
        print("Set them in your .env or export them in your terminal.")
        sys.exit(1)

    print("🚀 Initializing local database at:", DB_PATH)
    init_db(DB_PATH)

    print("🔑 Authenticating with Strava API v3...")
    token = get_access_token(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN)

    print(f"📥 Fetching recent {limit} activities...")
    acts = fetch_strava_activities(token, per_page=limit)
    runs = [a for a in acts if a.get("type") in ("Run", "TrailRun", "VirtualRun")]
    print(f"Found {len(runs)} running activities out of {len(acts)} total workouts.")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cctx = zstandard.ZstdCompressor(level=3)

    for i, a in enumerate(runs, 1):
        act_id = f"strava-{a['id']}"
        dt = a["start_date_local"].replace("T", " ").replace("Z", "")
        name = a["name"]
        dist = int(a["distance"])
        mov_time = int(a["moving_time"])
        avg_speed = float(a.get("average_speed", 0.0))
        max_speed = float(a.get("max_speed", 0.0))
        avg_hr = int(round(a["average_heartrate"])) if "average_heartrate" in a else None
        max_hr = int(round(a["max_heartrate"])) if "max_heartrate" in a else None
        elev = int(round(a.get("total_elevation_gain", 0)))
        cals = int(round(a["kilojoules"] * 0.239)) if "kilojoules" in a else None

        cur.execute("""
        INSERT OR REPLACE INTO Activity 
        (activityId, startDateTime, sportType, name, distance, movingTimeInSeconds, averageSpeed, maxSpeed, averageHeartRate, maxHeartRate, elevation, calories)
        VALUES (?, ?, 'Run', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (act_id, dt, name, dist, mov_time, avg_speed, max_speed, avg_hr, max_hr, elev, cals))

        # Fetch detailed sensor streams
        print(f"[{i}/{len(runs)}] Syncing streams for: {dt} | {name} ({dist/1000.0:.2f} km)...")
        streams = fetch_activity_streams(token, a["id"])

        for st_name, st_obj in streams.items():
            raw_data = st_obj.get("data", [])
            if raw_data:
                comp_data = cctx.compress(json.dumps(raw_data).encode("utf-8"))
                cur.execute("""
                INSERT OR REPLACE INTO ActivityStream
                (activityId, streamType, createdOn, dataSize, data)
                VALUES (?, ?, ?, ?, ?)
                """, (act_id, st_name, dt, len(comp_data), comp_data))

    conn.commit()
    conn.close()
    print("✅ Sync complete! Your local running database is ready.")

if __name__ == "__main__":
    sync()
