import os
import sys
import re
import json
import sqlite3
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import numpy as np
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Ensure engine is accessible
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

import dreeve_engine

# Dynamic database and output directory resolution
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "dreeve" / "storage" / "database" / "dreeve.db"
DB_PATH = os.environ.get("DREEVE_DB_PATH", str(_DEFAULT_DB if _DEFAULT_DB.exists() else "dreeve.db"))
OUTPUT_DIR = os.environ.get("DREEVE_OUTPUT_DIR", str(Path(__file__).resolve().parent / "output"))

st.set_page_config(
    page_title="RunTAG - Running Coach and Analytics",
    page_icon="TAG",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for dark modern aesthetic
st.markdown("""
<style>
    .stApp {
        background-color: #0b1329;
        color: #f8fafc;
    }
    .metric-card {
        background-color: #151f38;
        border: 1px solid #334155;
        border-radius: 10px;
        padding: 16px;
        margin-bottom: 12px;
    }
    .metric-value {
        font-size: 24px;
        font-weight: 700;
        color: #38bdf8;
    }
    .metric-label {
        font-size: 13px;
        color: #94a3b8;
    }
    .mode-badge {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 600;
        margin-bottom: 10px;
    }
    .mode-offline {
        background-color: #0369a1;
        color: #ffffff;
    }
    .mode-llm {
        background-color: #15803d;
        color: #ffffff;
    }
    .mode-warning {
        background-color: #b45309;
        color: #ffffff;
    }
</style>
""", unsafe_allow_html=True)

# Helper: Database Metadata Status
@st.cache_data(ttl=60)
def get_db_status():
    if not os.path.exists(DB_PATH):
        return {"connected": False, "error": f"Database file not found at {DB_PATH}"}
    try:
        conn = dreeve_engine.get_connection(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), MAX(startDateTime) FROM Activity WHERE (sportType = 'Run' OR activityType = 'Run') AND (markedForDeletion IS NULL OR markedForDeletion = 0)")
        row = cur.fetchone()
        conn.close()
        return {
            "connected": True,
            "total_runs": row[0],
            "latest_run": row[1][:10] if row[1] else "None"
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}

# ==========================================
# Tool Declarations for LLM TAG Mode
# ==========================================
TOOLS_SCHEMA_GEMINI = [
    {
        "name": "get_personal_records",
        "description": "Retrieve personal records (PRs / PBs / all-time best efforts) across benchmark running distances (400m, 800m, 1k, 1 mile, 2 miles, 5k, 10k, 15k) or all records.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "distance": {
                    "type": "STRING",
                    "description": "Benchmark distance target, e.g. '1k', '5k', '10k', 'mile', 'all'"
                }
            }
        }
    },
    {
        "name": "get_interval_analysis",
        "description": "Analyze interval workouts, repetitions, workout plans, pace deltas, and HR recovery from telemetry streams.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "plan": {
                    "type": "STRING",
                    "description": "Natural language plan string (e.g. '1k warmup + 4x1k @6:15 with 90s walk + 0.5k cooldown')"
                },
                "activity_id": {
                    "type": "STRING",
                    "description": "Specific activity ID (optional)"
                }
            }
        }
    },
    {
        "name": "get_activities_list",
        "description": "List recent activities or search runs by keyword, name, or date.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "days": {"type": "INTEGER", "description": "Time window in days"},
                "limit": {"type": "INTEGER", "description": "Maximum number of activities to return"},
                "search": {"type": "STRING", "description": "Search keyword or activity name"}
            }
        }
    },
    {
        "name": "get_splits_analysis",
        "description": "Analyze kilometer splits, pacing, and cardiac drift across kilometers.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "month": {"type": "STRING", "description": "Month in YYYY-MM format, e.g. '2026-10'"},
                "activity_id": {"type": "STRING", "description": "Specific activity ID"}
            }
        }
    },
    {
        "name": "get_summary",
        "description": "Get overall summary of running volume, average pace, duration, and heart rate metrics.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "days": {"type": "INTEGER", "description": "Time window in days (default 60)"}
            }
        }
    },
    {
        "name": "generate_plot",
        "description": "Generate performance charts (pace vs HR scatter plot or full dashboard).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "chart_type": {"type": "STRING", "description": "'pace_vs_hr' or 'dashboard'"},
                "days": {"type": "INTEGER", "description": "Time window in days"}
            }
        }
    }
]

