#!/usr/bin/env python3
"""DrawBox Web Dashboard — Control panel accessible from any browser on your network."""

import os, json, tempfile, subprocess, base64
from pathlib import Path
from io import BytesIO
from urllib.request import Request, urlopen
from flask import Flask, request, jsonify, Response, render_template_string, send_file
import replicate
from openai import OpenAI
from PIL import Image

# ── CONFIG ───────────────────────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "flux-schnell")
PRINTER_NAME = "drawbox-printer"
SETTINGS_FILE = Path.home() / ".drawbox" / "web_settings.json"
PLEASE_MODE_FILE = Path.home() / ".drawbox" / "please_mode"
SAFETY_MODE_FILE = Path.home() / ".drawbox" / "safety_mode"
GUIDE_PATH = Path.home() / "drawbox-guide.html"
SIMULATOR_PATH = Path.home() / "drawbox-simulator.html"

DEFAULT_COLORING_PROMPT = """Create a simple coloring page for children ages 3-8.
This is used by YOUNG CHILDREN — output MUST be 100% child-safe.
- Black and white LINE DRAWING only
- Thick, clean outlines (3-4px stroke)
- NO shading, NO gradients, NO filled/solid areas
- NO gray — pure black lines on white
- Simple shapes, minimal fine detail
- Large open areas for coloring with crayons
- Friendly, fun, cute, non-scary style
- Centered with padding — the subject must NOT touch or extend to the edges
- Leave at least 10% empty white space on all sides as margin
- Style: children's coloring book page
- ONLY draw safe, wholesome subjects (animals, nature, vehicles, food, toys)
- NEVER draw anything violent, scary, sexual, or inappropriate for a 5-year-old
- If the request is ambiguous, default to the most innocent interpretation"""

# ── SAFETY BLOCKLIST ─────────────────────────────
BLOCKED_WORDS = {
    "kill", "murder", "dead", "death", "die", "dying", "corpse",
    "blood", "bloody", "gore", "gory", "wound", "stab", "shoot",
    "gun", "guns", "rifle", "pistol", "shotgun", "weapon", "knife",
    "bomb", "explode", "explosion", "grenade", "missile", "nuke",
    "sex", "sexy", "sexual", "nude", "naked", "porn", "hentai",
    "penis", "dick", "cock", "vagina", "pussy", "boob", "breast",
    "nipple", "butt", "ass", "anus", "genital", "erotic", "orgasm",
    "fuck", "shit", "damn", "bitch", "bastard", "crap", "piss",
    "whore", "slut", "hooker", "prostitute", "stripper",
    "drug", "drugs", "cocaine", "heroin", "meth", "weed", "marijuana",
    "alcohol", "beer", "wine", "vodka", "drunk", "cigarette", "smoke",
    "devil", "satan", "demon", "hell", "torture", "horror", "zombie",
    "vampire", "skeleton", "skull", "coffin", "grave", "creepy",
    "scary", "nightmare", "terror", "evil", "wicked", "curse",
    "hate", "racist", "racism", "nazi", "hitler", "slavery", "slave",
    "suicide", "hanging", "noose", "drown", "poison", "overdose",
    "rape", "molest", "abuse", "kidnap", "assault", "victim",
    "war", "soldier", "army", "military", "combat", "battle",
    "thong", "lingerie", "bikini", "underwear",
    "pee", "fart", "vomit", "snot",
}

def is_safe(text):
    words = text.lower()
    return not any(w in words for w in BLOCKED_WORDS)

# ── SETTINGS ─────────────────────────────────────
def load_settings():
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {"coloring_prompt": DEFAULT_COLORING_PROMPT,
            "tts_voice": "nova", "record_seconds": 10}

def save_settings(data):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))

# ── IMAGE GENERATION & PRINTING ──────────────────
client = OpenAI(api_key=OPENAI_API_KEY)
is_generating = False
last_image_b64 = None

def generate_image(desc):
    settings = load_settings()
    prompt = settings.get("coloring_prompt", DEFAULT_COLORING_PROMPT)
    full_prompt = f"{prompt}\n\nChild requested: {desc}"
    model = settings.get("image_model", IMAGE_MODEL)
    if model == "nano-banana":
        img_bytes = _generate_nano_banana(full_prompt)
    elif model == "gpt-image":
        img_bytes = _generate_gpt_image(full_prompt)
    else:
        img_bytes = _generate_flux_schnell(full_prompt)
    return _postprocess(img_bytes)


