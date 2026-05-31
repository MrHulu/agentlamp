"""Live 172x320 browser simulator (display_spec.md → Browser Simulator).

Renders from the EXACT frame JSON returned by
``GET /api/v1/device/{device_id}/frame``. Polls every 3 s, shows the payload
byte size, highlights stale/expired TTL, and lets you inject mock events via
``POST /admin/event`` to drive scenes. The design (palette, device chrome,
scene layouts) is borrowed from ``docs/ui/mockups/scenes.html``.
"""
from __future__ import annotations

# A single self-contained HTML page (no external CDNs). It is parameterized only
# by the device id + token so the fetch carries the Bearer header.
PREVIEW_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>AgentLamp — live simulator</title>
<style>
  :root{
    --idle:#4d7cff; --think:#6d6bff; --code:#a06bff; --read:#22d3ee;
    --test:#2dd4a7; --wait:#ffb020; --done:#34d399; --err:#ff5470;
    --off:#7b8696; --stale:#d6dae0;
    --ink:#eef1f6; --ink-dim:#9aa3b2; --ink-faint:#5b6473; --screen:#080a10;
    --font:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",system-ui,sans-serif;
    --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,monospace;
  }
  /* accent enum -> css var */
  .blue{--accent:var(--idle)} .cyan{--accent:var(--read)} .purple{--accent:var(--code)}
  .yellow{--accent:var(--wait)} .green{--accent:var(--done)} .red{--accent:var(--err)}
  .white{--accent:var(--stale)} .muted{--accent:var(--off)}
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0c0d11;color:var(--ink);font-family:var(--font);
       padding:40px;-webkit-font-smoothing:antialiased;display:flex;gap:48px;flex-wrap:wrap}
  h1{font-size:22px;font-weight:800;letter-spacing:-.02em;margin-bottom:6px}
  .sub{color:var(--ink-dim);font-size:13px;line-height:1.5;max-width:420px;margin-bottom:20px}
  .panel{min-width:360px}
  .device{position:relative;width:172px;height:320px;border-radius:24px;background:#000;padding:8px;
          box-shadow:0 18px 40px -16px rgba(0,0,0,.9),0 0 0 1px #1c1f27}
  .device::after{content:"";position:absolute;inset:-22px;border-radius:38px;z-index:-1;
          background:radial-gradient(60% 50% at 50% 42%,var(--accent),transparent 70%);opacity:.32;filter:blur(8px)}
  .screen{position:relative;width:156px;height:304px;border-radius:17px;overflow:hidden;
          background:radial-gradient(120% 80% at 50% -10%,color-mix(in srgb,var(--accent) 30%,transparent),transparent 60%),
                     radial-gradient(120% 70% at 50% 115%,color-mix(in srgb,var(--accent) 16%,transparent),transparent 55%),var(--screen);
          display:flex;flex-direction:column;padding:16px 15px 15px}
  .screen.stale, .screen.offline{filter:grayscale(.4) brightness(.85)}
  .top{display:flex;justify-content:space-between;align-items:center;font:600 10px/1 var(--mono);
       letter-spacing:.06em;color:var(--ink-dim);text-transform:uppercase}
  .top .who{display:flex;align-items:center;gap:6px}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--accent);box-shadow:0 0 8px var(--accent)}
  .mid{flex:1;display:flex;flex-direction:column;justify-content:center}
  .bot{margin-top:auto}
  .status{font-weight:800;letter-spacing:-.03em;line-height:.92;color:var(--accent);
          text-shadow:0 0 22px color-mix(in srgb,var(--accent) 55%,transparent)}
  .kicker{font:600 10px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--ink-faint)}
  .meta{font:600 12px/1.35 var(--font);color:var(--ink)} .meta .s{color:var(--ink-dim);font-weight:500}
  .tiny{font:600 10px/1.5 var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--ink-faint)}
  .rows{display:flex;flex-direction:column;gap:9px}
  .row{display:flex;align-items:center;gap:8px;font:600 12px/1 var(--font)}
  .row .st{margin-left:auto;font:700 9px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;
           padding:3px 6px;border-radius:5px;color:var(--accent);background:color-mix(in srgb,var(--accent) 16%,transparent)}
  .q{display:flex;flex-direction:column;gap:5px;margin-bottom:13px}
  .q .ql{display:flex;justify-content:space-between;font:600 11px/1 var(--font);color:var(--ink)}
  .bar{height:7px;border-radius:4px;background:rgba(255,255,255,.07);overflow:hidden}
  .bar i{display:block;height:100%;border-radius:4px;background:linear-gradient(90deg,color-mix(in srgb,var(--accent) 60%,#000),var(--accent));box-shadow:0 0 10px var(--accent)}
  .ring{width:96px;height:96px;border-radius:50%;margin:0 auto 14px;display:flex;align-items:center;justify-content:center;
        border:3px solid var(--accent);box-shadow:0 0 26px color-mix(in srgb,var(--accent) 60%,transparent)}
  .ring b{font:800 30px/1 var(--font);color:var(--accent)}
  .center{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:10px}
  .lamp{font-size:34px;color:var(--accent);filter:drop-shadow(0 0 14px var(--accent))}
  .clock{font:300 52px/1 var(--font);letter-spacing:-.03em;color:var(--ink);opacity:.55}
  .hud{margin-top:18px;font:600 12px/1.6 var(--mono);color:var(--ink-dim)}
  .hud .k{color:var(--ink-faint)} .hud b{color:var(--ink)}
  .hud .warn{color:var(--err)}
  .controls{margin-top:22px;display:flex;flex-direction:column;gap:8px;max-width:360px}
  .controls h3{font:700 12px/1 var(--mono);text-transform:uppercase;letter-spacing:.1em;color:var(--ink-faint);margin-bottom:4px}
  .controls button{font:600 11px/1 var(--font);padding:8px 10px;border-radius:8px;border:1px solid #232733;
          background:#13161d;color:var(--ink);cursor:pointer;text-align:left}
  .controls button:hover{border-color:var(--accent)}
  .raw{margin-top:14px;font:500 10px/1.5 var(--mono);color:var(--ink-faint);white-space:pre-wrap;
       max-width:360px;max-height:240px;overflow:auto;background:#0a0c11;padding:10px;border-radius:8px;border:1px solid #1c1f27}
</style></head>
<body>
<div class="panel">
  <h1>AgentLamp · live</h1>
  <div class="sub">Rendered from the exact frame JSON at
    <code>/api/v1/device/__DEVICE__/frame</code>. Polls every 3 s.</div>
  <div class="device" id="device"><div class="screen" id="screen"></div></div>
  <div class="hud" id="hud"></div>
</div>
<div class="panel">
  <div class="controls">
    <h3>inject event → /admin/event</h3>
    <button data-ev='{"provider":"claude","account":"work","status":"CODING","project":"project-a","task":"implementing"}'>Claude · work → CODING</button>
    <button data-ev='{"provider":"codex","account":"main","status":"WAITING","project":"project-a","task":"waiting"}'>Codex · main → WAITING (alert)</button>
    <button data-ev='{"provider":"codex","account":"main","status":"ERROR","project":"project-b","task":"debugging","error_label":"tool_error"}'>Codex · main → ERROR (alert)</button>
    <button data-ev='{"provider":"claude","account":"main","status":"TESTING","project":"project-b","task":"testing"}'>Claude · main → TESTING</button>
    <button data-ev='{"provider":"claude","account":"work","status":"THINKING","project":"project-b","task":"planning"}'>Claude · work → THINKING</button>
    <button data-ev='{"provider":"claude","account":"work","status":"DONE","project":"project-a","task":"idle"}'>Claude · work → DONE</button>
    <button data-quota='{"provider":"codex","account":"main","window_type":"5h","used_ratio":0.93,"confidence":"medium"}'>Codex · main quota 93% (danger)</button>
    <button data-reset="1">⟲ reset state</button>
  </div>
  <div class="raw" id="raw"></div>
</div>
<script>
const DEVICE="__DEVICE__", TOKEN="__TOKEN__";
const AUTH={headers:{Authorization:"Bearer "+TOKEN,"X-Frame-Schema-Version":"1"}};
function esc(s){return String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function render(f,bytes){
  const dev=document.getElementById("device"), scr=document.getElementById("screen");
  dev.className="device "+f.accent; scr.className="screen "+f.scene;
  const time=new Date(f.server_time*1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
  const p=f.primary||{};
  let html="";
  if(f.scene==="sleep"){
    html=`<div class="center"><div class="clock">${time}</div><div class="tiny" style="opacity:.4">all idle</div></div>`;
  } else if(f.scene==="boot"){
    html=`<div class="center"><div class="lamp">◗</div><div style="font:800 21px/1 var(--font)">AgentLamp</div><div class="tiny">starting</div></div>`;
  } else if(f.scene==="offline"){
    html=`<div class="top"><span class="who">offline</span><span>${time}</span></div>
      <div class="center"><div class="status" style="font-size:28px">OFFLINE</div>
      <div class="meta s">frame source<br>unreachable</div></div>`;
  } else if(f.scene==="alert"){
    const sym=p.status==="ERROR"?"×":"!";
    html=`<div class="top"><span class="who"><i class="dot"></i>alert</span><span>${time}</span></div>
      <div class="mid" style="text-align:center"><div class="ring"><b>${sym}</b></div>
      <div class="status" style="font-size:30px">${esc(p.status)}</div>
      <div class="meta" style="margin-top:10px">${esc(p.provider)} · ${esc(p.account)}<br><span class="s">${esc(p.task)}</span></div></div>
      <div class="bot tiny" style="text-align:center">${esc(p.project)} · seq ${f.seq}</div>`;
  } else if(f.scene==="quota"){
    html=`<div class="top"><span class="who"><i class="dot"></i>quota</span><span>${time}</span></div><div class="mid">`;
    (f.quota||[]).forEach(q=>{const r=(q.w5??q.week??0); const w=q.w5!=null?"5h":"week";
      html+=`<div class="q"><div class="ql"><span>${esc(q.provider)} ·${esc(q.account)}</span><span class="tiny">${w}</span></div>
        <div class="bar"><i style="width:${Math.round(r*100)}%"></i></div>
        <div style="display:flex;justify-content:space-between"><span class="status" style="font-size:11px">${Math.round(r*100)}%</span>
        <span class="tiny">${q.estimated?"est":""}</span></div></div>`;});
    html+=`</div><div class="bot tiny">top-2 risk</div>`;
  } else if(f.scene==="fleet"){
    html=`<div class="top"><span class="who"><i class="dot"></i>agents</span><span>${time}</span></div><div class="mid"><div class="rows">`;
    (f.fleet||[]).forEach(r=>{html+=`<div class="row"><span>${esc(r.provider)} <span style="color:var(--ink-faint)">×${r.count}</span></span><span class="st">${esc(r.status)}</span></div>`;});
    html+=`</div></div><div class="bot tiny">${(f.fleet||[]).length} groups${f.fleet_more?` · +${f.fleet_more}`:""}</div>`;
  } else { /* focus + stale */
    html=`<div class="top"><span class="who"><i class="dot"></i>${esc(f.scene)}</span><span>${time}</span></div>
      <div class="mid"><div class="kicker" style="margin-bottom:6px">${esc(p.provider)} · ${esc(p.account)}</div>
      <div class="status" style="font-size:32px">${esc(p.status)}</div>
      <div class="meta" style="margin-top:12px">${esc(p.project)}<br><span class="s">${esc(p.task)}</span></div></div>
      <div class="bot tiny">seq ${f.seq}${f.scene==="stale"?" · cached":""}</div>`;
  }
  scr.innerHTML=html;
  const over=bytes>=2048;
  document.getElementById("hud").innerHTML=
    `<div><span class="k">scene</span> <b>${esc(f.scene)}</b> · <span class="k">accent</span> <b>${esc(f.accent)}</b></div>`+
    `<div><span class="k">seq</span> <b>${f.seq}</b> · <span class="k">ttl</span> <b>${f.ttl}s</b> · <span class="k">v</span> <b>${f.v}</b></div>`+
    `<div><span class="k">body</span> <b class="${over?"warn":""}">${bytes} B</b> / 2048 B${over?" ⚠ OVER CAP":""}</div>`;
  document.getElementById("raw").textContent=JSON.stringify(f,null,2);
}
async function poll(){
  try{
    const r=await fetch(`/api/v1/device/${DEVICE}/frame`,AUTH);
    const txt=await r.text(); const bytes=new Blob([txt]).size;
    if(r.ok){ render(JSON.parse(txt),bytes); }
    else{ document.getElementById("hud").innerHTML=`<div class="warn">HTTP ${r.status} ${esc(txt)}</div>`; }
  }catch(e){ document.getElementById("hud").innerHTML=`<div class="warn">${esc(e.message)}</div>`; }
}
async function inject(body){ await fetch("/admin/event",{method:"POST",headers:{"Content-Type":"application/json"},body}); poll(); }
async function quota(body){ await fetch("/admin/quota",{method:"POST",headers:{"Content-Type":"application/json"},body}); poll(); }
async function reset(){ await fetch("/admin/reset",{method:"POST"}); poll(); }
document.querySelectorAll(".controls button").forEach(b=>b.onclick=()=>{
  if(b.dataset.ev) inject(b.dataset.ev);
  else if(b.dataset.quota) quota(b.dataset.quota);
  else if(b.dataset.reset) reset();
});
poll(); setInterval(poll,3000);
</script>
</body></html>"""


def render_preview(device_id: str, token: str) -> str:
    return PREVIEW_HTML.replace("__DEVICE__", device_id).replace("__TOKEN__", token)