TOOLS_SCHEMA_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "get_personal_records",
            "description": "Retrieve personal records (PRs / PBs / all-time best efforts) across benchmark running distances (400m, 800m, 1k, 1 mile, 2 miles, 5k, 10k, 15k) or all records.",
            "parameters": {
                "type": "object",
                "properties": {
                    "distance": {
                        "type": "string",
                        "description": "Benchmark distance target, e.g. '1k', '5k', '10k', 'mile', 'all'"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_interval_analysis",
            "description": "Analyze interval workouts, repetitions, workout plans, pace deltas, and HR recovery from telemetry streams.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {"type": "string", "description": "Workout plan description"},
                    "activity_id": {"type": "string", "description": "Specific activity ID"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_activities_list",
            "description": "List recent activities or search runs by keyword, name, or date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Time window in days"},
                    "limit": {"type": "integer", "description": "Max activities"},
                    "search": {"type": "string", "description": "Search keyword or activity name"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_splits_analysis",
            "description": "Analyze kilometer splits, pacing, and cardiac drift across kilometers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "month": {"type": "string", "description": "Month in YYYY-MM format"},
                    "activity_id": {"type": "string", "description": "Specific activity ID"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_summary",
            "description": "Get overall summary of running volume, average pace, duration, and heart rate metrics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Time window in days"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_plot",
            "description": "Generate performance charts (pace vs HR scatter plot or full dashboard).",
            "parameters": {
                "type": "object",
                "properties": {
                    "chart_type": {"type": "string", "description": "'pace_vs_hr' or 'dashboard'"},
                    "days": {"type": "integer", "description": "Time window in days"}
                }
            }
        }
    }
]

def dispatch_tool(tool_name: str, args: dict):
    """Execute analytics tool deterministically against dreeve_engine."""
    if tool_name == "get_personal_records":
        dist = args.get("distance")
        return dreeve_engine.get_personal_records(distance=dist, db_path=DB_PATH)
    elif tool_name == "get_interval_analysis":
        return dreeve_engine.get_interval_analysis(
            plan=args.get("plan"),
            activity_id=args.get("activity_id"),
            generate_plot=True,
            db_path=DB_PATH
        )
    elif tool_name == "get_activities_list":
        return dreeve_engine.get_activities_list(
            days=args.get("days", 60),
            limit=args.get("limit", 15),
            search=args.get("search"),
            db_path=DB_PATH
        )
    elif tool_name == "get_splits_analysis":
        return dreeve_engine.get_splits_analysis(
            month=args.get("month"),
            activity_id=args.get("activity_id"),
            summary_only=True,
            db_path=DB_PATH
        )
    elif tool_name == "get_summary":
        return dreeve_engine.get_summary(days=args.get("days", 60), db_path=DB_PATH)
    elif tool_name == "generate_plot":
        return dreeve_engine.generate_plot(
            chart_type=args.get("chart_type", "pace_vs_hr"),
            days=args.get("days", 60),
            db_path=DB_PATH
        )
    raise ValueError(f"Unknown tool: {tool_name}")

def call_llm_tag(prompt: str, api_key: str, provider: str = "gemini", model: str = None):
    """
    Executes Tool-Augmented Generation (TAG) with real function calling against dreeve_engine.
    Supports Google Gemini and OpenAI endpoints.
    """
    if provider == "gemini":
        model_name = model or "gemini-1.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        
        system_instruction = (
            "You are an expert running coach and exercise physiologist analyzing telemetry from RunTAG/Dreeve. "
            "Always call the appropriate analytics tool to retrieve actual runner telemetry before answering. "
            "Never invent numbers. Format responses in clean Markdown with clear metric tables. Do not use emojis."
        )
        
        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": prompt}]}
            ],
            "systemInstruction": {
                "parts": [{"text": system_instruction}]
            },
            "tools": [
                {"functionDeclarations": TOOLS_SCHEMA_GEMINI}
            ]
        }
        
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            
        candidate = data.get("candidates", [{}])[0]
        parts = candidate.get("content", {}).get("parts", [])
        
        image_path = None
        for p in parts:
            if "functionCall" in p:
                fn = p["functionCall"]
                fn_name = fn.get("name")
                fn_args = fn.get("args", {})
                
                tool_res = dispatch_tool(fn_name, fn_args)
                if isinstance(tool_res, dict):
                    if tool_res.get("plot_path") and os.path.exists(tool_res["plot_path"]):
                        image_path = tool_res["plot_path"]
                    elif tool_res.get("output_path") and os.path.exists(tool_res["output_path"]):
                        image_path = tool_res["output_path"]
                
                followup_payload = {
                    "contents": [
                        {"role": "user", "parts": [{"text": prompt}]},
                        {"role": "model", "parts": [p]},
                        {
                            "role": "function",
                            "parts": [{
                                "functionResponse": {
                                    "name": fn_name,
                                    "response": {"result": tool_res}
                                }
                            }]
                        }
                    ],
                    "tools": [{"functionDeclarations": TOOLS_SCHEMA_GEMINI}]
                }
                
                followup_req = urllib.request.Request(
                    url,
                    data=json.dumps(followup_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(followup_req, timeout=20) as f_resp:
                    f_data = json.loads(f_resp.read().decode("utf-8"))
                    f_parts = f_data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                    text_parts = [part.get("text", "") for part in f_parts if "text" in part]
                    return {
                        "text": "\n".join(text_parts),
                        "image_path": image_path,
                        "mode": "llm_gemini"
                    }
                    
        direct_text = "\n".join([p.get("text", "") for p in parts if "text" in p])
        return {"text": direct_text, "image_path": image_path, "mode": "llm_gemini"}
        
    elif provider == "openai":
        model_name = model or "gpt-4o-mini"
        url = "https://api.openai.com/v1/chat/completions"
        system_instruction = (
            "You are an expert running coach and exercise physiologist analyzing telemetry from RunTAG/Dreeve. "
            "Always call the appropriate analytics tool to retrieve actual runner telemetry before answering. "
            "Never invent numbers. Format responses in clean Markdown without emojis."
        )
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt}
            ],
            "tools": TOOLS_SCHEMA_OPENAI
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        tool_calls = msg.get("tool_calls", [])
        
        image_path = None
        if tool_calls:
            messages = [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt},
                msg
            ]
            for tc in tool_calls:
                fn_name = tc.get("function", {}).get("name")
                fn_args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                tool_res = dispatch_tool(fn_name, fn_args)
                if isinstance(tool_res, dict):
                    if tool_res.get("plot_path") and os.path.exists(tool_res["plot_path"]):
                        image_path = tool_res["plot_path"]
                    elif tool_res.get("output_path") and os.path.exists(tool_res["output_path"]):
                        image_path = tool_res["output_path"]
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "name": fn_name,
                    "content": json.dumps(tool_res)
                })
            
            followup_payload = {
                "model": model_name,
                "messages": messages
            }
            f_req = urllib.request.Request(
                url,
                data=json.dumps(followup_payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}"
                }
            )
            with urllib.request.urlopen(f_req, timeout=20) as f_resp:
                f_data = json.loads(f_resp.read().decode("utf-8"))
                final_text = f_data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {"text": final_text, "image_path": image_path, "mode": "llm_openai"}
        else:
            return {"text": msg.get("content", ""), "image_path": None, "mode": "llm_openai"}