def _generate_flux_schnell(prompt):
    output = replicate.run(
        "black-forest-labs/flux-schnell",
        input={
            "prompt": prompt, "num_outputs": 1, "aspect_ratio": "3:4",
            "output_format": "png", "num_inference_steps": 4, "go_fast": True,
        })
    return output[0].read()


def _generate_nano_banana(prompt):
    url = (f"https://generativelanguage.googleapis.com/v1beta/"
           f"models/gemini-2.5-flash-image:generateContent"
           f"?key={GEMINI_API_KEY}")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": "3:4"},
        },
    }).encode()
    req = Request(url, data=body, headers={"Content-Type": "application/json"})
    resp = urlopen(req, timeout=60)
    data = json.loads(resp.read())
    for part in data["candidates"][0]["content"]["parts"]:
        if "inlineData" in part:
            return base64.b64decode(part["inlineData"]["data"])
    raise RuntimeError("No image in Gemini response")


def _generate_gpt_image(prompt):
    r = client.images.generate(
        model="gpt-image-1", prompt=prompt,
        size="1024x1536", quality="low")
    return base64.b64decode(r.data[0].b64_json)


def _postprocess(img_bytes):
    img = Image.open(BytesIO(img_bytes)).convert("L")
    img = img.point(lambda x: 0 if x < 180 else 255, "1").convert("L")
    iw, ih = img.size
    canvas_w, canvas_h = 1275, 1650
    margin = 75
    max_w = canvas_w - 2 * margin
    max_h = canvas_h - 2 * margin
    scale = min(max_w / iw, max_h / ih)
    new_w = int(iw * scale)
    new_h = int(ih * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("L", (canvas_w, canvas_h), 255)
    canvas.paste(img, ((canvas_w - new_w) // 2, (canvas_h - new_h) // 2))
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    canvas.save(tmp.name); tmp.close()
    return tmp.name

def print_image(path):
    subprocess.run(["lp", "-d", PRINTER_NAME,
        "-o", "media=Letter", "-o", "fit-to-page", path], check=True)
    os.unlink(path)

# ── DIAGNOSTICS ALLOWLIST ────────────────────────
DIAGNOSTIC_COMMANDS = {
    "printer_status": ["lpstat", "-p", "-d"],
    "printer_queue":  ["lpstat", "-o"],
    "audio_inputs":   ["arecord", "-l"],
    "audio_outputs":  ["aplay", "-l"],
    "service_status": ["systemctl", "status", "drawbox"],
    "web_service":    ["systemctl", "status", "drawbox-web"],
    "disk_usage":     ["df", "-h", "/"],
    "uptime":         ["uptime"],
    "temperature":    ["vcgencmd", "measure_temp"],
    "wifi_status":    ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"],
    "wifi_list":      ["nmcli", "dev", "wifi", "list"],
    "rpi_connect":    ["rpi-connect", "status"],
    "journal_errors": ["journalctl", "-u", "drawbox", "-p", "err", "-n", "20", "--no-pager"],
    "cups_log":       ["tail", "-n", "30", "/var/log/cups/error_log"],
}

# ── FLASK APP ────────────────────────────────────
from werkzeug.middleware.proxy_fix import ProxyFix
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# CORS: allow requests from the cloud hub dashboard
@app.after_request
def add_cors(response):
    origin = request.headers.get("Origin", "")
    if origin and (".drawbox." in origin or origin.endswith(".pages.dev")):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
    return response

@app.route("/api/<path:path>", methods=["OPTIONS"])
def cors_preflight(path):
    return "", 204

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DrawBox Dashboard</title>
<style>
:root{--bg:#faf9f6;--card:#fff;--border:#e8e5de;--text:#1a1a1a;--muted:#666;--accent:#c0392b;--green:#27ae60;--blue:#2980b9;--amber:#f39c12;--dark:#2c2c2c;--code-bg:#1e1e1e;--code-text:#d4d4d4}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'Helvetica Neue','Segoe UI',sans-serif;background:var(--bg);color:var(--text);line-height:1.65}
.wrap{max-width:720px;margin:0 auto;padding:24px 20px 80px}
.hero{background:var(--dark);color:#fff;padding:28px 28px;border-radius:14px;margin-bottom:22px;position:relative;overflow:hidden}
.hero::after{content:'\\1F3A8';position:absolute;right:20px;top:50%;transform:translateY(-50%);font-size:64px;opacity:.12}
.hero h1{font-size:24px;margin-bottom:4px}
.hero p{color:#aaa;font-size:13px}
.hero-links{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
.hero-links a{display:inline-flex;align-items:center;gap:5px;padding:6px 14px;font-size:12px;font-weight:600;background:rgba(255,255,255,.12);color:#fff;border-radius:7px;text-decoration:none;transition:background .2s}
.hero-links a:hover{background:rgba(255,255,255,.22)}
.panel{background:var(--card);border:1px solid var(--border);border-radius:10px;margin-bottom:18px;overflow:hidden}
.panel-h{padding:12px 18px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--muted);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px}
.panel-b{padding:16px 18px}
.status-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px}
.status-item{background:var(--bg);border-radius:8px;padding:10px 14px;text-align:center}
.status-item .val{font-size:16px;font-weight:700;margin-bottom:2px}
.status-item .lbl{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.dot-green{background:var(--green)}.dot-red{background:var(--accent)}.dot-amber{background:var(--amber)}
.cfg-row{margin-bottom:14px}
.cfg-row:last-child{margin-bottom:0}
.cfg-row label{display:block;font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px}
.cfg-row input,.cfg-row select,.cfg-row textarea{display:block;width:100%;padding:9px 14px;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:12.5px;background:var(--code-bg);color:var(--code-text);border:2px solid var(--border);border-radius:7px;outline:none;transition:border-color .2s}
.cfg-row input:focus,.cfg-row select:focus,.cfg-row textarea:focus{border-color:var(--blue)}
.cfg-row select{cursor:pointer;-webkit-appearance:none;appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23999' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center}
.cfg-row textarea{resize:vertical;min-height:120px;line-height:1.5}
.gen-row{display:flex;gap:10px}
.gen-row input{flex:1}
.btn{padding:9px 20px;font-size:13px;font-weight:600;border:none;border-radius:7px;cursor:pointer;transition:background .2s,opacity .2s}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-red{background:var(--accent);color:#fff}.btn-red:hover:not(:disabled){background:#a93226}
.btn-blue{background:var(--blue);color:#fff}.btn-blue:hover:not(:disabled){background:#2471a3}
.btn-green{background:var(--green);color:#fff}.btn-green:hover:not(:disabled){background:#229954}
.btn-dark{background:var(--dark);color:#fff}.btn-dark:hover:not(:disabled){background:#444}
.btn-outline{background:transparent;color:var(--text);border:1px solid var(--border)}.btn-outline:hover:not(:disabled){background:var(--bg)}
.btn-sm{padding:6px 12px;font-size:11px}
.btn-row{display:flex;gap:8px;flex-wrap:wrap}
.log-area{background:var(--code-bg);color:var(--code-text);border-radius:0 0 10px 10px;padding:14px 18px;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:11.5px;line-height:1.7;height:300px;overflow-y:auto;white-space:pre-wrap;word-break:break-word}
.diag-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px;margin-bottom:14px}
.diag-out{background:var(--code-bg);color:var(--code-text);border-radius:8px;padding:14px 18px;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:11.5px;line-height:1.6;min-height:60px;max-height:300px;overflow-y:auto;white-space:pre-wrap;word-break:break-word}
.preview-img{max-width:100%;border:1px solid var(--border);border-radius:6px;margin-top:12px;display:none}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);padding:10px 24px;border-radius:8px;font-size:13px;font-weight:600;color:#fff;z-index:999;opacity:0;transition:opacity .3s}
.toast.show{opacity:1}
.toast-ok{background:var(--green)}.toast-err{background:var(--accent)}
@media(max-width:600px){
.wrap{padding:14px 12px 60px}
.hero{padding:20px 18px;border-radius:10px}
.hero h1{font-size:20px}
.gen-row{flex-direction:column}
.status-grid{grid-template-columns:1fr 1fr}
.diag-grid{grid-template-columns:1fr 1fr}
.log-area{height:220px;font-size:11px}
}
</style>
</head>
<body>
<div class="wrap">

  <div class="hero">
    <h1>DrawBox Dashboard</h1>
    <p>Control panel for your AI coloring page printer</p>
    <div class="hero-links">
      <a href="/guide" target="_blank">&#128214; Build Guide</a>
      <a href="/simulator" target="_blank">&#127918; Simulator</a>
      <a href="https://connect.raspberrypi.com" target="_blank" rel="noopener">&#128268; RPi Connect</a>
    </div>
  </div>

  <!-- STATUS -->
  <div class="panel">
    <div class="panel-h">&#9889; Status</div>
    <div class="panel-b">
      <div class="status-grid" id="statusGrid">
        <div class="status-item"><div class="val" id="svcStatus">...</div><div class="lbl">DrawBox Service</div></div>
        <div class="status-item"><div class="val" id="piTemp">...</div><div class="lbl">Pi Temperature</div></div>
        <div class="status-item"><div class="val" id="piUptime">...</div><div class="lbl">Uptime</div></div>
        <div class="status-item"><div class="val" id="rpiConnect">...</div><div class="lbl">RPi Connect</div></div>
      </div>
    </div>
  </div>

  <!-- GENERATE -->
  <div class="panel">
    <div class="panel-h">&#127912; Generate &amp; Print</div>
    <div class="panel-b">
      <div class="gen-row">
        <input type="text" id="genInput" placeholder="A happy dinosaur with flowers..." style="padding:9px 14px;font-size:14px;border:2px solid var(--border);border-radius:7px;outline:none" />
        <button class="btn btn-red" id="genBtn" onclick="doGenerate()">Generate &amp; Print</button>
      </div>
      <img class="preview-img" id="previewImg" alt="Last generated coloring page" />
    </div>
  </div>

  <!-- LOGS -->
  <div class="panel">
    <div class="panel-h">&#128220; Live Logs <button class="btn btn-outline btn-sm" onclick="clearLog()" style="margin-left:auto">Clear</button></div>
    <div class="log-area" id="logArea">Connecting to log stream...
</div>
  </div>

  <!-- SETTINGS -->
  <div class="panel">
    <div class="panel-h">&#9881; Settings</div>
    <div class="panel-b">
      <div class="cfg-row">
        <label>Coloring Prompt</label>
        <textarea id="cfgPrompt" rows="8"></textarea>
      </div>
      <div class="cfg-row">
        <label>Image Model</label>
        <select id="cfgModel">
          <option value="flux-schnell">FLUX Schnell (Replicate) — fastest</option>
          <option value="nano-banana">Nano Banana 2 (Gemini)</option>
          <option value="gpt-image">GPT Image (OpenAI) — slowest</option>
        </select>
      </div>
      <div class="cfg-row">
        <label>TTS Voice</label>
        <select id="cfgVoice">
          <option value="nova">nova</option>
          <option value="alloy">alloy</option>
          <option value="echo">echo</option>
          <option value="fable">fable</option>
          <option value="onyx">onyx</option>
          <option value="shimmer">shimmer</option>
        </select>
      </div>
      <div class="cfg-row">
        <label>Record Seconds</label>
        <input type="number" id="cfgRecSec" min="3" max="30" />
      </div>
      <div class="cfg-row">
        <label>&ldquo;Please&rdquo; Mode</label>
        <button class="btn btn-outline" id="pleaseBtn" onclick="togglePlease()">...</button>
        <span style="font-size:11px;color:var(--muted);margin-top:4px;display:block">When enabled, kids must say &ldquo;please&rdquo; to get a drawing</span>
      </div>
      <div class="cfg-row">
        <label>Safety Filter</label>
        <button class="btn btn-outline" id="safetyBtn" onclick="toggleSafety()">...</button>
        <span style="font-size:11px;color:var(--muted);margin-top:4px;display:block">Word blocklist filter. When off, the AI prompt still instructs child-safe output.</span>
      </div>
      <div class="btn-row" style="margin-top:14px">
        <button class="btn btn-blue" onclick="saveSettings()">Save Settings</button>
        <button class="btn btn-outline" onclick="loadSettings()">Reset</button>
      </div>
    </div>
  </div>

  <!-- DIAGNOSTICS -->
  <div class="panel">
    <div class="panel-h">&#128295; Diagnostics</div>
    <div class="panel-b">
      <div class="diag-grid">
        <button class="btn btn-outline btn-sm" onclick="runDiag('printer_status')">Printer Status</button>
        <button class="btn btn-outline btn-sm" onclick="runDiag('printer_queue')">Print Queue</button>
        <button class="btn btn-outline btn-sm" onclick="runDiag('audio_inputs')">Audio Inputs</button>
        <button class="btn btn-outline btn-sm" onclick="runDiag('audio_outputs')">Audio Outputs</button>
        <button class="btn btn-outline btn-sm" onclick="runDiag('service_status')">Service Status</button>
        <button class="btn btn-outline btn-sm" onclick="runDiag('web_service')">Web Service</button>
        <button class="btn btn-outline btn-sm" onclick="runDiag('disk_usage')">Disk Usage</button>
        <button class="btn btn-outline btn-sm" onclick="runDiag('temperature')">Temperature</button>
        <button class="btn btn-outline btn-sm" onclick="runDiag('wifi_status')">WiFi Status</button>
        <button class="btn btn-outline btn-sm" onclick="runDiag('wifi_list')">WiFi Networks</button>
        <button class="btn btn-outline btn-sm" onclick="runDiag('rpi_connect')">RPi Connect</button>
        <button class="btn btn-outline btn-sm" onclick="runDiag('journal_errors')">Error Logs</button>
        <button class="btn btn-outline btn-sm" onclick="runDiag('cups_log')">CUPS Log</button>
      </div>
      <div class="diag-out" id="diagOut">Click a button above to run a diagnostic command.</div>
    </div>
  </div>

  <!-- WIFI -->
  <div class="panel">
    <div class="panel-h">&#128246; WiFi</div>
    <div class="panel-b">
      <div class="btn-row" style="margin-bottom:10px">
        <button class="btn btn-outline btn-sm" onclick="loadWifi()">Scan Networks</button>
        <span id="wifiCurrent" style="font-size:12px;color:var(--muted);line-height:28px;margin-left:8px"></span>
      </div>
      <div id="wifiList" style="display:none;margin-bottom:10px;max-height:200px;overflow-y:auto"></div>
      <div id="wifiConnect" style="display:none">
        <div style="font-size:12px;font-weight:600;margin-bottom:6px">Connect to: <span id="wifiSSID"></span></div>
        <div class="gen-row">
          <input type="password" id="wifiPass" placeholder="Password (leave empty for open networks)" style="padding:9px 14px;font-size:13px;border:2px solid var(--border);border-radius:7px;outline:none" />
          <button class="btn btn-blue btn-sm" onclick="connectWifi()">Connect</button>
        </div>
      </div>
    </div>
  </div>

  <!-- SERVICE CONTROL -->
  <div class="panel">
    <div class="panel-h">&#9881; Service Control</div>
    <div class="panel-b">
      <div class="btn-row">
        <button class="btn btn-green" onclick="svcAction('restart')">Restart DrawBox</button>
        <button class="btn btn-dark" onclick="svcAction('stop')">Stop DrawBox</button>
        <button class="btn btn-blue" onclick="svcAction('start')">Start DrawBox</button>
        <button class="btn btn-outline" onclick="if(confirm('Reboot the entire Pi?'))rebootPi()">Reboot Pi</button>
      </div>
    </div>
  </div>

</div>

<div class="toast" id="toast"></div>

<script>
const $log=document.getElementById('logArea');
const $genBtn=document.getElementById('genBtn');
const $genInput=document.getElementById('genInput');
const $preview=document.getElementById('previewImg');

// ── TOAST ──────────────────────────────────────
function toast(msg,ok=true){
  const t=document.getElementById('toast');
  t.textContent=msg;
  t.className='toast show '+(ok?'toast-ok':'toast-err');
  setTimeout(()=>t.classList.remove('show'),3000);
}

// ── STATUS ─────────────────────────────────────
async function refreshStatus(){
  try{
    const r=await fetch('/api/status');
    const d=await r.json();
    document.getElementById('svcStatus').innerHTML=
      (d.service_running?'<span class="dot dot-green"></span>Running':'<span class="dot dot-red"></span>Stopped');
    document.getElementById('piTemp').textContent=d.temperature||'--';
    document.getElementById('piUptime').textContent=d.uptime||'--';
    document.getElementById('rpiConnect').innerHTML=
      d.rpi_connect==='unknown'?'--':
      (d.rpi_connect==='connected'?'<span class="dot dot-green"></span>Connected':
       '<span class="dot dot-amber"></span>'+d.rpi_connect);
  }catch(e){}
}
refreshStatus();
setInterval(refreshStatus,15000);

// ── GENERATE ───────────────────────────────────
async function doGenerate(){
  const desc=$genInput.value.trim();
  if(!desc){toast('Type a description first',false);return;}
  $genBtn.disabled=true;$genBtn.textContent='Generating...';
  try{
    const r=await fetch('/api/generate',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({description:desc})});
    const d=await r.json();
    if(d.ok){
      toast('Coloring page sent to printer!');
      if(d.image){$preview.src='data:image/png;base64,'+d.image;$preview.style.display='block';}
      $genInput.value='';
    }else{toast(d.error||'Generation failed',false);}
  }catch(e){toast('Network error: '+e.message,false);}
  $genBtn.disabled=false;$genBtn.textContent='Generate & Print';
}
$genInput.addEventListener('keydown',e=>{if(e.key==='Enter')doGenerate();});

// ── LOGS (SSE) ─────────────────────────────────
let evtSrc=null;
function connectLogs(){
  if(evtSrc)evtSrc.close();
  evtSrc=new EventSource('/api/logs');
  evtSrc.onmessage=e=>{
    $log.textContent+=e.data+'\\n';
    $log.scrollTop=$log.scrollHeight;
  };
  evtSrc.onerror=()=>{
    $log.textContent+='[log stream disconnected — reconnecting...]\\n';
  };
}
connectLogs();
function clearLog(){$log.textContent='';}

// ── SETTINGS ───────────────────────────────────
async function loadSettings(){
  try{
    const r=await fetch('/api/settings');
    const d=await r.json();
    document.getElementById('cfgPrompt').value=d.coloring_prompt||'';
    document.getElementById('cfgModel').value=d.image_model||'flux-schnell';
    document.getElementById('cfgVoice').value=d.tts_voice||'nova';
    document.getElementById('cfgRecSec').value=d.record_seconds||10;
  }catch(e){}
}
async function saveSettings(){
  const data={
    coloring_prompt:document.getElementById('cfgPrompt').value,
    image_model:document.getElementById('cfgModel').value,
    tts_voice:document.getElementById('cfgVoice').value,
    record_seconds:parseInt(document.getElementById('cfgRecSec').value)||10
  };
  try{
    const r=await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const d=await r.json();
    if(d.ok)toast('Settings saved!');
    else toast('Save failed',false);
  }catch(e){toast('Error: '+e.message,false);}
}
loadSettings();

// ── PLEASE MODE ───────────────────────────────
async function loadPleaseMode(){
  try{
    const r=await fetch('/api/please-mode');
    const d=await r.json();
    const b=document.getElementById('pleaseBtn');
    b.textContent=d.enabled?'ON (required)':'OFF';
    b.className='btn '+(d.enabled?'btn-green':'btn-outline');
  }catch(e){}
}
async function togglePlease(){
  try{
    const r=await fetch('/api/please-mode');
    const d=await r.json();
    await fetch('/api/please-mode',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled:!d.enabled})});
    loadPleaseMode();
    toast(d.enabled?'"Please" mode disabled':'"Please" mode enabled!');
  }catch(e){toast('Error: '+e.message,false);}
}
loadPleaseMode();

// ── SAFETY MODE ───────────────────────────────
async function loadSafetyMode(){
  try{
    const r=await fetch('/api/safety-mode');
    const d=await r.json();
    const b=document.getElementById('safetyBtn');
    b.textContent=d.enabled?'ON (filtering)':'OFF (bypassed)';
    b.className='btn '+(d.enabled?'btn-green':'btn-outline');
  }catch(e){}
}
async function toggleSafety(){
  try{
    const r=await fetch('/api/safety-mode');
    const d=await r.json();
    if(d.enabled && !confirm('Turn OFF the safety word filter? The AI prompt still instructs child-safe output, but blocked words will be allowed.')) return;
    await fetch('/api/safety-mode',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled:!d.enabled})});
    loadSafetyMode();
    toast(d.enabled?'Safety filter disabled':'Safety filter enabled!');
  }catch(e){toast('Error: '+e.message,false);}
}
loadSafetyMode();

// ── WIFI ──────────────────────────────────────
let wifiTarget='';
async function loadWifi(){
  document.getElementById('wifiCurrent').textContent='Scanning...';
  try{
    // Get current connection
    const sr=await fetch('/api/diagnostics',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({command:'wifi_status'})});
    const sd=await sr.json();
    const cur=(sd.output||'').split('\\n').find(l=>l.startsWith('yes'));
    document.getElementById('wifiCurrent').textContent=cur?'Connected: '+cur.split(':')[1]:'Not connected';
    // Get network list
    const r=await fetch('/api/wifi/networks');
    const d=await r.json();
    const $list=document.getElementById('wifiList');
    if(!d.networks||d.networks.length===0){$list.innerHTML='<div style=\"font-size:12px;color:var(--muted)\">No networks found</div>';$list.style.display='block';return;}
    $list.innerHTML=d.networks.map(n=>'<div style=\"display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);cursor:pointer\" onclick=\"selectWifi(\\''+n.ssid.replace(/'/g,\"\\\\'\")+'\\')\">'
      +'<span style=\"font-size:13px;font-weight:500\">'+n.ssid+'</span>'
      +'<span style=\"font-size:11px;color:var(--muted);margin-left:auto\">'+n.signal+'% &middot; '+n.security+'</span>'
      +'</div>').join('');
    $list.style.display='block';
  }catch(e){document.getElementById('wifiCurrent').textContent='Error: '+e.message;}
}
function selectWifi(ssid){
  wifiTarget=ssid;
  document.getElementById('wifiSSID').textContent=ssid;
  document.getElementById('wifiConnect').style.display='block';
  document.getElementById('wifiPass').value='';
  document.getElementById('wifiPass').focus();
}
async function connectWifi(){
  if(!wifiTarget) return;
  toast('Connecting to '+wifiTarget+'...');
  try{
    const r=await fetch('/api/wifi/connect',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ssid:wifiTarget,password:document.getElementById('wifiPass').value})});
    const d=await r.json();
    if(d.ok){toast('Connected to '+wifiTarget);document.getElementById('wifiConnect').style.display='none';loadWifi();}
    else toast(d.error||'Failed to connect',false);
  }catch(e){toast('Error: '+e.message,false);}
}

