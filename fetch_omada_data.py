import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point, WritePrecision

load_dotenv("config.env")

EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
INFLUX_URL = os.getenv("INFLUX_URL")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN")
INFLUX_ORG = os.getenv("INFLUX_ORG")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET_PAST_CONNECTIONS")

TZ_OFFSET = timezone(timedelta(hours=8))  # GMT+8

with open("config/provinces.json", "r") as f:
    provinces = json.load(f)


def login_to_omada(base_url, email, password, creds):
    login_url = f"{base_url}/openapi/authorize/login?client_id={creds['client_id']}&omadac_id={creds['omadac_id']}"
    payload = {"username": email, "password": password}
    try:
        response = requests.post(login_url, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        csrf_token = data["result"]["csrfToken"]
        session_id = data["result"]["sessionId"]
        return csrf_token, session_id
    except Exception as e:
        print(f"❌ Error logging into Omada: {e}")
        raise


def get_authorization_code(base_url, creds, csrf_token, session_id):
    auth_code_url = f"{base_url}/openapi/authorize/code?client_id={creds['client_id']}&omadac_id={creds['omadac_id']}&response_type=code"
    headers = {
        "Csrf-Token": csrf_token,
        "Cookie": f"TPOMADA_SESSIONID={session_id}"
    }
    try:
        response = requests.post(auth_code_url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()["result"]
    except Exception as e:
        print(f"❌ Error getting authorization code: {e}")
        raise


def get_access_token(base_url, creds, auth_code):
    token_url = f"{base_url}/openapi/authorize/token?grant_type=authorization_code&code={auth_code}"
    payload = {
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"]
    }
    try:
        response = requests.post(token_url, json=payload, timeout=15)
        response.raise_for_status()
        return response.json()["result"]["accessToken"]
    except Exception as e:
        print(f"❌ Error getting access token: {e}")
        raise


def get_sites(base_url, omadac_id, access_token):
    site_url = f"{base_url}/openapi/v1/{omadac_id}/sites?pageSize=80&page=1"
    headers = {"Authorization": f"AccessToken={access_token}"}
    try:
        response = requests.get(site_url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()["result"]["data"]
    except Exception as e:
        print(f"❌ Error fetching sites: {e}")
        raise


def fetch_past_connections(base_url, omadac_id, site_id, access_token, time_start, time_end, page):
    url = f"{base_url}/openapi/v1/{omadac_id}/sites/{site_id}/insight/past-connection?pageSize=1000&page={page}&filters.timeStart={time_start}&filters.timeEnd={time_end}"
    headers = {"Authorization": f"AccessToken={access_token}"}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json().get("result", {}).get("data", [])
    except Exception as e:
        print(f"❌ Error fetching past connections: {e}")
        raise



def process_and_store_data(data, influx_writer, province_name, site_name):
    points = []
    philippines_tz = timezone(timedelta(hours=8))

    for conn in data:
        mac = conn.get("mac")
        name = conn.get("name", "")
        device_name = conn.get("deviceName", "")
        ssid = conn.get("ssid", "")
        first_seen = conn.get("firstSeen")
        last_seen = conn.get("lastSeen")
        duration = conn.get("duration", 0)
        download = conn.get("download", 0)
        upload = conn.get("upload", 0)

        if not (mac and first_seen and last_seen):
            continue

        total_traffic_MB = round((download + upload) / (1024 * 1024), 2)
        session_id = f"{mac}_{first_seen}_{last_seen}"
        common_tags = {
            "province": province_name,
            "site": site_name,
            "mac": mac,
            "device": name,
            "ssid": ssid,
            "ap_name": device_name,
            "session_id": session_id
        }

        first_seen_dt = datetime.fromtimestamp(first_seen / 1000.0, tz=philippines_tz)
        last_seen_dt = datetime.fromtimestamp(last_seen / 1000.0, tz=philippines_tz)

        start_point = Point("connection_traffic").time(first_seen_dt, WritePrecision.MS).field("total_traffic_MB", total_traffic_MB)
        end_point = Point("connection_traffic").time(last_seen_dt, WritePrecision.MS).field("total_traffic_MB", total_traffic_MB)

        for k, v in common_tags.items():
            start_point.tag(k, v)
            end_point.tag(k, v)

        points.append(start_point)
        points.append(end_point)

    if points:
        influx_writer.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)

def to_utc_millis(dt_local):
    """Convert timezone-aware local datetime to UTC milliseconds"""
    return int(dt_local.astimezone(timezone.utc).timestamp() * 1000)

def get_all_past_connections(province_name, creds, influx_writer):
    try:
        base_url = creds["url"]
        csrf_token, session_id = login_to_omada(base_url, EMAIL, PASSWORD, creds)
        auth_code = get_authorization_code(base_url, creds, csrf_token, session_id)
        access_token = get_access_token(base_url, creds, auth_code)
        sites = get_sites(base_url, creds["omadac_id"], access_token)

        # Set time range to the last 5 minutes
        now = datetime.now(TZ_OFFSET)
        five_minutes_ago = now - timedelta(minutes=5)
        time_start = to_utc_millis(five_minutes_ago)
        time_end = to_utc_millis(now)

        for site in sites:
            site_id = site["siteId"]
            site_name = site["name"]
            print(f"🚧 Fetching data for Site: {site_name} in Province: {province_name}...")
            print(f"📡 Fetching data for the last 5 minutes for Site: {site_name} in Province: {province_name}...")
            page = 1
            while True:
                data = fetch_past_connections(base_url, creds["omadac_id"], site_id, access_token, time_start, time_end, page)
                if not data:
                    break
                print(f"📊 Processing page {page}...")
                process_and_store_data(data, influx_writer, province_name, site_name)
                if len(data) < 1000:
                    break
                page += 1
                time.sleep(0.5)
    except Exception as e:
        print(f"❌ Error fetching data for {province_name}: {e}")







if __name__ == "__main__":
    try:
        with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as influx:
            influx_writer = influx.write_api()

            for province_name, creds in provinces.items():
                print(f"📡 Fetching for {province_name}")
                try:
                    get_all_past_connections(province_name, creds, influx_writer)
                except Exception as e:
                    print(f"❌ Error fetching data for {province_name}: {e}")
    except Exception as e:
        print(f"❌ Failed InfluxDB operation: {e}")