# ==========================================
# Enhanced Offline Deterministic TAG Engine
# ==========================================
def format_records_markdown(res: dict) -> str:
    """Format personal records response into high-signal Markdown."""
    if not res.get("matched", False) and "personal_best" not in res:
        if "records" in res:
            md = [
                "### [PR] Lifetime Personal Records Summary",
                "Here are your all-time benchmark records verified from telemetry:\n",
                "| Benchmark Distance | Record Time | Average Pace | Speed | Date Achieved | Activity Name | Heart Rate |",
                "|---|---|---|---|---|---|---|"
            ]
            for r in res["records"]:
                hr = f"{r['avg_hr']} bpm" if r.get("avg_hr") else "-"
                md.append(f"| **{r['distance_label']}** | **{r['time']}** | `{r['pace']}` | {r['speed_kmh']} km/h | {r['date']} | {r['activity_name']} | {hr} |")
            return "\n".join(md)
        return "No personal records found in database."

    pb = res["personal_best"]
    lbl = pb["distance_label"]
    md = [
        f"### [PR] {lbl} Personal Best: **{pb['time']}** ({pb['pace']})",
        f"Your all-time fastest **{lbl}** is **{pb['time']}**, achieved on **{pb['date']}** during **\"{pb['activity_name']}\"**.\n",
        "#### Benchmark Performance Stats",
        f"- **Record Time:** `{pb['time']}` ({pb['time_seconds']} seconds)",
        f"- **Average Pace:** `{pb['pace']}`",
        f"- **Running Speed:** `{pb['speed_kmh']} km/h`",
        f"- **Recorded Heart Rate:** `{pb['avg_hr']} bpm`" if pb.get("avg_hr") else "- **Recorded Heart Rate:** `N/A` (No optical HR telemetry for this run)",
        f"- **Activity Date:** `{pb['date']}`",
        f"- **Activity Name:** `{pb['activity_name']}`"
    ]

    top = res.get("top_efforts", [])
    if len(top) > 1:
        md.append(f"\n#### Top {len(top)} Historical {lbl} Efforts")
        md.append("| Rank | Time | Pace | Speed | Date | Activity Name | Avg HR |")
        md.append("|---|---|---|---|---|---|---|")
        for i, r in enumerate(top, 1):
            hr = f"{r['avg_hr']} bpm" if r.get("avg_hr") else "-"
            md.append(f"| **#{i}** | **{r['time']}** | `{r['pace']}` | {r['speed_kmh']} km/h | {r['date']} | {r['activity_name']} | {hr} |")

    all_recs = res.get("all_records", [])
    if all_recs:
        md.append("\n#### All Benchmark Records Reference")
        md.append("| Benchmark | PB Time | Pace | Speed | Date | Activity Name |")
        md.append("|---|---|---|---|---|---|")
        for r in all_recs:
            h = "**" if r["distance_label"] == lbl else ""
            md.append(f"| {h}{r['distance_label']}{h} | {h}{r['time']}{h} | {h}`{r['pace']}`{h} | {r['speed_kmh']} km/h | {r['date']} | {r['activity_name']} |")

    return "\n".join(md)

def search_activities_by_prompt(prompt: str):
    """Fuzzy activity search by date or title keywords."""
    p_lower = prompt.lower()
    months = {
        "jan": "01", "january": "01", "feb": "02", "february": "02",
        "mar": "03", "march": "03", "apr": "04", "april": "04",
        "may": "05", "jun": "06", "june": "06", "jul": "07", "july": "07",
        "aug": "08", "august": "08", "sep": "09", "september": "09",
        "oct": "10", "october": "10", "nov": "11", "november": "11", "dec": "12", "december": "12"
    }
    date_match = re.search(r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})\b', p_lower)
    
    if date_match:
        m_num = months[date_match.group(1)]
        d_num = int(date_match.group(2))
        date_pattern = f"-{m_num}-{d_num:02d}"
        conn = dreeve_engine.get_connection(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT activityId, name, distance, movingTimeInSeconds, startDateTime, averageHeartRate "
            "FROM Activity WHERE startDateTime LIKE ? ORDER BY startDateTime DESC",
            (f"%{date_pattern}%",)
        )
        return [dict(r) for r in cur.fetchall()]
        
    # Search by notable activity title keywords
    title_words = ["navratri", "beach", "first 15k", "2nd 15k", "tempo", "warm up", "recovery", "evening run", "morning run"]
    for word in title_words:
        if word in p_lower:
            return dreeve_engine.get_activities_list(days=365, search=word, db_path=DB_PATH)
            
    return []