// ── DIAGNOSTICS ────────────────────────────────
async function runDiag(cmd){
  const out=document.getElementById('diagOut');
  out.textContent='Running '+cmd+'...';
  try{
    const r=await fetch('/api/diagnostics',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({command:cmd})});
    const d=await r.json();
    out.textContent=d.output||d.error||'No output';
  }catch(e){out.textContent='Error: '+e.message;}
}

// ── SERVICE CONTROL ────────────────────────────
async function svcAction(action){
  try{
    const r=await fetch('/api/service/'+action,{method:'POST'});
    const d=await r.json();
    toast(d.message||(action+' done'));
    setTimeout(refreshStatus,2000);
  }catch(e){toast('Error: '+e.message,false);}
}
async function rebootPi(){
  try{
    await fetch('/api/reboot',{method:'POST'});
    toast('Rebooting Pi...');
  }catch(e){toast('Error: '+e.message,false);}
}
</script>
</body>
</html>"""

# ── ROUTES ───────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/guide")
def guide():
    if GUIDE_PATH.exists():
        return send_file(GUIDE_PATH)
    return "Guide not found. Copy drawbox-guide.html to ~/", 404

@app.route("/simulator")
def simulator():
    if SIMULATOR_PATH.exists():
        return send_file(SIMULATOR_PATH)
    return "Simulator not found. Copy drawbox-simulator.html to ~/", 404

@app.route("/api/status")
def api_status():
    # Service status
    svc = subprocess.run(["systemctl", "is-active", "drawbox"],
                         capture_output=True, text=True)
    running = svc.stdout.strip() == "active"

    # Temperature
    temp = "--"
    try:
        t = subprocess.run(["vcgencmd", "measure_temp"],
                           capture_output=True, text=True, timeout=5)
        temp = t.stdout.strip().replace("temp=", "")
    except Exception:
        pass

    # Uptime
    up = "--"
    try:
        u = subprocess.run(["uptime", "-p"],
                           capture_output=True, text=True, timeout=5)
        up = u.stdout.strip().replace("up ", "")
    except Exception:
        pass

    # RPi Connect
    rpi = "unknown"
    try:
        rc = subprocess.run(["rpi-connect", "status"],
                            capture_output=True, text=True, timeout=5)
        out = rc.stdout.strip().lower()
        if "connected" in out:
            rpi = "connected"
        elif "not" in out or rc.returncode != 0:
            rpi = "off"
        else:
            rpi = out[:20]
    except FileNotFoundError:
        rpi = "not installed"
    except Exception:
        pass

    return jsonify(service_running=running, temperature=temp,
                   uptime=up, rpi_connect=rpi)

@app.route("/api/generate", methods=["POST"])
def api_generate():
    global is_generating, last_image_b64
    if is_generating:
        return jsonify(ok=False, error="Already generating — please wait.")

    data = request.get_json() or {}
    desc = (data.get("description") or "").strip()
    if not desc:
        return jsonify(ok=False, error="Please describe what to draw.")
    if len(desc) > 500:
        return jsonify(ok=False, error="Description too long (max 500 chars).")
    if SAFETY_MODE_FILE.exists() and not is_safe(desc):
        return jsonify(ok=False,
            error="That description contains blocked words. "
                  "Try something fun like an animal or a rainbow!")

    is_generating = True
    try:
        path = generate_image(desc)
        # Read image for preview before printing
        with open(path, "rb") as f:
            last_image_b64 = base64.b64encode(f.read()).decode()
        print_image(path)
        return jsonify(ok=True, image=last_image_b64)
    except Exception as e:
        return jsonify(ok=False, error=str(e))
    finally:
        is_generating = False

@app.route("/api/logs")
def api_logs():
    def generate():
        proc = subprocess.Popen(
            ["journalctl", "-u", "drawbox", "-f", "-n", "50", "--no-pager"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for line in proc.stdout:
                yield f"data: {line.rstrip()}\n\n"
        except GeneratorExit:
            proc.kill()
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify(load_settings())
    data = request.get_json() or {}
    settings = load_settings()
    if "coloring_prompt" in data:
        settings["coloring_prompt"] = data["coloring_prompt"][:5000]
    if "image_model" in data and data["image_model"] in ("flux-schnell", "nano-banana", "gpt-image"):
        settings["image_model"] = data["image_model"]
    if "tts_voice" in data:
        settings["tts_voice"] = data["tts_voice"]
    if "record_seconds" in data:
        settings["record_seconds"] = max(3, min(30, int(data["record_seconds"])))
    save_settings(settings)
    return jsonify(ok=True)

@app.route("/api/please-mode", methods=["GET", "POST"])
def api_please_mode():
    if request.method == "GET":
        return jsonify(enabled=PLEASE_MODE_FILE.exists())
    data = request.get_json() or {}
    if data.get("enabled"):
        PLEASE_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PLEASE_MODE_FILE.touch()
    else:
        PLEASE_MODE_FILE.unlink(missing_ok=True)
    return jsonify(ok=True, enabled=PLEASE_MODE_FILE.exists())

@app.route("/api/safety-mode", methods=["GET", "POST"])
def api_safety_mode():
    if request.method == "GET":
        return jsonify(enabled=SAFETY_MODE_FILE.exists())
    data = request.get_json() or {}
    if data.get("enabled"):
        SAFETY_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SAFETY_MODE_FILE.touch()
    else:
        SAFETY_MODE_FILE.unlink(missing_ok=True)
    return jsonify(ok=True, enabled=SAFETY_MODE_FILE.exists())

@app.route("/api/wifi/networks")
def api_wifi_networks():
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=15)
        networks = []
        seen = set()
        for line in r.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[0] and parts[0] not in seen:
                seen.add(parts[0])
                networks.append({
                    "ssid": parts[0],
                    "signal": int(parts[1]) if parts[1].isdigit() else 0,
                    "security": parts[2] or "Open"
                })
        networks.sort(key=lambda n: n["signal"], reverse=True)
        return jsonify(networks=networks)
    except Exception as e:
        return jsonify(networks=[], error=str(e))

@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    data = request.get_json() or {}
    ssid = (data.get("ssid") or "").strip()
    password = (data.get("password") or "").strip()
    if not ssid:
        return jsonify(ok=False, error="SSID required")
    try:
        cmd = ["sudo", "nmcli", "dev", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return jsonify(ok=True, message=f"Connected to {ssid}")
        else:
            return jsonify(ok=False, error=r.stderr.strip() or r.stdout.strip())
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route("/api/diagnostics", methods=["POST"])
def api_diagnostics():
    data = request.get_json() or {}
    cmd_key = data.get("command", "")
    if cmd_key not in DIAGNOSTIC_COMMANDS:
        return jsonify(error="Unknown command: " + cmd_key)
    cmd = DIAGNOSTIC_COMMANDS[cmd_key]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        output = r.stdout
        if r.stderr:
            output += "\n" + r.stderr
        return jsonify(output=output.strip() or "(no output)")
    except FileNotFoundError:
        return jsonify(output=f"Command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return jsonify(output="Command timed out after 15 seconds.")
    except Exception as e:
        return jsonify(error=str(e))

@app.route("/api/service/<action>", methods=["POST"])
def api_service(action):
    if action not in ("restart", "stop", "start"):
        return jsonify(ok=False, message="Invalid action"), 400
    try:
        subprocess.run(["sudo", "systemctl", action, "drawbox"],
                       check=True, timeout=30)
        return jsonify(ok=True,
                       message=f"DrawBox service {action}ed successfully.")
    except Exception as e:
        return jsonify(ok=False, message=str(e))

@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    try:
        subprocess.Popen(["sudo", "reboot"])
        return jsonify(ok=True, message="Rebooting...")
    except Exception as e:
        return jsonify(ok=False, message=str(e))

# ── INIT ─────────────────────────────────────────
SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
# Safety mode ON by default (create sentinel file if missing)
if not SAFETY_MODE_FILE.exists():
    SAFETY_MODE_FILE.touch()

if __name__ == "__main__":
    if not OPENAI_API_KEY:
        print("⚠️  OPENAI_API_KEY not set. Voice features won't work.")
    print(f"   Image model: {IMAGE_MODEL}")
    print(f"   Safety filter: {'ON' if SAFETY_MODE_FILE.exists() else 'OFF'}")
    print("DrawBox Web Dashboard starting on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