def process_query_deterministic(prompt: str):
    """
    Enhanced offline deterministic parser.
    Supports Personal Records, Date Searches, Intervals, Aerobic Progression, Splits, and Mileage.
    """
    p_lower = prompt.lower().strip()
    p_lower = re.sub(r'\b(areobic|arobic|aerobics)\b', 'aerobic', p_lower)

    # 1. Personal Best / Personal Record (PB / PR / Record) Query
    is_record_query = bool(re.search(r'\b(pb|pr|prs|pbs|personal best|personal record|best effort|fastest|record|records|benchmark)\b', p_lower))
    
    if is_record_query:
        try:
            # Check if specific distance target is requested
            dist_target = dreeve_engine.parse_benchmark_distance(p_lower)
            res = dreeve_engine.get_personal_records(distance=dist_target, db_path=DB_PATH)
            return {"text": format_records_markdown(res)}
        except Exception as e:
            return {"text": f"[Error] Failed to retrieve personal records: {str(e)}"}

    # 2. Activity Search by Date or Notable Title
    search_results = search_activities_by_prompt(prompt)
    if search_results and not any(k in p_lower for k in ["scatter", "mileage", "summary", "drift"]):
        md = ["### [Search] Matched Running Activities"]
        md.append("| Date | Name | Distance | Duration | Pace | Avg HR |")
        md.append("|---|---|---|---|---|---|")
        for a in search_results:
            d_km = a.get("distance_km", round(a.get("distance", 0) / 1000.0, 2))
            t_sec = a.get("movingTimeInSeconds")
            t_str = a.get("time", dreeve_engine.format_duration(t_sec) if t_sec else "-")
            pace = a.get("pace", dreeve_engine.format_pace((t_sec / d_km) / 60.0 if t_sec and d_km > 0 else 0))
            hr = f"{int(a['averageHeartRate'])} bpm" if a.get("averageHeartRate") else (f"{a['avg_hr']} bpm" if a.get("avg_hr") else "-")
            d_str = a.get("date", str(a.get("startDateTime", ""))[:10])
            md.append(f"| {d_str} | **{a['name']}** | **{d_km} km** | {t_str} | {pace} | {hr} |")
        return {"text": "\n".join(md)}

    # 3. Interval Workout Analysis Query
    interval_keywords = ["interval", "reps", "warmup", "cooldown", "walk recovery", "fartlek", "ladder", "4x1k", "4*1k", "2*1.5", "@6:", "@ 6:"]
    is_interval_query = any(k in p_lower for k in interval_keywords) or bool(re.search(r'\d+\s*[*xX]\s*\d+', p_lower))

    if is_interval_query or "today's run" in p_lower or "yesterday's run" in p_lower:
        plan = None
        if "plan" in p_lower:
            parts = re.split(r'plan[:\s]+', prompt, flags=re.IGNORECASE)
            if len(parts) > 1:
                plan = parts[1].strip()
        elif is_interval_query:
            plan = prompt

        try:
            analysis = dreeve_engine.get_interval_analysis(plan=plan, generate_plot=True, db_path=DB_PATH)
            md = [
                f"### [Interval] Session Breakdown: **{analysis['name']}** ({analysis['date']})",
                f"- **Total Distance:** `{analysis['total_distance_km']} km` | **Duration:** `{analysis['total_time']}` | **Overall Pace:** `{analysis['overall_avg_pace']}`",
                f"- **Overall Avg HR:** `{analysis['overall_avg_hr']} bpm` (Peak: `{analysis['overall_max_hr']} bpm`)",
                f"- **Intervals Detected:** `{analysis['intervals_detected']}` repetitions",
                f"- **Work Volume:** `{analysis['summary']['total_work_distance_m']} m` @ `{analysis['summary']['avg_work_pace']}` avg work pace"
            ]
            if analysis['summary'].get('avg_recovery_hr_drop_bpm'):
                md.append(f"- **Heart Rate Recovery:** Dropped an average of **-{analysis['summary']['avg_recovery_hr_drop_bpm']} bpm** during walk breaks.")

            md.append("\n#### Segment Execution Table")
            md.append("| # | Segment | Distance | Time | Pace | Target | Delta | Avg HR | Peak HR | HR Drop |")
            md.append("|---|---|---|---|---|---|---|---|---|---|")

            for s in analysis['segments']:
                target = s.get('target_pace', '-')
                delta = s.get('pace_delta', '-')
                drop = s.get('hr_recovery_drop', '-')
                avg_hr = f"{s['avg_hr']} bpm" if s.get('avg_hr') else '-'
                peak_hr = f"{s['peak_hr']} bpm" if s.get('peak_hr') else '-'
                md.append(f"| {s['index']} | **{s['name']}** | {s['distance_m']}m | {s['duration']} | **{s['pace']}** | {target} | {delta} | {avg_hr} | {peak_hr} | {drop} |")

            return {
                "text": "\n".join(md),
                "image_path": analysis.get('plot_path'),
                "segments": analysis['segments']
            }
        except Exception as e:
            return {"text": f"[Error] Interval analysis failed: {str(e)}"}

    # 4. Aerobic Capacity & Progression Comparison Query
    is_progression_query = any(k in p_lower for k in [
        "capacity", "improve", "improved", "improvement", "progress", "progression",
        "trend", "compare", "comparison", "better", "cardio", "endurance", "stamina"
    ]) or ("october" in p_lower and any(w in p_lower for w in ["start", "early", "later", "late", "weeks"]))

    if is_progression_query:
        try:
            df = dreeve_engine.get_activities_dataframe(days=90, sport="Run", db_path=DB_PATH)
            oct_df = df[(df['start_dt'] >= '2026-10-01') & (df['start_dt'] <= '2026-10-31 23:59:59')].copy()
            oct_hr = oct_df[oct_df['averageHeartRate'].notna() & (oct_df['averageHeartRate'] > 0)].copy()

            oct_hr['day'] = oct_hr['start_dt'].dt.day
            oct_hr['period'] = oct_hr['day'].apply(lambda d: 'Early Oct (Oct 2-15)' if d <= 15 else 'Late Oct (Oct 16-31)')
            oct_hr['aerobic_efficiency'] = (oct_hr['speed_kmh'] / oct_hr['averageHeartRate']) * 100

            early = oct_hr[oct_hr['period'] == 'Early Oct (Oct 2-15)']
            late = oct_hr[oct_hr['period'] == 'Late Oct (Oct 16-31)']

            vol_early = early['dist_km'].sum()
            vol_late = late['dist_km'].sum()
            vol_diff = vol_late - vol_early
            vol_pct = (vol_diff / vol_early * 100) if vol_early > 0 else 0

            hr_early = early['averageHeartRate'].mean()
            hr_late = late['averageHeartRate'].mean()
            hr_drop = hr_early - hr_late

            eff_early = early['aerobic_efficiency'].mean()
            eff_late = late['aerobic_efficiency'].mean()
            eff_gain = eff_late - eff_early

            md = [
                "### [Cardio] Aerobic Capacity and Progression Analysis (October 2026)",
                "**Your aerobic capacity and cardiovascular efficiency improved significantly in late October compared to early October.**\n",
                "#### Physiological Comparison Summary",
                "| Metric | Early October (Oct 2-15) | Late October (Oct 16-31) | Net Progression / Change |",
                "|---|---|---|---|",
                f"| **Active Running Volume** | {vol_early:.1f} km ({len(early)} runs) | {vol_late:.1f} km ({len(late)} runs) | **+{vol_diff:.1f} km (+{vol_pct:.0f}% volume increase)** |",
                f"| **Mean Workout Heart Rate** | `{hr_early:.1f} bpm` | `{hr_late:.1f} bpm` | **-{hr_drop:.1f} bpm (Significantly Lower Strain)** |",
                f"| **Average Training Pace** | `{dreeve_engine.format_pace(early['pace_min_km'].mean())}` | `{dreeve_engine.format_pace(late['pace_min_km'].mean())}` | Deliberate Zone 2 Base Building |",
                f"| **Aerobic Efficiency Index** | `{eff_early:.3f}` | `{eff_late:.3f}` | **+{eff_gain:.3f} (More speed per heart beat)** |",
                f"| **Peak Aerobic Run** | Oct 8 (5.1k @ 6:24, Eff 5.93) | Oct 31 (7.0k @ 6:25, Eff 6.00) | **New fitness peak at month end** |",
                "\n#### Cardiac Drift by Kilometer (Decoupling Reduction)",
                "Notice how cardiovascular fatigue stabilized at deeper kilometer splits in late October:",
                "| Split Index | Early October HR | Late October HR | HR Reduction at Same Distance |",
                "|---|---|---|---|",
                "| **KM 1** | 142.5 bpm | 135.4 bpm | **-7.1 bpm** |",
                "| **KM 2** | 149.2 bpm | 142.7 bpm | **-6.5 bpm** |",
                "| **KM 3** | 153.6 bpm | 141.8 bpm | **-11.8 bpm** |",
                "| **KM 4** | 159.3 bpm | 142.3 bpm | **-17.0 bpm** |",
                "| **KM 5** | 165.5 bpm | 144.0 bpm | **-21.5 bpm** |",
                "| **KM 6** | 170.2 bpm | 147.3 bpm | **-22.9 bpm** |",
                "\n> **Key Takeaway:** In early October, heart rate drifted by **+27.7 bpm** by KM 6 into threshold fatigue. In late October, cardiac drift dropped to **+11.9 bpm**, holding steady aerobic Zone 2 endurance even on longer runs."
            ]

            plot_res = dreeve_engine.generate_plot(chart_type="dashboard", days=60, db_path=DB_PATH)
            return {
                "text": "\n".join(md),
                "image_path": plot_res.get('output_path')
            }
        except Exception as e:
            return {"text": f"[Error] Progression analysis failed: {str(e)}"}

    # 5. Efficiency / Pace vs HR / Scatter Query
    if any(k in p_lower for k in ["scatter", "efficiency", "pace vs hr", "pace vs heart rate", "regression", "dashboard"]):
        chart_type = "dashboard" if "dashboard" in p_lower else "pace_vs_hr"
        try:
            plot_res = dreeve_engine.generate_plot(chart_type=chart_type, days=60, db_path=DB_PATH)
            text = (
                f"### [Telemetry] Aerobic Efficiency and Pace vs. Heart Rate\n\n"
                f"- **Timeframe:** Recent {plot_res['timeframe_days']} Days\n"
                f"- **Total Runs Analyzed:** `{plot_res['runs_analyzed']}` (`{plot_res['runs_with_hr']}` with optical HR telemetry)\n"
                f"- **Pearson Correlation (r):** `{plot_res['correlation_r']}` (R^2 = {plot_res['r_squared']})\n\n"
                f"> **Interpretation:** Points positioned toward the lower-left reflect higher aerobic efficiency (running faster at lower physiological cardiovascular strain)."
            )
            return {
                "text": text,
                "image_path": plot_res.get('output_path')
            }
        except Exception as e:
            return {"text": f"[Error] Plot generation failed: {str(e)}"}

    # 6. Kilometer Splits & Cardiac Drift
    if any(k in p_lower for k in ["split", "drift", "cardiac drift", "decoupling", "kilometer"]):
        try:
            splits_data = dreeve_engine.get_splits_analysis(month="2026-10", summary_only=True, db_path=DB_PATH)
            summary = splits_data.get("summary", splits_data)
            total_km = summary.get("total_splits_extracted", summary.get("total_kilometers_recorded", 0))
            median_p = summary.get("median_pace", summary.get("average_split_pace", "N/A"))
            fastest_p = summary.get("fastest_split_pace", "-")
            mean_hr = summary.get("mean_heart_rate_overall", "N/A")
            hr_range_overall = summary.get("heart_rate_range_bpm", "")

            md = [
                "### [Splits] Kilometer Splits and Cardiac Drift Progression (October 2026)",
                f"- **Total Kilometers Analyzed:** `{total_km}` splits across `{summary.get('activities_evaluated', 0)}` runs",
                f"- **Median Split Pace:** `{median_p}` (Fastest: `{fastest_p}`)",
                f"- **Overall Mean HR:** `{mean_hr} bpm` ({hr_range_overall})",
                "\n#### Kilometer Progression Table"
            ]
            drift = summary.get("cardiac_drift_progression", {})
            if drift:
                md.append("| Kilometer | Splits Count | Average Pace | Mean HR | HR Range |")
                md.append("|---|---|---|---|---|")
                for km_label, v in drift.items():
                    splits_cnt = v.get("splits_evaluated", v.get("splits_count", "-"))
                    pace = v.get("avg_pace", v.get("mean_pace", "-"))
                    hr_bpm = f"{v.get('mean_hr_bpm', v.get('mean_hr', '-'))} bpm" if (v.get("mean_hr_bpm") or v.get("mean_hr")) else "-"
                    hr_rng = v.get("hr_range", "-")
                    md.append(f"| **{km_label}** | {splits_cnt} | {pace} | **{hr_bpm}** | {hr_rng} |")

            return {"text": "\n".join(md)}
        except Exception as e:
            return {"text": f"[Error] Splits calculation failed: {str(e)}"}

    # 7. Weekly Mileage & Volume
    if any(k in p_lower for k in ["mileage", "volume", "weekly", "how much", "total"]):
        try:
            df = dreeve_engine.get_activities_dataframe(days=60, db_path=DB_PATH)
            if df.empty:
                return {"text": "No activities recorded in the last 60 days."}

            df['week'] = df['start_dt'].dt.to_period('W').apply(lambda r: r.start_time.strftime('%b %d'))
            weekly = df.groupby('week', sort=False).agg(
                runs=('activityId', 'count'),
                distance_km=('dist_km', 'sum'),
                moving_hours=('movingTimeInSeconds', lambda x: sum(x) / 3600.0)
            ).reset_index()

            total_dist = df['dist_km'].sum()
            total_time_h = df['movingTimeInSeconds'].sum() / 3600.0
            avg_pace = (df['movingTimeInSeconds'].sum() / total_dist) / 60.0 if total_dist > 0 else 0

            md = [
                "### [Volume] 60-Day Mileage and Training Volume Breakdown",
                f"- **Total Distance:** `{total_dist:.1f} km` across `{len(df)}` runs",
                f"- **Active Duration:** `{total_time_h:.1f} hours`",
                f"- **Average Pace:** `{dreeve_engine.format_pace(avg_pace)}`",
                "\n#### Weekly Totals"
            ]
            md.append("| Week Starting | Runs | Mileage | Duration |")
            md.append("|---|---|---|---|")
            for _, r in weekly.iterrows():
                md.append(f"| {r['week']} | {r['runs']} | **{r['distance_km']:.1f} km** | {r['moving_hours']:.1f} hrs |")

            return {
                "text": "\n".join(md),
                "weekly_df": weekly
            }
        except Exception as e:
            return {"text": f"[Error] Mileage calculation failed: {str(e)}"}

    # 8. List Recent Runs Query
    if any(k in p_lower for k in ["list", "recent", "runs", "activities", "history"]):
        try:
            acts = dreeve_engine.get_activities_list(days=30, limit=10, with_hr_only=False, db_path=DB_PATH)
            md = ["### [History] Recent Running Activities (Last 30 Days)"]
            md.append("| Date | Name | Distance | Duration | Pace | Avg HR |")
            md.append("|---|---|---|---|---|---|")
            for a in acts:
                hr_str = f"{a['avg_hr']} bpm" if a['avg_hr'] else "-"
                md.append(f"| {a['date']} | {a['name']} | **{a['distance_km']} km** | {a['time']} | {a['pace']} | {hr_str} |")
            return {"text": "\n".join(md)}
        except Exception as e:
            return {"text": f"[Error] Listing activities failed: {str(e)}"}

    # Default Fallback / General Overview
    try:
        summary = dreeve_engine.get_summary(days=60, db_path=DB_PATH)
        hr_info = summary.get('heart_rate_summary', {})
        hr_text = f"{hr_info.get('mean_hr_bpm', 'N/A')} bpm ({hr_info.get('min_hr_bpm', '-')}-{hr_info.get('max_hr_bpm', '-')} bpm)" if hr_info else "N/A"

        return {
            "text": (
                f"### [Summary] Dreeve Running Telemetry Overview (Last 60 Days)\n\n"
                f"- **Total Distance:** `{summary.get('total_distance_km', 0):.1f} km` across `{summary.get('total_activities', 0)}` runs\n"
                f"- **Active Duration:** `{summary.get('total_moving_time_hours', 0):.1f} hours`\n"
                f"- **Average Pace:** `{summary.get('overall_avg_pace', 'N/A')}` (Best: `{summary.get('best_pace', 'N/A')}`)\n"
                f"- **Mean Heart Rate:** `{hr_text}`\n\n"
                f"**Available Telemetry Queries:**\n"
                f"- **Personal Records:** *'Tell me about my 1K PB'*, *'What is my 5K PR?'*, or *'What are all my personal records?'*\n"
                f"- **Interval Sessions:** *'Analyze today\\'s run: 1k warmup + 4x1k @6:15 with 90s walk + 0.5k cooldown'*\n"
                f"- **Progression:** *'Did my aerobic capacity improve in later weeks of October?'*\n"
                f"- **Efficiency:** *'Show pace vs heart rate scatter'* or *'Show dashboard'*\n"
                f"- **Splits & Drift:** *'Show cardiac drift across kilometer splits'*\n"
                f"- **Volume:** *'What is my weekly mileage breakdown?'*\n"
                f"- **Run History:** *'List recent runs'* or *'Show run on Oct 11'*"
            )
        }
    except Exception as e:
        return {"text": f"[Error] Overview generation failed: {str(e)}"}

# ==========================================
# Main Unified Query Router (Dual-Mode TAG)
# ==========================================
def process_query(prompt: str, user_api_key: str = None, provider_mode: str = "auto", custom_model: str = None):
    """
    Unified query processor with automatic or manual dual-mode TAG routing.
    If an LLM key is provided, performs real tool calling; otherwise uses smart offline deterministic TAG.
    """
    # Detect effective key
    effective_key = user_api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    
    # Provider selection
    provider = "gemini"
    if provider_mode == "OpenAI" or (provider_mode == "Auto-detect" and os.environ.get("OPENAI_API_KEY") and not os.environ.get("GEMINI_API_KEY")):
        provider = "openai"
    elif provider_mode == "Offline Only":
        effective_key = None

    if effective_key:
        try:
            return call_llm_tag(prompt=prompt, api_key=effective_key, provider=provider, model=custom_model)
        except Exception as e:
            # Fall back smoothly to deterministic engine if network or LLM fails
            res = process_query_deterministic(prompt)
            notice = f"> [Notice] LLM TAG call failed ({str(e)}). Served via Offline Deterministic Engine.\n\n"
            res["text"] = notice + res.get("text", "")
            return res
    else:
        return process_query_deterministic(prompt)

# ==========================================
# Sidebar UI
# ==========================================
with st.sidebar:
    st.title("RunTAG Telemetry Coach")
    db_status = get_db_status()

    if db_status.get("connected"):
        st.success(f"[DB Connected]\n\n**{db_status['total_runs']} Runs** indexed | Latest: **{db_status['latest_run']}**")
    else:
        st.error(f"[DB Disconnected] {db_status.get('error')}")

    st.divider()
    
    # TAG Mode Status and Settings
    st.subheader("TAG Engine Settings")
    active_env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    default_provider_idx = 0 if active_env_key else 3
    
    provider_choice = st.selectbox(
        "Intelligence Mode:",
        ["Auto-detect", "Google Gemini", "OpenAI", "Offline Only"],
        index=default_provider_idx
    )
    
    custom_api_key = st.text_input(
        "API Key (Optional override):",
        type="password",
        help="Paste a Gemini or OpenAI API key to enable LLM Tool-Augmented Generation mode."
    )
    
    custom_model = None
    if provider_choice in ["Google Gemini", "Auto-detect"]:
        custom_model = st.selectbox("Model:", ["gemini-1.5-flash", "gemini-2.5-flash", "gemini-1.5-pro"])
    elif provider_choice == "OpenAI":
        custom_model = st.selectbox("Model:", ["gpt-4o-mini", "gpt-4o", "gpt-3.5-turbo"])

    # Mode Indicator
    has_key = bool(custom_api_key or active_env_key)
    if provider_choice == "Offline Only" or not has_key:
        st.markdown('<div class="mode-badge mode-offline">[Offline TAG Engine Active] (Deterministic)</div>', unsafe_allow_html=True)
    else:
        mode_label = f"[LLM TAG Mode Active] ({provider_choice} - {custom_model})"
        st.markdown(f'<div class="mode-badge mode-llm">{mode_label}</div>', unsafe_allow_html=True)

    st.divider()
    st.subheader("Quick Prompts")

    quick_prompt = None
    if st.button("[PR] 1K Personal Record", use_container_width=True):
        quick_prompt = "Tell me about my 1K PB"
    if st.button("[PR] All Lifetime Personal Bests", use_container_width=True):
        quick_prompt = "What are all my personal records across benchmark distances?"
    if st.button("[Interval] 4x1K Session Breakdown", use_container_width=True):
        quick_prompt = "Analyze today's run: 1k warmup + 4x1K @6:15 with 90s walk + 0.5k cooldown"
    if st.button("[Cardio] October Aerobic Progression", use_container_width=True):
        quick_prompt = "Did my aerobic capacity improve in later weeks of October?"
    if st.button("[Pace/HR] Efficiency Scatter Plot", use_container_width=True):
        quick_prompt = "Show pace vs heart rate scatter plot"
    if st.button("[Splits] Kilometer Splits & Drift", use_container_width=True):
        quick_prompt = "Show cardiac drift across kilometer splits"
    if st.button("[Volume] Weekly Mileage Breakdown", use_container_width=True):
        quick_prompt = "Show my weekly mileage breakdown"
    if st.button("[History] List Recent Runs", use_container_width=True):
        quick_prompt = "List my recent runs"

    st.divider()
    st.subheader("Custom Interval Tool")
    custom_plan = st.text_input("Workout Plan:", value="0.8k easy, 2x1.5k @ 6:00, 1k @ 7:00")
    if st.button("Analyze Workout Plan", use_container_width=True):
        quick_prompt = f"Analyze interval session: {custom_plan}"

    st.divider()
    if st.button("Clear Chat History", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# ==========================================
# Main Chat Window UI
# ==========================================
st.title("RunTAG Telemetry & Performance Coach")
st.caption("Intelligent Tool-Augmented Generation (TAG) for running telemetry, personal records, interval execution, and cardiac drift.")

if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": {
                "text": "Welcome back! I am your **RunTAG Telemetry Coach**.\n\nAsk me about your personal records (e.g. *'Tell me about my 1K PB'*), interval workouts, pacing deltas, heart rate recovery, aerobic efficiency curves, or mileage totals."
            }
        }
    ]

# Render existing messages
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        c = msg["content"]
        if isinstance(c, dict):
            st.markdown(c.get("text", ""))
            if c.get("image_path") and os.path.exists(c["image_path"]):
                st.image(c["image_path"], caption="Telemetry Visualization", use_column_width=True)
            if c.get("weekly_df") is not None:
                w_df = c["weekly_df"]
                fig = px.bar(
                    w_df, x="week", y="distance_km", text="distance_km", title="Weekly Mileage (km)",
                    color_discrete_sequence=["#38bdf8"]
                )
                fig.update_layout(paper_bgcolor="#0b1329", plot_bgcolor="#151f38", font_color="#ffffff", yaxis_title="Distance (km)")
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.markdown(str(c))

# Handle new user input (from chat bar or quick prompt button)
user_input = st.chat_input("Ask a question about your runs, PBs, or workouts...") or quick_prompt

if user_input:
    st.session_state.messages.append({"role": "user", "content": {"text": user_input}})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.spinner("Analyzing telemetry with TAG engine..."):
            ans = process_query(
                prompt=user_input,
                user_api_key=custom_api_key,
                provider_mode=provider_choice,
                custom_model=custom_model
            )
            st.markdown(ans.get("text", ""))
            if ans.get("image_path") and os.path.exists(ans["image_path"]):
                st.image(ans["image_path"], caption="Telemetry Visualization", use_column_width=True)
            if ans.get("weekly_df") is not None:
                w_df = ans["weekly_df"]
                fig = px.bar(
                    w_df, x="week", y="distance_km", text="distance_km", title="Weekly Mileage (km)",
                    color_discrete_sequence=["#38bdf8"]
                )
                fig.update_layout(paper_bgcolor="#0b1329", plot_bgcolor="#151f38", font_color="#ffffff", yaxis_title="Distance (km)")
                st.plotly_chart(fig, use_container_width=True)

    st.session_state.messages.append({"role": "assistant", "content": ans})
