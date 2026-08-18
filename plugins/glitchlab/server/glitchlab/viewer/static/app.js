/* GlitchLab viewer client: live state, guarded controls, and evidence inspection. */
const OC = {
  "candidate":   {c:"var(--caution)", g:"?", lbl:"candidate"},
  "confirmed":   {c:"var(--ok)",      g:"✓", lbl:"confirmed"},
  "reset":       {c:"var(--reset)",   g:"↻", lbl:"reset"},
  "exception":   {c:"var(--danger)",  g:"✕", lbl:"exception"},
  "invalid_infrastructure":{c:"var(--danger)",g:"!",lbl:"invalid"},
  "false-positive":{c:"var(--fp)",    g:"?", lbl:"false-pos"},
  "no-effect":   {c:"var(--noeff)",   g:"·", lbl:"no-effect"},
  "flash-erased":{c:"var(--danger)",  g:"E", lbl:"flash-erased"},
  "no-data":     {c:"var(--noeff)",   g:" ", lbl:"no-data"}
};
const state = {activeSweep:null, page:"live", grid:null, totals:{}, mapTotals:{}, attempts:[], filter:null,
                confirmationFilter:null, psScope:"campaign",
               dry:true, sumTotal:0, ratehist:[], boot:null, ws:null, running:false, t0:null,
               lastCap:null, lastMeas:null, workflow:null, profileReady:false,
               recipeLocked:false, formDirty:false, targetState:{state:"clear",blocking:false},lastSweepStatus:null,
               connectors:[],waveformLoading:{}};

/* ---------------- WebSocket ---------------- */
function connect(){
  const ws = new WebSocket((location.protocol==="https:"?"wss":"ws")+"://"+location.host+"/ws");
  state.ws = ws;
  ws.onopen = ()=>{ document.getElementById("mcpind").classList.add("on"); reportState(); };
  ws.onclose = ()=>{ document.getElementById("mcpind").classList.remove("on"); setTimeout(connect,1200); };
  ws.onmessage = e=>{ const m=JSON.parse(e.data);
    if(m.type==="event") onEvent(m.kind,m.data);
    else if(m.type==="command") onCommand(m);
    else if(m.type==="hello" && m.active && m.active.sweep_id){ state.activeSweep=m.active.sweep_id; refreshLive(); }
  };
}
function send(o){ try{ state.ws.send(JSON.stringify(o)); }catch(e){} }
function ack(id,d){ send({type:"ack",id,ack:{...d,state:uiState()}}); }
function reportState(){ send({type:"state",state:uiState()}); }
function uiState(){ const f={}; document.querySelectorAll("[data-mcp-field]").forEach(el=>f[el.getAttribute("data-mcp-field")]=el.value);
  return {page:state.page,fields:f,active_sweep:state.activeSweep,dry_run:state.dry,
    recipe_locked:false,target_state:state.targetState,scope_policy:state.scopePolicy||{}}; }

/* ---------------- visible-MCP commands ---------------- */
function onCommand(m){ const {id,action,payload}=m; let r={ok:true};
  try{
    if(action==="navigate"){ navigate(payload.page); r={ok:true,navigated:payload.page}; }
    else if(action==="click"){ r=doClick(payload.target); }
    else if(action==="set_field"){ r=setField(payload.field,payload.value); }
    else if(action==="fill_form"){ r={ok:true,set:{}}; for(const k in payload.fields) r.set[k]=setField(k,payload.fields[k]).ok; }
    else if(action==="highlight"){ r=highlight(payload.target,payload.note); }
    else if(action==="toast"){ toast(payload.message,payload.level); }
    else if(action==="get_state"){ r={ok:true}; }
    else r={ok:false,reason:"unknown_action"};
  }catch(e){ r={ok:false,error:String(e)}; }
  ack(id,r); reportState();
}
function byMcp(t){ return document.querySelector(`[data-mcp="${t}"]`)||document.querySelector(`[data-mcp-field="${t}"]`); }
function doClick(t){ const el=byMcp(t); if(!el) return {ok:false,applied:false,reason:"element_not_found",target:t};
  flash(el); el.click(); return {ok:true,applied:true,clicked:t}; }
function setField(f,v){ const el=document.querySelector(`[data-mcp-field="${f}"]`); if(!el) return {ok:false,reason:"field_not_found",field:f};
  el.value=v; state.formDirty=true; el.dispatchEvent(new Event("input",{bubbles:true})); flash(el); return {ok:true,applied:true,field:f,value:String(v)}; }
function highlight(t,note){ const el=byMcp(t); if(!el) return {ok:false,reason:"not_found"};
  flash(el); el.scrollIntoView({behavior:"smooth",block:"center"}); if(note) toast(note,"info"); return {ok:true,highlighted:t}; }
function flash(el){ el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); }

/* ---------------- actions (buttons → backend) ---------------- */
function act(name,payload){ if(name==="noop")return; logAgent(name, payload); send({type:"action",name,payload:payload||{}}); }
function saveNotificationSettings(){
  const enabled=!!document.getElementById("notification-enabled")?.checked;
  const topic=document.getElementById("notification-topic")?.value||"";
  const base_url=document.getElementById("notification-base")?.value||"https://ntfy.sh";
  act("save_notification_settings",{enabled,topic,base_url});
  const input=document.getElementById("notification-topic"); if(input)input.value="";
}
function selectedMosfets(){ return ["lp","hp"].filter(m=>{ const e=document.getElementById("mos-"+m);
  return e && e.classList.contains("on"); }); }
function toggleMos(m){ const e=document.getElementById("mos-"+m);
  if(!e) return; ["lp","hp"].forEach(x=>{ const n=document.getElementById("mos-"+x); if(n)n.classList.toggle("on",x===m); }); state.formDirty=true; reportState(); }
function connectorForm(){ const sel=document.getElementById("f-connector"), params={};
  document.querySelectorAll("[data-connector-param]").forEach(el=>{ const n=el.dataset.connectorParam,t=el.dataset.paramType;
    params[n]=t==="boolean"?!!el.checked:(t==="integer"?parseInt(el.value,10):(t==="number"?parseFloat(el.value):el.value)); });
  return {id:sel?sel.value:"",parameters:params}; }
function sweepForm(){ const g=id=>document.getElementById(id).value; const mos=selectedMosfets();
  return { campaign_name:g("f-campaign"), width_min:g("f-wmin"), width_max:g("f-wmax"),
  width_step:g("f-wstep"), offset_min:g("f-omin"), offset_max:g("f-omax"), offset_step:g("f-ostep"), repeats:g("f-rep"),
  recipe:"default", mosfet:(mos.length===1?mos:null), stop_on_success:true, dry_run:state.dry,
  campaign_id:(state.scope&&state.scope.kind==="campaign"?state.scope.id:null),
  connector:connectorForm()}; }

function connectorVisible(def,values){ const rule=def.visible_if||{}; return Object.keys(rule).every(k=>values[k]===rule[k]); }
function renderConnectorParameters(){ const select=document.getElementById("f-connector"), host=document.getElementById("connector-params"), src=document.getElementById("connector-source");
  if(!select||!host)return; const c=(state.connectors||[]).find(x=>x.id===select.value); if(!c){host.innerHTML="";return;}
  const current={}; (c.dynamic_parameters||[]).forEach(d=>current[d.name]=d.default);
  document.querySelectorAll("[data-connector-param]").forEach(el=>{ current[el.dataset.connectorParam]=el.dataset.paramType==="boolean"?!!el.checked:el.value; });
  host.innerHTML=(c.dynamic_parameters||[]).filter(d=>connectorVisible(d,current)).map(d=>{ const id="cp-"+d.name,tip=(d.description||"").replace(/"/g,"&quot;"),val=current[d.name];
    let control;if(d.type==="boolean")control=`<input type="checkbox" id="${id}" data-connector-param="${d.name}" data-param-type="boolean" ${val?"checked":""} onchange="renderConnectorParameters()">`;
    else if(d.type==="select")control=`<select class="inp" id="${id}" data-connector-param="${d.name}" data-param-type="select" onchange="renderConnectorParameters()">${(d.choices||[]).map(v=>`<option ${v===val?"selected":""}>${v}</option>`).join("")}</select>`;
    else control=`<input class="inp" id="${id}" data-connector-param="${d.name}" data-param-type="${d.type}" type="${["integer","number"].includes(d.type)?"number":"text"}" value="${val}" ${d.minimum!=null?`min="${d.minimum}"`:""} ${d.maximum!=null?`max="${d.maximum}"`:""}>`;
    return `<label class="fld" title="${tip}"><div class="l">${d.title||d.name}</div>${control}</label>`; }).join("");
  if(src)src.textContent=`${c.display_name} · ${c.fingerprint.slice(0,12)} · ${c.source||"connector"}`;
  select.disabled=false;
  document.querySelectorAll("[data-connector-param]").forEach(el=>{ el.disabled=false; });
  reportState(); }
async function loadConnectors(){ try{ const d=await (await fetch("/api/connectors")).json(); state.connectors=d.connectors||[]; const sel=document.getElementById("f-connector");
    if(sel){ const old=sel.value; sel.innerHTML=state.connectors.map(c=>`<option value="${c.id}">${c.display_name}</option>`).join(""); if(state.connectors.some(c=>c.id===old))sel.value=old; renderConnectorParameters(); }
  }catch(e){ const src=document.getElementById("connector-source");if(src)src.textContent="connector scan failed: "+e; } }
function targetInterlocked(){ return !!(state.targetState&&state.targetState.blocking); }
function toggleDry(){ state.dry=!state.dry; document.getElementById("dryrun").classList.toggle("on",state.dry); reportState(); }
function toggleTheme(){ const r=document.documentElement; const d=r.getAttribute("data-theme")==="light";
  if(d) r.removeAttribute("data-theme"); else r.setAttribute("data-theme","light");
  document.getElementById("theme-lbl").textContent=d?"DARK":"LIGHT"; if(state.grid) drawHeatmap(); }
/* ---------- campaign control: editable next plan + immutable active sweep ---------- */
const RECIPE_FIELDS=["f-wmin","f-wmax","f-wstep","f-omin","f-omax","f-ostep","f-rep"];
function viewingOpenRigSweep(){
  const active=(state.boot||{}).active||{}, selected=curScope();
  if(!active.sweep_id||!selected)return false;
  return selected.kind==="campaign" ? selected.id===active.campaign_id : selected.id===active.sweep_id;
}
function controlsBelongToAnotherCampaign(){ return !!state.running&&!viewingOpenRigSweep(); }
function setFieldsEnabled(_on){ const campaign=document.getElementById("f-campaign");
  if(campaign){ campaign.disabled=false; campaign.style.opacity=""; campaign.style.cursor=""; }
  const connector=document.getElementById("f-connector");
  if(connector){ connector.disabled=false; connector.style.opacity=""; connector.style.cursor=""; }
  document.querySelectorAll("[data-connector-param]").forEach(el=>{ el.disabled=false; });
  RECIPE_FIELDS.forEach(id=>{ const e=document.getElementById(id); if(e){ e.readOnly=false; e.disabled=false; e.style.opacity=""; e.style.cursor=""; } });
  const dt=document.getElementById("dryrun"); if(dt){ dt.style.pointerEvents=""; dt.style.opacity=""; }
  ["lp","hp"].forEach(m=>{ const e=document.getElementById("mos-"+m); if(e) e.setAttribute("aria-disabled","false"); }); }
function updateControlUI(){ const ss=document.getElementById("btn-startstop"), pr=document.getElementById("btn-pauseresume"),
    st=document.getElementById("ctl-status"); if(!ss||!pr) return;
  const running=!!state.running&&viewingOpenRigSweep(), paused=!!state.paused;
  if(running){
    ss.textContent=browseOnly()?"Rig sweep running":"■ Stop Sweep"; ss.classList.remove("acc"); ss.classList.add("danger");
    ss.classList.toggle("disabled",browseOnly()); ss.setAttribute("aria-disabled",browseOnly()?"true":"false");
    pr.style.display=browseOnly()?"none":""; pr.style.opacity=""; pr.style.pointerEvents="";
    pr.textContent = paused ? "▶ Resume" : "❚❚ Pause";
    if(st && !state.stopping) st.textContent = paused ? "paused" : "running";
    setFieldsEnabled(true);
  } else {
    ss.textContent="▶ Start Sweep"; ss.classList.add("acc"); ss.classList.remove("danger");
    pr.style.display="none"; state.stopping=false;
    if(controlsBelongToAnotherCampaign()) ss.textContent="Rig sweep running";
    setFieldsEnabled(true);
    const blocked=targetInterlocked()||browseOnly()||controlsBelongToAnotherCampaign();
    ss.classList.toggle("disabled",blocked); ss.setAttribute("aria-disabled",blocked?"true":"false");
    if(st && controlsBelongToAnotherCampaign()) st.textContent="viewing this campaign · controls remain with the live campaign";
    if(st && browseOnly()) st.textContent="viewing another project · controls remain with rig project";
    else if(st && !controlsBelongToAnotherCampaign() && targetInterlocked()) st.textContent=state.lastSweepStatus||"target state interlocked";
    else if(st && !controlsBelongToAnotherCampaign() && !/done/.test(st.textContent||"")) st.textContent="idle";
  } }
function onStartStop(){ if(browseOnly()){ toast("This is a visual project view; the active rig campaign was not changed.","info"); }
  else if(controlsBelongToAnotherCampaign()){ toast("This is a visual campaign view; controls remain with the open live campaign.","info"); }
  else if(state.running&&viewingOpenRigSweep()){ state.stopping=true;
    const st=document.getElementById("ctl-status"); if(st) st.textContent="stopping…";
    act("stop_sweep",{}); /* running flips off when sweep_done arrives */ }
  else if(targetInterlocked()){ toast("Start refused: preserved/unknown-held target state is interlocked","danger"); }
  else if(!state.profileReady){ toast("Start refused: active project recipe is not loaded","danger"); }
  else if(!state.dry && !workflowReadyForLive()){ toast("Live start refused: complete run readiness first","danger"); }
  else { act("start_sweep",sweepForm()); } }
function onPauseResume(){ if(!state.running||!viewingOpenRigSweep()) return;
  if(state.paused){ act("resume_sweep",{}); state.paused=false; } else { act("pause_sweep",{}); state.paused=true; }
  updateControlUI(); }
function populateControlFromLive(){
  const b=state.boot||{}, a=(b.active||{});
  // A campaign opened from the browser is the displayed next-plan context. It
  // must win over the server's last active campaign, which is only relevant to
  // a currently running rig sweep.
  let cname=(state.scope&&state.scope.kind==="campaign"&&state.scope.name)||null;
  if(!cname && b.campaigns && a.campaign_id){ const c=b.campaigns.find(x=>x.id===a.campaign_id||x.campaign_id===a.campaign_id); if(c) cname=c.name; }
  if(!cname && state.scope && state.scope.name) cname=state.scope.name;
  // Campaign selection is navigation, not a user parameter edit: always show
  // the selected campaign name even when the remaining next-plan fields are dirty.
  const cf=document.getElementById("f-campaign");
  if(cf && cname) cf.value=cname;
  const crumb=document.getElementById("crumb-camp");
  if(crumb && cname) crumb.textContent=cname;
  if(state.formDirty) return;
  const setv=(id,v)=>{ const e=document.getElementById(id); if(e&&v!=null&&isFinite(+v)) e.value=(v%1===0?+v:(+v).toFixed(3)); };
  const setMos=(list)=>{ if(!list||list.length!==1) return; ["lp","hp"].forEach(m=>{ const e=document.getElementById("mos-"+m); if(e) e.classList.toggle("on", list[0]===m); }); };
  const ps=a.param_spec;
  if(ps && viewingOpenRigSweep()){                 // exact plan from the open running sweep
    const ax=ps.axes||{};
    const pc=ax.pulse_cycles||ax.width;
    if(pc){ const vals=axisBounds(pc); setv("f-wmin",vals.min); setv("f-wmax",vals.max); setv("f-wstep",vals.step); }
    if(ax.ext_offset){ const vals=axisBounds(ax.ext_offset); setv("f-omin",vals.min); setv("f-omax",vals.max); setv("f-ostep",vals.step); }
    if(ps.repeats_per_cell!=null) setv("f-rep",ps.repeats_per_cell);
    else if(ps.repeats!=null) setv("f-rep",ps.repeats);
    let mset=[];
    if(ax.mosfet) mset=Array.isArray(ax.mosfet)?ax.mosfet.slice():[ax.mosfet];
    else if(ps.mosfet) mset=[ps.mosfet];
    else if(ps.lp&&!ps.hp) mset=["lp"]; else if(ps.hp&&!ps.lp) mset=["hp"];
    setMos(mset);
  } else if(viewingOpenRigSweep() && state.grid){          // fallback: derive from the open live grid
    const xs=state.grid.xs||[], ys=state.grid.ys||[];
    if(xs.length){ setv("f-wmin",xs[0]); setv("f-wmax",xs[xs.length-1]); }
    if(ys.length){ setv("f-omin",ys[0]); setv("f-omax",ys[ys.length-1]); const st=minStep(ys); if(st) setv("f-ostep",st); }
    const rep=maxCellCount(state.grid.cells); if(rep>0) setv("f-rep",rep);
  } }

async function refreshActiveContext(){
  try{
    const b=await (await fetch("/api/bootstrap")).json(); state.boot=b;
    state.projects=b.projects||state.projects; state.rigProject=b.active_project||state.rigProject;
    if(!state.activeProject) state.activeProject=state.rigProject;
    if(b.active&&b.active.sweep_id){ state.rigSweep=b.active.sweep_id; if(!browseOnly()) state.activeSweep=b.active.sweep_id; }
    applyTargetState(b.target_state||{}); updateProjName(); if(!browseOnly()) populateControlFromLive();
  }catch(e){}
}

/* ---------------- navigation ---------------- */
const PAGES={home:"Home",campaigns:"Campaigns",live:"Live Sweep",paramdb:"Parameter DB",instruments:"Instruments",settings:"Settings"};
function loadPane(name, loader){
  Promise.resolve().then(loader).catch(err=>{
    console.error("Unable to load "+name+" pane",err);
    toast(name+" data is temporarily unavailable; the rest of GlitchLab is still usable.","danger");
  });
}
function navigate(p,opts){ if(!PAGES[p])return; opts=opts||{}; state.page=p;
  if(opts.history!==false){
    const nextUrl=new URL(location.href); nextUrl.hash=p;
    const entry={...(history.state||{}),page:p};
    if(opts.replace) history.replaceState(entry,"",nextUrl);
    else if(history.state?.page!==p) history.pushState(entry,"",nextUrl);
  }
  if(p!=="instruments"){ stopMirror();
    // release the scope's single video socket when we navigate away from the live pane, not just on
    // whole-window hide — otherwise this window keeps the socket locked while showing another page.
    const fr=document.getElementById("scope-iframe");
    if(fr&&fr.getAttribute("src")!=="about:blank"){ state._scopeHidden=false;
      clearScopeReclaim(); fr.setAttribute("src","about:blank"); } }
  document.querySelectorAll(".view").forEach(el=>el.classList.toggle("active",el.dataset.page===p));
  document.querySelectorAll(".nav").forEach(el=>el.classList.toggle("active",el.dataset.page===p));
  document.getElementById("crumb").textContent=PAGES[p];
  if(p==="home") loadPane("Home",loadHome); if(p==="campaigns") loadPane("Campaigns",loadCampaigns);
  if(p==="live") loadPane("Live Sweep",refreshLive);
  if(p==="instruments") loadPane("Instruments",loadInstruments);
  if(p==="paramdb") loadPane("Parameter DB",loadParamDb); if(p==="settings") loadPane("Settings",loadSettings);
  reportState();
}
window.addEventListener("popstate",event=>{
  const page=(event.state&&event.state.page)||location.hash.slice(1)||"live";
  navigate(PAGES[page]?page:"live",{history:false});
});
async function loadHome(){ let ov; try{ ov=await (await fetch(overviewUrl())).json(); }catch(e){ ov={attempts:0,successes:0,campaigns:[]}; }
  state.campaigns=ov.campaigns||[];
  const b=state.boot||{}; const cm=b.capability_manifest||{}; const gl=cm.glitcher||{}, sc=b.scope||{}, sp=b.scope_policy||{};
  document.getElementById("home-hero").innerHTML=[
    ["Total attempts",(ov.attempts||0).toLocaleString(),"across "+(ov.campaigns.length)+" campaigns","§Total attempts§Every injection ever recorded in this store, across all campaigns."],
    ["Candidates",(ov.candidate_successes||0).toLocaleString(),"connector positives awaiting full gates","§Candidates§Legacy success rows that do not pass the complete project-connector and raw-connection contract. A verified bit alone is insufficient."],
    ["Fully confirmed",(ov.confirmed_successes||0).toLocaleString(),"complete persisted connector contract","§Fully confirmed§Verified attempts whose project connector stored complete connection, target-state and runtime evidence."],
    ["Connections",gl.bound&&(sc.bound||sp.project_evidence_owned)?"ready":"not connected",gl.bound?(sp.project_evidence_owned?"glitcher connected · project evidence active":sc.bound?"glitcher and scope connected":"glitcher connected · scope unavailable"):"glitcher unavailable","§Connections§Connection availability only—not a health verdict. Run staged preflight before a live epoch."]
  ].map(([l,v,s,tip])=>`<div class="herocard" data-tip="${esc(tip)}"><div class="hlab">${l}</div><div class="hval">${v}</div><div class="hsub">${s}</div></div>`).join("");
  document.getElementById("home-rig").innerHTML=kv({target:(cm.rig&&cm.rig.target_model)||"unknown",
    package:(cm.rig&&cm.rig.target_package)||"unknown", glitcher:gl.id||"—",
    "pulse cycles≤":(cm.limits_in_force&&cm.limits_in_force.glitch&&cm.limits_in_force.glitch.pulse_cycles_max)||"—",
    "Vcc≤":(cm.limits_in_force&&cm.limits_in_force.target_power&&cm.limits_in_force.target_power.vcc_max_v)||"—"});
  document.getElementById("home-inst").innerHTML=kv({scope:sc.idn?sc.idn.split(",").slice(1,2)[0]:"unbound",
    resource:sc.resource||"—", "driven by":sc.driven_by_mcp?"MCP":"idle"});
  document.getElementById("home-campaigns").innerHTML=campCards(ov.campaigns);
}
function overviewUrl(){ return "/api/overview?project_id="+encodeURIComponent(state.activeProject||state.rigProject||""); }
async function loadCampaigns(){ let ov; try{ ov=await (await fetch(overviewUrl())).json(); }catch(e){ ov={campaigns:[]}; }
  state.campaigns=ov.campaigns||[];
  renderCampaignCards();
}
function filterCampaigns(value){ state.campaignQuery=String(value||"").trim().toLowerCase(); renderCampaignCards(); }
function renderCampaignCards(){
  const all=state.campaigns||[], q=state.campaignQuery||"";
  const rows=q?all.filter(c=>[c.name,c.objective,c.target,c.mode,c.id].some(v=>String(v||"").toLowerCase().includes(q))):all;
  const count=document.getElementById("camp-count"), cards=document.getElementById("camp-cards");
  if(count) count.textContent=q?`${rows.length} of ${all.length} campaigns`:`${all.length} campaigns`;
  if(cards) cards.innerHTML=rows.length?campCards(rows):(q?'<div class="sub" style="padding:8px">No campaigns match this search.</div>':campCards(rows));
}
function campCards(camps){ if(!camps||!camps.length) return '<div class="sub" style="padding:8px">No campaigns yet — Start a sweep on the Live Sweep board.</div>';
  // Running campaigns sort to the top and are visually distinct (accent border + pulsing LIVE
  // badge + the active sweep's name). With 40+ campaigns on file the active one was previously
  // impossible to pick out at a glance.
  const ordered=[...camps].sort((a,b)=>(b.running?1:0)-(a.running?1:0));
  return ordered.map(c=>{
    const live=!!c.running;
    const tip=`§${esc(c.name)}§${live?"● RUNNING NOW — "+esc(c.running_sweep||"sweep in progress")+" · ":""}`
      +`${esc(c.objective||'campaign')} · ${c.attempts} attempts · ${c.candidate_successes||0} candidates · ${c.confirmed_successes||0} confirmed · click to open`;
    return `<div class="campcard${live?" live":""}" data-tip="${tip}" onclick="openCampaign('${c.id}')">
    <div class="cn">${live?'<span class="livedot"></span>':""}${esc(c.name)}</div>
    <div class="cm">${esc(c.target||'unknown')} · ${c.mode||'full'} · ${c.id}</div>
    ${live?`<div class="crun">▶ ${esc(c.running_sweep||"sweep running")}</div>`:""}
    <div class="cbar">${live?'<span class="badge live">LIVE</span>':""}<span class="badge acc">Σ ${(c.attempts||0).toLocaleString()}</span><span class="badge">? ${c.candidate_successes||0}</span><span class="badge ok">✓ ${c.confirmed_successes||0}</span></div></div>`;
  }).join("");
}
function openCampaign(cid){ const c=(state.campaigns||[]).find(x=>x.id===cid);
  state.scope={kind:"campaign",id:cid,name:c?c.name:cid}; state.activeSweep=null;
  document.getElementById("crumb-camp").textContent=(c?c.name:cid);
  populateControlFromLive();
  updateControlUI();
  toast("Opening "+(c?c.name:cid)+" · "+(c?c.candidate_successes:"?")+" candidates · "+(c?c.confirmed_successes:"?")+" confirmed","info");
  navigate("live"); refreshLive(); }
/* scope = which data the Live board shows: a live/selected sweep, or an aggregated campaign */
function curScope(){ return state.scope || (state.activeSweep?{kind:"sweep",id:state.activeSweep}:null); }
function scopedGridUrl(){ const s=curScope(); return s.kind==="campaign"?`/api/campaign/${s.id}/grid`:`/api/sweep/${s.id}/grid`; }
function gridUrl(){ if(state.psScope==="all") return "/api/paramspace/project?project_id="+encodeURIComponent(state.activeProject||state.rigProject||"");
  return scopedGridUrl(); }
function mapScopeLabel(){ const sc=curScope(); if(state.psScope==="all") return "all campaigns in project";
  return sc&&sc.kind==="sweep"?"current sweep":"selected campaign"; }
function togglePsScope(){ state.psScope = (state.psScope==="all") ? "campaign" : "all";
  const c=document.getElementById("ps-scope");
  if(c){ c.textContent = state.psScope==="all"?"MAP: PROJECT":"MAP: CAMPAIGN"; c.classList.toggle("on", state.psScope==="all"); }
  toast("Heatmap coverage: "+mapScopeLabel()+". Attempt rows remain on the selected campaign.","info");
  refreshLive(); }
function summaryUrl(){ const s=curScope(); return s.kind==="campaign"?`/api/campaign/${s.id}/summary`:`/api/sweep/${s.id}/summary`; }
function attemptsUrl(lim,outcome){ const s=curScope(); const oc=outcome?("&outcome="+outcome):"";
  return s.kind==="campaign"?`/api/campaign/${s.id}/attempts?limit=${lim}${oc}`:`/api/attempts?sweep_id=${s.id}&limit=${lim}${oc}`; }
async function loadAttempts(){ const sc=curScope(); if(!sc){ state.attempts=[]; renderTable(); return; }
  const lim=state.filter?500:250;
  try{ const at=await (await fetch(attemptsUrl(lim,state.filter))).json();
    state.attempts=(at.attempts||[]).map(a=>({id:a.id,seq:a.seq,outcome:a.outcome_class,width:a.width,offset:a.offset,
      voltage:a.voltage,repeat:a.repeat,conf:a.outcome_confidence,ms:a.duration_ms,
      verified:a.classification==="fully_confirmed",classification:a.classification,source:a.verdict_source,notes:a.notes,oracle:a.oracle_summary,
      dip:a.dip_min_V,depth:a.dip_depth_V,wave:a.wave,pinWave:a.pin_wave,pinDip:a.pin_dip_min_V,
      pulseWidthNs:a.pulse_width_ns,injectionDelayUs:a.trigger_to_injection_us,signalMin:a.observed_signal_min_v,
      attemptValid:a.attempt_valid,validity:a.validity,requested:a.requested||{},readback:a.readback||{},
      effective:a.effective_settings||{},phase:a.phase||{},trigger:a.trigger||{},oracleState:a.oracle_state||{},
      waveformAvailable:!!a.waveform_available,outcomeDetail:a.outcome_detail||"",
      evidenceChecks:a.required_evidence_checks||{},evidenceFailed:a.required_evidence_failed||[],
      evidencePassed:a.required_evidence_passed||0,evidenceTotal:a.required_evidence_total||0}));
    if(state.confirmationFilter){
      state.attempts=state.attempts.filter(a=>state.confirmationFilter==="confirmed" ? a.classification==="fully_confirmed" : a.outcome==="success"&&a.classification!=="fully_confirmed");
      state.attemptsTotal=state.attempts.length;
    } else state.attemptsTotal=at.total||state.attempts.length;
    state.invalidAttempts=at.invalid||0; state.coverageValid=at.coverage_valid??(state.attemptsTotal-state.invalidAttempts);
  }catch(e){ state.attempts=[]; state.attemptsTotal=0; }
  renderTable();
  // Start only after the rows are attached and painted. This avoids the first
  // page-load race where an initial refresh replaced table cells mid-hydration.
  requestAnimationFrame(()=>hydrateWaveforms(state.attempts));
}
function statsUrl(m){ const s=curScope(); return s.kind==="campaign"?`/api/stats?metric=${m}&campaign_id=${s.id}`:`/api/stats?metric=${m}&sweep_id=${s.id}`; }
function esc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
function kv(o){ return Object.entries(o).map(([k,v])=>`<span class="k">${k}</span><span class="v">${esc(v)}</span>`).join(""); }

/* ---------------- projects ---------------- */
function updateProjName(){ const p=(state.projects||[]).find(x=>x.id===state.activeProject);
  const el=document.getElementById("proj-name"); if(el) el.textContent=(p?p.name:"Default project")+(browseOnly()?" · viewing":""); }
function browseOnly(){ return !!(state.rigProject&&state.activeProject&&state.rigProject!==state.activeProject); }
function toggleProjMenu(e){ if(e)e.stopPropagation(); const m=document.getElementById("projmenu");
  const show=!m.classList.contains("show"); m.classList.toggle("show",show); if(show){ renderProjMenu();
    const r=document.getElementById("projsel").getBoundingClientRect(); m.style.left=r.left+"px"; m.style.top=(r.bottom+6)+"px"; } }
function renderProjMenu(){ document.getElementById("pm-list").innerHTML=(state.projects||[]).map(p=>
  `<div class="pm-row ${p.id===state.activeProject?'cur':''}" onclick="setProject('${p.id}',event)">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="${p.id===state.activeProject?'var(--acc)':'var(--tx3)'}" stroke-width="1.8" stroke-linecap="round"><path d="M3 7.5 5 5h5l2 2.5h7v11H3z"/></svg>
    <div style="flex:1;min-width:0"><div class="pn">${esc(p.name)}</div><div class="pmeta">${p.campaigns} campaigns · Σ ${(p.attempts||0).toLocaleString()} · ${p.successes||0} success-class</div></div>
    ${p.id===state.rigProject?'<span class="badge">rig</span>':''}${p.id===state.activeProject?'<span class="badge acc">viewing</span>':''}</div>`).join(""); }
function setProject(pid,e){ if(e)e.stopPropagation(); document.getElementById("projmenu").classList.remove("show");
  if(pid===state.activeProject) return; state.activeProject=pid; state.scope=null; state.activeSweep=null;
  updateProjName(); reProject(); toast("Project view changed. Any running campaign continues unchanged.","info"); }
function createProject(e){ if(e)e.stopPropagation(); const inp=document.getElementById("pm-newname");
  const name=(inp.value||"").trim(); if(!name) return; inp.value="";
  document.getElementById("projmenu").classList.remove("show"); act("new_project",{name});
  toast("Creating project "+name,"info"); }

/* ---------------- deterministic workflow state ---------------- */
function workflowUrl(){ const sc=curScope();
  if(!sc) return "/api/workflow?recent_attempts=3";
  const key=sc.kind==="campaign"?"campaign_id":"sweep_id";
  return `/api/workflow?${key}=${encodeURIComponent(sc.id)}&recent_attempts=3`; }
function setGate(name,status,label){ const dot=document.getElementById(`gate-${name}-dot`), val=document.getElementById(`gate-${name}`);
  if(dot){ dot.className="gdot "+String(status||"").replace(/[^a-z_]/g,"_"); }
  if(val) val.textContent=label==null?"—":String(label); }
function axisBounds(axis){
  if(Array.isArray(axis)){ const nums=axis.map(Number).filter(Number.isFinite); return {min:Math.min(...nums),max:Math.max(...nums),step:nums.length>1?Math.abs(nums[1]-nums[0]):1}; }
  if(axis&&typeof axis==="object"){
    const min=Number(axis.min??axis.start), max=Number(axis.max??axis.stop??axis.min??axis.start);
    return {min,max,step:Number(axis.step??1)};
  }
  const n=Number(axis); return {min:n,max:n,step:1};
}
function setUiDisabled(el,disabled){ if(!el)return; el.classList.toggle("disabled",!!disabled);
  el.setAttribute("aria-disabled",disabled?"true":"false"); }
function timingLabel(status){ return ({
  captured_this_session:"captured this session",
  profile_managed_known_envelope:"profile-managed known envelope",
  profile_managed_pending_preflight:"profile envelope pending preflight",
  not_captured:"not captured"
})[status]||(status||"—").replace(/_/g," "); }
function applyTargetState(ts){ ts=ts||{state:"clear",blocking:false}; state.targetState=ts;
  const blocking=!!ts.blocking, banner=document.getElementById("target-state-banner");
  if(banner) banner.hidden=!blocking;
  const title=document.getElementById("target-state-title"), reason=document.getElementById("target-state-reason"), source=document.getElementById("target-state-source");
  if(title) title.textContent=ts.unknown_held?"TARGET STATE UNKNOWN / HELD — NO ACTUATION":"TARGET STATE PRESERVED — NO ACTUATION";
  if(reason) reason.textContent=ts.reason||"The durable target-state interlock is active.";
  if(source) source.textContent=[ts.source,ts.sweep_id?`sweep ${ts.sweep_id}`:null,ts.sweep_status].filter(Boolean).join(" · ")||"persisted interlock";
  setUiDisabled(document.querySelector('[data-mcp="run_preflight"]'),blocking);
  setUiDisabled(document.getElementById("btn-readconnector"),blocking);
  setUiDisabled(document.querySelector('[data-mcp="discover_timing"]'),blocking);
  if(!state.running) setUiDisabled(document.getElementById("btn-startstop"),blocking);
  setFieldsEnabled(!state.running&&!blocking);
  if(typeof applyScopePolicyControls==="function") applyScopePolicyControls(state.scopePolicy||{});
  updateControlUI();
}
function applyProjectProfile(profile, connectResult){
  profile=profile||{}; const recipe=profile.default_recipe||{}, ax=recipe.axes||{};
  if(!profile.id){ state.profileReady=false; return; }
  const fixed=profile.fixed_phase||{}, phase=(connectResult&&connectResult.phase)||{}, rb=phase.readback_steps||{};
  const phaseEl=document.getElementById("phase-profile"); if(phaseEl){
    const configured=(fixed.width_percent!=null&&fixed.offset_percent!=null)
      ? `${fixed.width_percent}% width / ${fixed.offset_percent}% offset`
      : "project defaults";
    phaseEl.textContent=`Configured: ${configured} · device readback: ${rb.width!=null?rb.width:"connect to read"} / ${rb.offset!=null?rb.offset:"connect to read"} steps`;
  }
  const pc=axisBounds(ax.pulse_cycles), eo=axisBounds(ax.ext_offset), setv=(id,v)=>{ const e=document.getElementById(id); if(e&&Number.isFinite(v))e.value=String(v); };
  if(!state.formDirty){
    if(Number.isFinite(pc.min)){ setv("f-wmin",pc.min); setv("f-wmax",pc.max); setv("f-wstep",pc.step); }
    if(Number.isFinite(eo.min)){ setv("f-omin",eo.min); setv("f-omax",eo.max); setv("f-ostep",eo.step); }
    setv("f-rep",Number(recipe.repeats_per_cell??recipe.samples_per_cell??1));
    const mos=Array.isArray(ax.mosfet)?ax.mosfet:[]; if(mos.length===1) ["lp","hp"].forEach(m=>{ const e=document.getElementById("mos-"+m); if(e)e.classList.toggle("on",mos[0]===m); });
  }
  const campaign=document.getElementById("f-campaign"); if(campaign&&(!campaign.value||campaign.value==="—")) campaign.value=profile.id+" · campaign";
  state.profileAppliedId=profile.id;
  state.profileReady=true;
}
function resetSweepDefaults(){ state.formDirty=false; applyProjectProfile((state.boot||{}).project_profile||{},
  ((((state.boot||{}).capability_manifest||{}).glitcher||{}).connect_result)); toast("Project defaults restored","info"); reportState(); }
function workflowReadyForLive(){ const by={}; ((state.workflow||{}).stages||[]).forEach(s=>by[s.name]=s);
  const timingReady=["captured_this_session","profile_managed_known_envelope"].includes(by.physical_timing?.status);
  return by.project_profile?.status==="ready" && by.husky_connection?.status==="ready" &&
    by.preflight?.status==="passed" && timingReady &&
    by.target_acknowledgment?.status==="ready" && !targetInterlocked();
}
window.runPreflight=()=>{ if(targetInterlocked()){ toast("Preflight disabled while target state is held","danger"); return; } act("preflight_check",{}); };
window.discoverTiming=function(){ const by={}; ((state.workflow||{}).stages||[]).forEach(s=>by[s.name]=s);
  if(targetInterlocked()){ toast("Timing discovery disabled while target state is held","danger"); return; }
  const d=(by.physical_timing&&by.physical_timing.detail)||{};
  if(d.project_evidence_owned){ toast("Project evidence owns the companion scope; preflight validates its timing policy","caution"); return; }
  act("discover_timing",{}); };
window.acknowledgeTarget=function(){ const by={}; ((state.workflow||{}).stages||[]).forEach(s=>by[s.name]=s);
  const d=(by.target_acknowledgment&&by.target_acknowledgment.detail)||{};
  if(!d.target_model||!d.required_limits){ toast("Load workflow state before acknowledging","danger"); return; }
  act("acknowledge_target",{target_model:d.target_model,stated:d.required_limits});
};
async function loadWorkflow(){ try{
    const w=await (await fetch(workflowUrl())).json(); state.workflow=w; renderWorkflow(w);
  }catch(e){ const chip=document.getElementById("workflow-overall"); if(chip){ chip.textContent="UNAVAILABLE"; chip.className="chip"; } } }
function renderWorkflow(w){ w=w||{}; const by={}; (w.stages||[]).forEach(s=>by[s.name]=s);
  const target=by.target_state||{}, profile=by.project_profile||{}, husky=by.husky_connection||{}, pre=by.preflight||{}, timing=by.physical_timing||{}, ackg=by.target_acknowledgment||{};
  applyTargetState(w.target_state||target.detail||{});
  setGate("target",target.status,target.status?target.status.replace(/_/g," "):"clear");
  setGate("profile",profile.status,(profile.detail&&(profile.detail.connector_id||profile.detail.oracle_plugin))||profile.status||"—");
  setGate("husky",husky.status,(husky.detail&&husky.detail.bound)?"BOUND":(husky.status||"—").replace(/_/g," "));
  setGate("preflight",pre.status,(pre.status||"—").replace(/_/g," "));
  setGate("timing",timing.status,timingLabel(timing.status));
  setGate("ack",ackg.status,(ackg.status||"—").replace(/_/g," "));
  applyProjectProfile(w.project_profile||profile.detail||{}, husky.detail&&husky.detail.connect_result);
  const timingBtn=document.querySelector('[data-mcp="discover_timing"]');
  if(timingBtn){ const owned=!!(timing.detail&&timing.detail.project_evidence_owned), blocked=owned||targetInterlocked();
    timingBtn.textContent=owned?"2 · project timing":"2 · timing";
    setUiDisabled(timingBtn,blocked); }
  const counts=w.counts||{}, candidateTotal=(counts.candidates||0)+(counts.partial_candidates||0);
  setGate("candidate",candidateTotal?"candidate":"none",candidateTotal);
  setGate("confirmed",counts.confirmed?"confirmed":"none",counts.confirmed||0);
  const chip=document.getElementById("workflow-overall"); let text="READY", cls="chip ok";
  if(targetInterlocked()){ text=state.targetState.unknown_held?"TARGET UNKNOWN / HELD":"TARGET PRESERVED"; cls="chip"; }
  else if(counts.confirmed){ text="FULLY CONFIRMED"; cls="chip ok"; }
  else if(candidateTotal){ text="CANDIDATE — VERIFY"; cls="chip"; }
  else if(profile.status!=="ready"||husky.status==="health_unknown"||pre.status==="failed"){ text="BLOCKED"; cls="chip"; }
  else if(husky.status!=="ready"||pre.status!=="passed"||!["captured_this_session","profile_managed_known_envelope"].includes(timing.status)||ackg.status!=="ready"){ text="GATES REQUIRED"; cls="chip"; }
  if(chip){ chip.textContent=text; chip.className=cls; }
  const next=w.next_action||{}, nxt=document.getElementById("workflow-next");
  if(nxt) nxt.textContent=`Next: ${next.tool||"—"}${next.reason?" · "+next.reason:""}`;
}
async function reProject(){ try{
    const ov=await (await fetch("/api/overview?project_id="+encodeURIComponent(state.activeProject||""))).json();
    state.projects=ov.projects||state.projects; state.campaigns=ov.campaigns||[]; state.activeProject=ov.project_id||state.activeProject;
    const best=preferredCampaign(ov.campaigns,state.boot&&state.boot.active&&state.boot.active.campaign_id);
    if(best){ state.scope={kind:"campaign",id:best.id,name:best.name}; document.getElementById("crumb-camp").textContent=best.name; }
    else { state.scope=null; state.activeSweep=null; document.getElementById("crumb-camp").textContent="—"; }
    updateProjName(); refreshLive();
    if(state.page==="home") loadHome(); if(state.page==="campaigns") loadCampaigns();
  }catch(e){} }
function preferredCampaign(campaigns,activeId){
  const rows=campaigns||[];
  if(activeId){ const active=rows.find(c=>c.id===activeId||c.campaign_id===activeId); if(active)return active; }
  // Store APIs return campaigns newest-first. Preserve that chronology rather
  // than selecting an older campaign merely because it ran more shots.
  return rows.find(c=>Number(c.attempts||0)>0)||rows[0]||null;
}
document.querySelectorAll(".nav").forEach(el=>el.addEventListener("click",()=>navigate(el.dataset.page)));

/* ---------------- live events ---------------- */
function onEvent(kind,data){
  if(kind==="sweep_defined"){ state.rigSweep=data.sweep_id; state.sweepTotal=(data.total!=null?data.total:null);
    const preserveOpenCampaign=state.scope&&state.scope.kind==="campaign"&&!viewingOpenRigSweep();
    if(!browseOnly()&&!preserveOpenCampaign){ state.activeSweep=data.sweep_id; state.scope={kind:"sweep",id:data.sweep_id};
      state.grid={xs:[],ys:[],cells:{},x_name:"width",y_name:"ext-offset"}; state.totals={}; state.sumTotal=0;
      state.attempts=[]; state.ratehist=[]; refreshLive(); }
    refreshActiveContext(); }
  else if(kind==="sweep_started"){ state.running=true; state.paused=false; state.stopping=false; state.lastSweepStatus=null; state.t0=Date.now();
    if(viewingOpenRigSweep()) document.getElementById("ctl-status").textContent="running";
    if(!browseOnly()) populateControlFromLive(); refreshActiveContext(); updateControlUI();
    logAgent("control_sweep","start "+(data.points||"")); toast("Sweep started","info"); }
  else if(kind==="target_prepared"){ toast("Target preflight · "+(data.ok?"passed":"failed"), data.ok?"info":"danger"); loadWorkflow(); }
  else if(kind==="attempt_recorded"){ if(!browseOnly()) ingest(data); }
  else if(kind==="sweep_progress"){ if(!browseOnly()) progress(data); }
  else if(kind==="success"){ const o=data.params.ext_offset??data.params.offset;
    toast("Success-class row persisted — validating complete evidence · pulse="+data.params.width+" off="+o,"caution");
    logAgent("evidence_validation","attempt "+(data.attempt_id||"?")+" · pulse="+data.params.width+" off="+o); loadWorkflow(); }
  else if(kind==="candidate_preserved"||kind==="candidate_preservation_latched"){ toast(data.verified?"Fully confirmed state preserved":"Incomplete/unknown target state preserved",data.verified?"success":"caution"); loadWorkflow(); }
  else if(kind==="sweep_done"){ state.running=false; state.paused=false; state.stopping=false;
    const valid=data.done??0, planned=data.planned_valid_attempts??((data.timing&&data.timing.total)!=null?data.timing.total:"?");
    let status, message, level;
    if(data.candidate_preserved){ status=`candidate preserved · ${valid}/${planned} valid`;
      message=`Target state preserved after ${valid}/${planned} valid shots. Inspect/export persisted evidence.`; level="caution"; }
    else if(data.infrastructure_failure){ status=`infrastructure stop · ${valid}/${planned} valid`;
      message=`Infrastructure stop after ${valid}/${planned} valid shots. Inspect the held state and failure before any resume.`; level="danger"; }
    else if(data.status==="aborted"||data.operator_stopped){ status=`operator stopped · ${valid}/${planned} valid`;
      message=`Sweep was stopped by the operator after ${valid}/${planned} valid shots; ${Math.max(0,(Number(planned)||0)-valid)} remain.`; level="caution"; }
    else if(data.status==="incomplete"){ status=`incomplete · ${valid}/${planned} valid`;
      message=`Sweep invocation ended before the plan was complete: ${valid}/${planned} valid shots. Resume the same immutable plan.`; level="caution"; }
    else if(data.status==="done"){ status=`done · ${valid}/${planned} valid · ${data.successes||0} success-class`;
      message=`Sweep plan finished · ${valid}/${planned} valid shots; check evidence.`; level=(data.successes?"caution":"info"); }
    else { status=`${data.status||"unknown stop"} · ${valid}/${planned} valid`;
      message=`Sweep stopped with status ${data.status||"unknown"}; inspect persisted state before continuing.`; level="danger"; }
    state.lastSweepStatus=status; updateControlUI(); document.getElementById("ctl-status").textContent=status;
    toast(message,level); if(!browseOnly()) refreshLive(); loadWorkflow(); }
  else if(kind==="sweep_dry_run"){
    const ok=!!(data.result&&data.result.ok);
    document.getElementById("ctl-status").textContent=ok?"dry-run passed · exact plan saved":"dry-run failed";
    toast(ok?"Dry-run passed. Turn DRY-RUN off to execute this exact immutable plan.":"Dry-run failed; inspect the refusal before live execution",ok?"success":"danger");
    logAgent("sweep_dry_run",ok?"validated "+data.sweep_id:"failed"); loadWorkflow(); }
  else if(kind==="danger_state"){ applyDanger(data); }
  else if(kind==="sweep_refused"){ document.getElementById("ctl-status").textContent="sweep refused"; showRefusal(data.rule+": "+data.detail); loadWorkflow(); }
  else if(kind==="sweep_stopped_preflight_failure"){ state.running=false;
    state.lastSweepStatus="preflight stop · 0 valid"; updateControlUI(); document.getElementById("ctl-status").textContent=state.lastSweepStatus;
    showRefusal("preflight failure: "+(data.detail||"inspect staged evidence")); toast("Sweep did not start: preflight failed","danger"); loadWorkflow(); }
  else if(kind==="sweep_stopped_parameter_refusal"){ document.getElementById("ctl-status").textContent="parameter refusal · inspect";
    showRefusal(data.detail||data.violated_rule||"parameter refused"); toast("Sweep stopped on a parameter safety refusal","danger"); loadWorkflow(); }
  else if(kind==="sweep_stopped_persistence_failure"){ document.getElementById("ctl-status").textContent="persistence stop · target preserved";
    showRefusal(data.detail||"post-shot persistence failure"); toast("Persistence failed after a shot; target state was preserved","danger"); loadWorkflow(); }
  else if(kind==="scope_bound"){ loadInstruments(); toast("Scope bound: "+(data.idn||""),"info"); logAgent("scope_bind","ok"); }
  else if(kind==="scope_unbound"){ loadInstruments(); toast("Companion scope session released","info"); logAgent("scope_unbind","ok"); }
  else if(kind==="notification_test"){ const ok=!!data.accepted; toast(ok?"Notification test queued":"Notification test could not be queued",ok?"success":"caution"); if(state.page==="settings") setTimeout(loadSettings,350); }
  else if(kind==="notification_settings"){ toast("Private notification settings saved",data.enabled?"success":"info"); if(state.page==="settings") setTimeout(loadSettings,100); }
  else if(kind==="firmware_flashed"){ toast("Firmware flashed: "+(data.ok?"OK":"FAIL"),data.ok?"success":"danger"); logAgent("flash_target",data.ok?"ok":"fail"); }
  else if(kind==="scope_capture"){ state.lastCap=data; updateTrace(); logAgent("scope_capture",data.samples+" samp"); }
  else if(kind==="scope_measure"){ state.lastMeas=data.measurements; updateTrace(); }
  else if(kind==="recovery"){ toast("Target power-cycled","caution"); }
  else if(kind==="project_changed"){ fetch("/api/bootstrap").then(r=>r.json()).then(b=>{ state.boot=b;
      state.projects=b.projects||[]; state.activeProject=b.active_project;
      applyTargetState(b.target_state||{});
      if(data.created) toast("Project created","success"); updateProjName(); reProject(); }); }
  else if(kind==="project_namespace_created"){ fetch("/api/bootstrap").then(r=>r.json()).then(b=>{
      state.boot=b; state.projects=b.projects||[]; renderProjMenu();
      toast("Analysis namespace created; restart with --project-profile to use it live","info"); }); }
  else if(kind==="connector_read"){ renderConnectorRead(data); loadWorkflow(); }
  else if(kind==="preserved_target_state_discarded"){ fetch("/api/bootstrap").then(r=>r.json()).then(b=>{ state.boot=b; applyTargetState(b.target_state); loadWorkflow(); loadInstruments(); }); }
  else if(kind==="action_refused"){ const message=(data.reason||data.detail||"refused");
    toast((data.action||"action")+": "+message,"danger"); showRefusal(message);
    if(data.action==="start_sweep"){ const st=document.getElementById("ctl-status"); if(st)st.textContent="start refused"; }
    if(data.target_state||data.violated_rule==="preserved_target_state_interlock") loadWorkflow(); }
  else if(kind==="preflight"||kind==="preflight_result"||kind==="husky_health"||kind==="glitcher_health"||kind==="oracle_health"||kind==="physical_timing"||kind==="timing_result"||kind==="target_acknowledged"){ loadWorkflow(); }
  else if(kind==="audit"){ /* reflected in settings */ }
}

/* ---------- current-state connector read ---------- */
window.readConnector=function(){
  if(targetInterlocked()){ toast("Connector read disabled while target state is held; inspect persisted evidence","danger"); return; }
  const b=document.getElementById("btn-readconnector");
  if(b){ b.dataset.txt=b.textContent; b.textContent="… reading"; b.style.pointerEvents="none"; b.style.opacity=".55"; }
  const badge=document.getElementById("connector-badge");
  if(badge){ badge.textContent="running a read-only connector observation…"; badge.style.color="var(--tx3)"; badge.style.borderColor="var(--line)"; }
  act("read_connector",{});
};
function renderConnectorRead(d){
  d=d||{};
  const b=document.getElementById("btn-readconnector");
  if(b){ b.textContent="⟳ Check current state"; b.style.pointerEvents=""; b.style.opacity=""; }
  const badge=document.getElementById("connector-badge"), kv=document.getElementById("connector-kv"), ev=document.getElementById("connector-evidence");
  const v=d.verdict||d.outcome||"?", connectorPassed=d.confirmed===true||d.verified===true||d.evidence_complete===true;
  let txt,col;
  if(connectorPassed){ txt="CONNECTOR PASSED · CAMPAIGN EVIDENCE STILL REQUIRED"; col="var(--caution)"; }
  else if(d.partial_candidate_observed===true||v==="candidate"){ txt="PARTIAL CONNECTION EVIDENCE · PRESERVE AND TRIAGE"; col="var(--caution)"; }
  else if(v==="no-effect"){ txt="NO TARGET EFFECT OBSERVED"; col="var(--tx3)"; }
  else if(v==="reset"){ txt="INVALID TARGET STATE · RESET/POWER GATE FAILED"; col="var(--danger)"; }
  else if(v==="exception"){ txt="INFRASTRUCTURE ERROR · NOT A GLITCH"; col="var(--danger)"; }
  else { txt="UNCONFIRMED CONNECTOR RESULT · "+String(v).toUpperCase(); col="var(--tx3)"; }
  if(badge){ badge.textContent=txt; badge.style.color=col; badge.style.borderColor=col; }
  const rows=[];
  if(d.verdict!=null) rows.push(["VERDICT",d.verdict]);
  if(d.latency_ms!=null) rows.push(["connector latency",String(d.latency_ms)+" ms"]);
  if(d.target_voltage_mv!=null) rows.push(["target voltage",d.target_voltage_mv+" mV"]);
  else if(d.vtref_mv!=null) rows.push(["target voltage",d.vtref_mv+" mV"]);
  if(d.connection_healthy!=null) rows.push(["connection healthy",String(d.connection_healthy)]);
  if(d.runtime_confirmed!=null) rows.push(["runtime confirmed",String(d.runtime_confirmed)]);
  if(d.evidence_complete!=null) rows.push(["evidence complete",String(d.evidence_complete)]);
  if(d.failure_stage) rows.push(["failed stage",String(d.failure_stage)]);
  if(d.preserve_target!=null) rows.push(["preserve target",String(d.preserve_target)]);
  if(kv) kv.innerHTML=rows.map(r=>`<span class="k">${esc(r[0])}</span><span class="v">${esc(r[1])}</span>`).join("");
  const failed=[]; function walk(o,p){ if(!o||typeof o!=="object")return; Object.entries(o).forEach(([k,x])=>{
      const q=p?`${p}.${k}`:k; if(x===false) failed.push(q);
      else if(x&&typeof x==="object") walk(x,q); }); }
  walk(d.checks||{},"");
  const stamp=new Date().toLocaleTimeString();
  const summary=d.evidence||d.summary||d.attach_error||d.error||d.reason||(failed.length?"Failed checks: "+failed.slice(0,12).join(", "):"Connector evidence returned; inspect the persisted attempt contract for full details.");
  if(ev) ev.textContent=summary+"\n— checked current state at "+stamp;
  toast("Connector: "+txt,(v==="exception"||v==="reset")?"danger":"caution");
  logAgent("connection_check_current_state",(connectorPassed?"connector_passed":"unconfirmed")+(d.failure_stage?(" · "+d.failure_stage):""));
}
window.inspectEvidence=async function(attemptId){
  const badge=document.getElementById("connector-badge"), ev=document.getElementById("connector-evidence"), kvh=document.getElementById("connector-kv");
  if(badge){ badge.textContent=`loading attempt ${attemptId} evidence…`; badge.style.color="var(--tx3)"; }
  try{ const e=await (await fetch(`/api/attempt/${attemptId}/evidence`)).json();
    const confirmed=e.classification==="fully_confirmed", candidate=!!e.candidate;
    if(badge){ badge.textContent=confirmed?"FULLY CONFIRMED · PERSISTED CONTRACT":candidate?"CANDIDATE · CONFIRMATION INCOMPLETE":"NOT A CONFIRMED GLITCH";
      badge.style.color=confirmed?"var(--ok)":candidate?"var(--caution)":"var(--danger)"; badge.style.borderColor=badge.style.color; }
    const a=e.attempt||{}, p=e.physical_timing&&e.physical_timing.summary||{}, o=e.connection||e.oracle||{}, aux=(e.environment&&e.environment.aux_telemetry)||{}, eff=aux.effective_settings||{}, frozen=eff.frozen_readback||{};
    const rows=[["attempt",attemptId],["classification",e.classification||"—"],["verified",String(!!a.verified)],
      ["campaign",a.campaign_name?`${a.campaign_name} (${a.campaign_id})`:(a.campaign_id||"—")],
      ["sweep",a.sweep_name?`${a.sweep_name} (${a.sweep_id})`:(a.sweep_id||"—")],
      ["valid shot",aux.attempt_valid==null?"legacy unknown":String(aux.attempt_valid)],
      ["connector",o.plugin||"—"],["failure stage",o.failure_stage||"—"],
      ["runtime confirmed",o.underlying_detail&&o.underlying_detail.runtime_confirmed!=null?String(o.underlying_detail.runtime_confirmed):"—"],
      ["ext-offset req → read",eff.ext_offset_requested!=null?`${eff.ext_offset_requested} → ${eff.ext_offset_readback}`:"—"],
      ["pulse req → read",eff.pulse_cycles_requested!=null?`${eff.pulse_cycles_requested} → ${eff.pulse_cycles_readback}`:"—"],
      ["MOSFET",eff.mosfet||"—"],["phase width / offset",frozen.phase_width_steps!=null?`${frozen.phase_width_steps} / ${frozen.phase_offset_steps} of ${frozen.phase_shift_steps}`:"—"],
      ["trigger",frozen.trigger_module?`${frozen.trigger_module} · ${frozen.trigger_edge} · ${frozen.trigger_level_v} V`:"—"],
      ["pulse width",p.pulse_width_ns!=null?p.pulse_width_ns+" ns":"—"],
      ["trigger → injection",p.trigger_to_injection_us!=null?p.trigger_to_injection_us+" µs":"—"],
      ["observed signal min",p.observed_signal_min_v!=null?p.observed_signal_min_v+" V":"—"]];
    if(kvh) kvh.innerHTML=rows.map(r=>`<span class="k">${esc(r[0])}</span><span class="v">${esc(r[1])}</span>`).join("");
    const failed=(o.failed_gates||[]), missing=(e.missing_confirmation_evidence||[]), required=o.required_checks||{};
    if(ev) ev.textContent=(failed.length?"Failed gates: "+failed.join(", ")+"\n":"")+
      (Object.keys(required).length?"Required project evidence: "+Object.entries(required).map(([k,v])=>`${k}=${v}`).join(", ")+"\n":"")+
      (missing.length?"Missing: "+missing.join("; ")+"\n":"")+(e.interpretation||"");
    toast(`Attempt ${attemptId}: ${e.classification}`,confirmed?"success":candidate?"caution":"danger");
  }catch(err){ if(ev) ev.textContent="Evidence read failed: "+String(err); }
};
function applyDanger(_d){}
function showRefusal(msg){ const r=document.getElementById("refusal"); document.getElementById("refusal-txt").textContent="Refused: "+msg;
  r.classList.add("show"); toast("Refused: "+msg,"danger"); logAgent("refused",msg); }

function ingest(d){ const o=d.params.ext_offset??d.params.offset;
  if(!state.filter || state.filter===d.outcome){
    state.attempts.unshift({id:d.attempt_id,seq:d.seq,outcome:d.outcome,width:d.params.width,offset:o,
      voltage:d.params.voltage,repeat:d.params.repeat,conf:d.confidence,ms:d.duration_ms,verified:!!d.verified,
      dip:d.dip_min_V,depth:d.dip_depth_V,attemptValid:null,validity:"refresh_pending",
      wave:d.wave,pinWave:d.pin_wave,pinDip:d.pin_dip_min_V});
    if(state.attempts.length>500) state.attempts.pop();
  }
  state.attemptsTotal=(state.attemptsTotal||0)+1;
  // The compact websocket event deliberately does not carry persisted aux/effective evidence.
  // Never infer validity from outcome: batch-refetch the authoritative row + valid-only grid.
  // Persisted aux/effective/connection fields are the source of truth. Throttle an
  // authoritative API refresh so a continuous sweep cannot starve the refresh
  // (a debounce would postpone it until attempts stopped arriving).
  if(!state._attemptRefresh){
    state._attemptRefresh=setTimeout(()=>{
      state._attemptRefresh=null;
      refreshLive();
    },750);
  }
  // throughput
  state._t=state._t||[]; state._t.push(Date.now()); if(state._t.length>40)state._t.shift();
  if(state._t.length>2){ const sp=(state._t[state._t.length-1]-state._t[0])/1000; if(sp>0) document.getElementById("rd-rate").textContent=((state._t.length-1)/sp).toFixed(1); }
  renderTable();
}
function fmtDur(s){ if(s==null) return "—"; s=Math.round(s); const m=Math.floor(s/60),h=Math.floor(m/60);
  return h>0?(h+"h"+(m%60)+"m"):(m>0?(m+"m"+(s%60)+"s"):(s+"s")); }
function fmtClock(s){ s=Math.max(0,Math.floor(s)); return String(Math.floor(s/3600)).padStart(2,"0")+":"+
  String(Math.floor(s/60)%60).padStart(2,"0")+":"+String(s%60).padStart(2,"0"); }
function progress(d){
  const t=d.timing;
  if(t&&t.total!=null) state.sweepTotal=t.total; else if(d.total!=null) state.sweepTotal=d.total;
  if(t){
    if(t.total!=null) document.getElementById("rd-eta").textContent =
      "· "+t.done+"/"+t.total+" ("+t.pct+"%) · "+t.s_per_attempt+"s/att · "+t.attempts_per_min+"/min · ETA "+fmtDur(t.eta_s);
    if(t.attempts_per_min) document.getElementById("rd-rate").textContent=(t.attempts_per_min/60).toFixed(2);
  } else {
    if(d.total) document.getElementById("rd-eta").textContent="· "+d.done+"/"+d.total;
  }
  if((d.done||0)%18===0) loadStats(); }

/* ---------------- data load ---------------- */
async function refreshLive(){ const sc=curScope();
  loadWorkflow();
  if(!sc && state.psScope!=="all"){ drawEmptyHeat(); renderTiles(); renderTable(); return; }
  const attemptsPromise=loadAttempts();
  let gridLoaded=false;
  try{
    const g=await (await fetch(gridUrl())).json();
    const cells={}; (g.cells||[]).forEach(c=>{ const k=c.x+","+c.y; cells[k]=cells[k]||{}; cells[k][c.c]=(cells[k][c.c]||0)+c.n; });
    state.grid={xs:(g.xs||[]).slice(),ys:(g.ys||[]).slice(),cells,x_name:g.x_name||"width",y_name:"ext-offset",
      samples:g.samples||[],samples_are_complete:!!g.samples_are_complete,sample_count:g.sample_count||0};
    state.mapTotals=g.totals||{};
    // Project coverage is deliberately map-only. Keep tiles, filters, rate,
    // and attempt totals aligned to the selected campaign/sweep.
    let totalsGrid=g;
    if(state.psScope==="all" && sc) totalsGrid=await (await fetch(scopedGridUrl())).json();
    state.totals=totalsGrid.totals||{}; state.sumTotal=Object.values(state.totals).reduce((a,b)=>a+b,0);
    document.getElementById("rd-total").textContent=state.sumTotal.toLocaleString();
    gridLoaded=true;
  }catch(e){ drawEmptyHeat(); }
  await attemptsPromise;
  renderTiles(); if(gridLoaded) drawHeatmap(); loadStats();
}
async function loadStats(){ const sc=curScope(); if(!sc) return;
  try{
    const [tb,rr,s]=await Promise.all([
      fetch(statsUrl("time_between_success")).then(r=>r.json()),
      fetch(statsUrl("rolling_rate")).then(r=>r.json()),
      fetch(summaryUrl()).then(r=>r.json())]);
    document.getElementById("s-med").textContent=(tb.median_attempts!=null?tb.median_attempts:"—");
    document.getElementById("s-mean").textContent=(tb.mean_attempts!=null?tb.mean_attempts:"—");
    document.getElementById("s-eta").textContent=(tb.eta_attempts!=null?(tb.eta_attempts+" att"):"—");
    const positives=(state.totals.candidate||0)+(state.totals.confirmed||0);
    document.getElementById("s-succ").textContent=(state.totals.confirmed||0);
    const validAttempts=state.sumTotal||0;
    const rate=(validAttempts?100*positives/validAttempts:0);
    document.getElementById("rr-cur").textContent=rate.toFixed(2)+"%";
    drawSpark(rr);
    if(s && s.suggested_refine_bbox) state._refine=s.suggested_refine_bbox;
    if(s && s.textmap) state._textmap=s.textmap;
  }catch(e){}
}

/* ---------------- heatmap (dense square-cell grid) ---------------- */
const PLOTX=54, PLOTY=8, PLOTW=798, PLOTH=360;
function fmt(v){ v=+v; if(Math.abs(v)>=1e6)return (v/1e6).toFixed(2)+"M"; if(Math.abs(v)>=1e3)return (v/1e3).toFixed(1)+"k"; return v%1===0?v:v.toFixed(1); }
function minStep(arr){ if(!arr||arr.length<2) return null; let d=Infinity;
  for(let i=1;i<arr.length;i++){ const dd=arr[i]-arr[i-1]; if(dd>0&&dd<d)d=dd; } return isFinite(d)?d:null; }
function maxCellCount(cells){ let m=0; for(const k in cells){ let s=0; for(const oc in cells[k]) s+=cells[k][oc]; if(s>m)m=s; } return m; }
// exact planned ext-offset extent from the live sweep's param_spec (D), when available
function plannedOffset(){ const a=(state.boot&&state.boot.active)||{}; const ax=(a.param_spec&&a.param_spec.axes)||{};
  if(ax.ext_offset && ax.ext_offset.max!=null) return {min:+ax.ext_offset.min, max:+ax.ext_offset.max}; return null; }
// Adaptive, VALUE-based binning (not rank): fixed cell budget that always fits the panel, auto-fit
// to the tested value range, and a 1-D coverage-track mode when only one axis is being swept.
function binHeat(){ const g=state.grid; if(!g||!g.xs.length) return null;
  const xs=g.xs, ys=g.ys;
  const xmin=xs[0], xmax=xs[xs.length-1], ymin=ys[0], ymax=ys[ys.length-1];
  const xDegen=(xs.length<=1)||(xmax===xmin), yDegen=(ys.length<=1)||(ymax===ymin);
  // --- 1-D track: exactly one axis varies -> put the VARYING axis on X and fill the full width.
  //     Extend to the FULL PLANNED extent (derived on the client) so it doubles as a progress bar. ---
  if(xDegen!==yDegen){
    const varyY=xDegen; let vmin=varyY?ymin:xmin; let vmax=varyY?ymax:xmax;
    const frontier=vmax;                      // latest tested value = the live sweep frontier
    let planned=false;
    const liveProg = state.psScope!=="all";      // the "all campaigns" union is coverage, not live progress
    const po = (varyY && liveProg) ? plannedOffset() : null;   // EXACT extent from param_spec (D)
    if(po && po.max>vmax){ vmin=Math.min(vmin,po.min); vmax=po.max; planned=true; }
    else if(liveProg && state.sweepTotal){ const step=minStep(varyY?ys:xs), reps=maxCellCount(g.cells), nOther=Math.max(1,(varyY?xs:ys).length);
      if(step&&reps){ const nPts=Math.round(state.sweepTotal/(reps*nOther));
        if(nPts>1){ const pmax=vmin+(nPts-1)*step; if(pmax>vmax+step*0.5){ vmax=pmax; planned=true; } } } }
    const nBins=Math.max(2,Math.min(120,Math.floor(PLOTW/8)));
    const bidx=v=>Math.min(nBins-1,Math.max(0,Math.floor((v-vmin)/((vmax-vmin)||1)*nBins)));
    const bins={};
    for(const key in g.cells){ const p=key.split(",").map(Number); const bi=bidx(varyY?p[1]:p[0]);
      bins[bi]=bins[bi]||{}; for(const oc in g.cells[key]) bins[bi][oc]=(bins[bi][oc]||0)+g.cells[key][oc]; }
    const binVal=[]; for(let i=0;i<nBins;i++) binVal.push(vmin+(i+0.5)/nBins*(vmax-vmin));
    return {mode:"1d", axis:(varyY?"ext-offset":"glitch width"), fixedName:(varyY?"width":"ext-offset"),
            fixedVal:(varyY?xs[0]:ys[0]), nBins, bins, binVal, vmin, vmax, frontier, planned};
  }
  // --- 2-D value-binned heatmap (both axes vary, or both single -> 1x1) ---
  const nCols=Math.max(1,Math.min(120,xs.length)), nRows=Math.max(1,Math.min(72,ys.length));
  const cIdx=v=>Math.min(nCols-1,Math.max(0,Math.floor((v-xmin)/((xmax-xmin)||1)*nCols)));
  const rIdx=v=>Math.min(nRows-1,Math.max(0,Math.floor((v-ymin)/((ymax-ymin)||1)*nRows)));
  const bins={};
  for(const key in g.cells){ const p=key.split(",").map(Number); const c=cIdx(p[0]),r=rIdx(p[1]);
    const bk=c+","+r; bins[bk]=bins[bk]||{}; for(const oc in g.cells[key]) bins[bk][oc]=(bins[bk][oc]||0)+g.cells[key][oc]; }
  const colVal=[],rowVal=[];
  for(let c=0;c<nCols;c++) colVal.push(xmin+(c+0.5)/nCols*((xmax-xmin)||0));
  for(let r=0;r<nRows;r++) rowVal.push(ymin+(r+0.5)/nRows*((ymax-ymin)||0));
  return {mode:"2d", nCols,nRows,bins,colVal,rowVal};
}
function cellColor(cell){ let tr=0,candidate=0,confirmed=0,best="no-effect",bn=-1;
  for(const raw in cell){ const oc=raw==="success"?"candidate":raw, n=cell[raw]; tr+=n;
    if(oc==="candidate")candidate+=n; if(oc==="confirmed")confirmed+=n; if(n>bn){bn=n;best=oc;} }
  const su=candidate+confirmed, rate=tr>0?su/tr:0; let col,op;
  if(confirmed){ col="var(--ok)"; op=Math.max(0.62,Math.min(1,0.42+confirmed/Math.max(1,tr))); }
  else if(candidate){ col="var(--caution)"; op=Math.max(0.55,Math.min(1,0.35+candidate/Math.max(1,tr))); }
  else { col=(OC[best]||OC["no-effect"]).c; op=best==="no-effect"?0.42+Math.min(0.22,tr/40):0.55+Math.min(0.35,tr/30); }
  return {tr,su,candidate,confirmed,best,rate,col,op}; }
const HM_DEFS='<defs><pattern id="gl-hatch" width="5" height="5" patternTransform="rotate(45)" patternUnits="userSpaceOnUse"><line x1="0" y1="0" x2="0" y2="5" stroke="var(--bg)" stroke-width="1.4" opacity=".5"/></pattern></defs>';
function drawHeatmap(){ const samples=state.grid&&state.grid.samples||[];
  if(state.grid&&state.grid.samples_are_complete&&samples.length){ drawIndividualAttempts(samples); return; }
  if(state.grid&&state.grid.xs.length===1&&state.grid.ys.length===1){ drawDenseFixedPoint(); return; }
  const b=binHeat(); if(!b){ drawEmptyHeat(); return; }
  return b.mode==="1d" ? drawHeat1D(b) : drawHeat2D(b); }
// Small runs are rendered as individual shots, not as a single aggregate cell.
// A repeated fixed point becomes a readable shot matrix; otherwise marks stay at
// their exact parameter coordinates.  Detail is native SVG title text, so it is
// present only on hover rather than as a permanent overlay.
function drawIndividualAttempts(samples){
  const pointKeys=new Set(samples.map(a=>a.x+","+a.y));
  if(pointKeys.size===1){ drawShotMatrix(samples); return; }
  const xs=samples.map(a=>+a.x),ys=samples.map(a=>+a.y),x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
  const xp=Math.max((x1-x0)*.08,.5),yp=Math.max((y1-y0)*.08,.5),left=x0-xp,right=x1+xp,bottom=y0-yp,top=y1+yp;
  const xOf=v=>PLOTX+(v-left)/(right-left)*PLOTW,yOf=v=>PLOTY+PLOTH-(v-bottom)/(top-bottom)*PLOTH,groups={};
  samples.forEach(a=>{const k=a.x+","+a.y;(groups[k]||(groups[k]=[])).push(a);});
  let s=`<rect x="${PLOTX}" y="${PLOTY}" width="${PLOTW}" height="${PLOTH}" fill="var(--panel)" stroke="var(--line)"/>`;
  Object.values(groups).forEach(group=>{const n=group.length,cols=Math.ceil(Math.sqrt(n)),rows=Math.ceil(n/cols),size=Math.max(8,Math.min(18,Math.floor(44/cols))),cx=xOf(+group[0].x),cy=yOf(+group[0].y);
    group.forEach((a,i)=>{const col=i%cols,row=Math.floor(i/cols),x=cx+(col-(cols-1)/2)*(size+2)-size/2,y=cy+(row-(rows-1)/2)*(size+2)-size/2,o=OC[a.c]||OC["no-effect"],lab=size>=15?(a.seq??i+1):"",click=a.id?` onclick="inspectEvidence(${Number(a.id)})"`:"";
      s+=`<g class="map-shot"${click}><title>attempt ${a.seq??a.id} · ${state.grid.x_name} ${fmt(a.x)} · ext-offset ${fmt(a.y)} · ${a.c}</title><rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${size}" height="${size}" rx="2" fill="${o.c}"/>${lab?`<text x="${(x+size/2).toFixed(1)}" y="${(y+size*.67).toFixed(1)}" text-anchor="middle" font-family="var(--mono)" font-size="${Math.max(7,size*.38)}" fill="#061016">${lab}</text>`:""}</g>`;});});
  s+=`<text x="${PLOTX+PLOTW/2}" y="${PLOTY+PLOTH+34}" text-anchor="middle" font-family="var(--sans)" font-size="10" font-weight="600" fill="var(--tx2)">${state.grid.x_name} (cyc)</text><text x="16" y="${PLOTY+PLOTH/2}" text-anchor="middle" font-family="var(--sans)" font-size="10" font-weight="600" fill="var(--tx2)" transform="rotate(-90 16 ${PLOTY+PLOTH/2})">ext-offset (cyc)</text>`;
  document.getElementById("heatmap").innerHTML=s; document.getElementById("hm-sub").textContent=`${samples.length} individual attempts · exact positions · ${mapScopeLabel()}`; document.getElementById("hm-bin").textContent=`shots: ${samples.length}`; renderLegend();
}
function drawShotMatrix(samples){
  const n=samples.length,cols=Math.min(16,Math.max(4,Math.ceil(Math.sqrt(n*1.8)))),rows=Math.ceil(n/cols),size=Math.max(10,Math.min(28,Math.floor(Math.min((PLOTW-64)/cols,(PLOTH-80)/rows)))),gw=cols*(size+3)-3,gh=rows*(size+3)-3,startX=PLOTX+(PLOTW-gw)/2,startY=PLOTY+(PLOTH-gh)/2+12;
  let s=`<rect x="${PLOTX}" y="${PLOTY}" width="${PLOTW}" height="${PLOTH}" fill="var(--panel)" stroke="var(--line)"/>`;
  samples.forEach((a,i)=>{const x=startX+(i%cols)*(size+3),y=startY+Math.floor(i/cols)*(size+3),o=OC[a.c]||OC["no-effect"],lab=size>=17?(a.seq??i+1):"",click=a.id?` onclick="inspectEvidence(${Number(a.id)})"` : "";
    s+=`<g class="map-shot"${click}><title>attempt ${a.seq??a.id} · ${state.grid.x_name} ${fmt(a.x)} · ext-offset ${fmt(a.y)} · ${a.c}</title><rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${size}" height="${size}" rx="2" fill="${o.c}"/>${lab?`<text x="${(x+size/2).toFixed(1)}" y="${(y+size*.67).toFixed(1)}" text-anchor="middle" font-family="var(--mono)" font-size="${Math.max(7,size*.38)}" fill="#061016">${lab}</text>`:""}</g>`;});
  s+=`<text x="${PLOTX+PLOTW/2}" y="${PLOTY+24}" text-anchor="middle" font-family="var(--mono)" font-size="11" font-weight="600" fill="var(--tx2)">${n} individual attempts · ${state.grid.x_name} ${fmt(samples[0].x)} cyc · ext-offset ${fmt(samples[0].y)} cyc</text>`;
  document.getElementById("heatmap").innerHTML=s; document.getElementById("hm-sub").textContent=`exact fixed-point history · ${mapScopeLabel()}`; document.getElementById("hm-bin").textContent=`shots: ${n}`; renderLegend();
}
function drawDenseFixedPoint(){
  const key=state.grid.xs[0]+","+state.grid.ys[0],cell=state.grid.cells[key]||{},cc=cellColor(cell),total=cc.tr,cols=20,rows=Math.max(2,Math.min(6,Math.ceil(Math.sqrt(total)/3))),marks=cols*rows,size=Math.min(Math.floor((PLOTW-72)/cols),Math.floor((PLOTH-76)/rows)),gw=cols*(size+2)-2,gh=rows*(size+2)-2,startX=PLOTX+(PLOTW-gw)/2,startY=PLOTY+(PLOTH-gh)/2+12,per=Math.ceil(total/marks);
  let s=`<rect x="${PLOTX}" y="${PLOTY}" width="${PLOTW}" height="${PLOTH}" fill="var(--panel)" stroke="var(--line)"/>`;
  for(let i=0;i<marks;i++){const x=startX+(i%cols)*(size+2),y=startY+Math.floor(i/cols)*(size+2);s+=`<rect x="${x}" y="${y}" width="${size}" height="${size}" rx="2" fill="${cc.col}" fill-opacity="${cc.op}"><title>about ${per} attempts in this density mark</title></rect>`;}
  s+=`<text x="${PLOTX+PLOTW/2}" y="${PLOTY+24}" text-anchor="middle" font-family="var(--mono)" font-size="11" font-weight="600" fill="var(--tx2)">${total.toLocaleString()} attempts · ${state.grid.x_name} ${fmt(state.grid.xs[0])} cyc · ext-offset ${fmt(state.grid.ys[0])} cyc</text>`;
  document.getElementById("heatmap").innerHTML=s; document.getElementById("hm-sub").textContent=`repeated fixed point · density marks summarize every attempt · ${mapScopeLabel()}`; document.getElementById("hm-bin").textContent=`${marks} density marks`; renderLegend();
}
function drawHeat2D(b){ const {nCols,nRows,bins,colVal,rowVal}=b;
  const cs=Math.max(3,Math.min(18,Math.floor(Math.min(PLOTW/nCols, PLOTH/nRows))));
  const gw=nCols*cs, gh=nRows*cs;
  let s=`<rect x="${PLOTX}" y="${PLOTY}" width="${PLOTW}" height="${PLOTH}" fill="var(--panel)" stroke="var(--line)"/>`;
  let sx=[], sy=[];
  for(let r=0;r<nRows;r++)for(let c=0;c<nCols;c++){ const cell=bins[c+","+r]; if(!cell) continue;
    const cc=cellColor(cell); const x=PLOTX+c*cs, y=PLOTY+(nRows-1-r)*cs;
    if(cc.su>0){ sx.push(c); sy.push(r); }
    s+=`<rect x="${x+0.5}" y="${y+0.5}" width="${cs-1}" height="${cs-1}" rx="1.5" fill="${cc.col}" fill-opacity="${cc.op.toFixed(2)}"><title>${state.grid.x_name} ${fmt(colVal[c])}; ext-offset ${fmt(rowVal[r])}; ${cc.tr} attempts</title></rect>`;
    if(cc.confirmed>0 && cs>=12) s+=`<text x="${x+cs/2}" y="${y+cs/2+3}" text-anchor="middle" font-size="${Math.min(9,cs-4)}" fill="#04121a">✓</text>`;
    else if(cc.candidate>0 && cs>=12) s+=`<text x="${x+cs/2}" y="${y+cs/2+3}" text-anchor="middle" font-size="${Math.min(9,cs-4)}" fill="#04121a">?</text>`;
    else if(cc.tr>=3 && cs>=11) s+=`<rect x="${x+0.5}" y="${y+0.5}" width="${cs-1}" height="${cs-1}" fill="url(#gl-hatch)"/>`;
  }
  if(false && sx.length){ const c0=Math.min(...sx),c1=Math.max(...sx),r0=Math.min(...sy),r1=Math.max(...sy);
    const bx=PLOTX+c0*cs-1.5, by=PLOTY+(nRows-1-r1)*cs-1.5, bw=(c1-c0+1)*cs+3, bh=(r1-r0+1)*cs+3;
    s+=`<rect x="${bx}" y="${by}" width="${bw}" height="${bh}" fill="none" stroke="var(--caution)" stroke-width="1.6" stroke-dasharray="5 3" rx="2"/>`;
    s+=`<rect x="${PLOTX+8}" y="${PLOTY+4}" width="218" height="30" rx="4" fill="var(--panel2)" stroke="var(--caution)"/>`;
    s+=`<text x="${PLOTX+15}" y="${PLOTY+16}" font-family="var(--mono)" font-size="9" font-weight="700" fill="var(--caution)">? SUCCESS-CLASS · ${state.mapTotals.success||0}</text>`;
    s+=`<text x="${PLOTX+15}" y="${PLOTY+28}" font-family="var(--mono)" font-size="8.5" fill="var(--tx2)">verify each attempt's full contract</text>`;
  }
  const xstep=Math.max(1,Math.ceil(nCols/8)), ystep=Math.max(1,Math.ceil(nRows/8));
  let ax='<g text-anchor="middle" font-family="var(--mono)" font-size="9" fill="var(--tx3)">';
  for(let c=0;c<nCols;c+=xstep) ax+=`<text x="${PLOTX+c*cs+cs/2}" y="${PLOTY+gh+16}">${fmt(colVal[c])}</text>`;
  ax+='</g><g text-anchor="end" font-family="var(--mono)" font-size="9" fill="var(--tx3)">';
  for(let r=0;r<nRows;r+=ystep) ax+=`<text x="${PLOTX-6}" y="${PLOTY+(nRows-1-r)*cs+cs/2+3}">${fmt(rowVal[r])}</text>`;
  ax+='</g>';
  ax+=`<text x="${PLOTX+gw/2}" y="${PLOTY+gh+34}" text-anchor="middle" font-family="var(--sans)" font-size="10" font-weight="600" fill="var(--tx2)">${state.grid.x_name} (cyc)</text>`;
  ax+=`<text x="16" y="${PLOTY+gh/2}" text-anchor="middle" font-family="var(--sans)" font-size="10" font-weight="600" fill="var(--tx2)" transform="rotate(-90 16 ${PLOTY+gh/2})">ext-offset (cyc)</text>`;
  document.getElementById("heatmap").innerHTML=HM_DEFS+s+ax;
  document.getElementById("hm-sub").textContent=`${state.grid.x_name} × ext-offset · ${nCols}×${nRows} bins · ${mapScopeLabel()}`;
  document.getElementById("hm-bin").textContent=`cells: ${Object.keys(bins).length}`;
  renderLegend();
}
// 1-D coverage track: the swept axis runs horizontally across the full panel; each column is one
// value-bin. Untested bins show as faint dashed gaps so you can see WHERE we haven't looked yet.
function drawHeat1D(b){ const {nBins,bins,binVal,axis,fixedName,fixedVal,vmin,vmax,frontier,planned}=b;
  const cw=PLOTW/nBins, bandH=Math.min(PLOTH-70,210), top=PLOTY+(PLOTH-bandH)/2, bot=top+bandH;
  const xOf=v=>PLOTX+((v-vmin)/((vmax-vmin)||1))*PLOTW, binHi=i=>vmin+(i+1)/nBins*(vmax-vmin);
  let s=`<rect x="${PLOTX}" y="${PLOTY}" width="${PLOTW}" height="${PLOTH}" fill="var(--panel)" stroke="var(--line)"/>`;
  let succ=[];
  for(let i=0;i<nBins;i++){ const x=PLOTX+i*cw, w=Math.max(1,cw-1.2), cell=bins[i], swept=binHi(i)<=frontier+1e-6;
    if(cell){ const cc=cellColor(cell); if(cc.su>0) succ.push(i);       // swept + has data: outcome colour
      s+=`<rect x="${x+0.6}" y="${top}" width="${w}" height="${bandH}" rx="1.5" fill="${cc.col}" fill-opacity="${cc.op.toFixed(2)}"><title>${axis} ${fmt(binVal[i])}; ${cc.tr} attempts</title></rect>`;
      if(cc.confirmed>0 && cw>=10) s+=`<text x="${x+cw/2}" y="${top+bandH/2+4}" text-anchor="middle" font-size="10" fill="#04121a">✓</text>`;
      else if(cc.candidate>0 && cw>=10) s+=`<text x="${x+cw/2}" y="${top+bandH/2+4}" text-anchor="middle" font-size="10" fill="#04121a">?</text>`;
    } else if(swept){                                                    // swept, thin no-data gap: dim fill
      s+=`<rect x="${x+0.6}" y="${top}" width="${w}" height="${bandH}" rx="1.5" fill="var(--noeff)" fill-opacity="0.16"/>`;
    } else {                                                            // NOT yet swept: hollow = remaining
      s+=`<rect x="${x+0.6}" y="${top}" width="${w}" height="${bandH}" fill="none" stroke="var(--line)" stroke-opacity="0.45" stroke-dasharray="2 3"/>`;
    }
  }
  // live progress frontier (boundary between swept and remaining) — the visual "you are here"
  if(planned && frontier>vmin && frontier<vmax-1e-6){ const fx=xOf(frontier);
    s+=`<line x1="${fx}" y1="${top-7}" x2="${fx}" y2="${bot+7}" stroke="var(--acc)" stroke-width="1.5"/>`;
    s+=`<path d="M${fx-4.5} ${top-7} L${fx+4.5} ${top-7} L${fx} ${top-1.5} Z" fill="var(--acc)"/>`;
  }
  if(false && succ.length){ const i0=Math.min(...succ),i1=Math.max(...succ);
    s+=`<rect x="${PLOTX+i0*cw-1.5}" y="${top-3}" width="${(i1-i0+1)*cw+3}" height="${bandH+6}" fill="none" stroke="var(--caution)" stroke-width="1.6" stroke-dasharray="5 3" rx="2"/>`;
    s+=`<rect x="${PLOTX+8}" y="${PLOTY+4}" width="250" height="30" rx="4" fill="var(--panel2)" stroke="var(--caution)"/>`;
    s+=`<text x="${PLOTX+15}" y="${PLOTY+16}" font-family="var(--mono)" font-size="9" font-weight="700" fill="var(--caution)">? SUCCESS-CLASS · ${state.mapTotals.success||0} @ off ${fmt(binVal[i0])}</text>`;
    s+=`<text x="${PLOTX+15}" y="${PLOTY+28}" font-family="var(--mono)" font-size="8.5" fill="var(--tx2)">width=${fmt(fixedVal)} fixed · confirm separately</text>`;
  }
  const xstep=Math.max(1,Math.ceil(nBins/10));
  let ax='<g text-anchor="middle" font-family="var(--mono)" font-size="9" fill="var(--tx3)">';
  for(let i=0;i<nBins;i+=xstep) ax+=`<text x="${PLOTX+i*cw+cw/2}" y="${bot+16}">${fmt(binVal[i])}</text>`;
  ax+='</g>';
  ax+=`<text x="${PLOTX+PLOTW/2}" y="${bot+34}" text-anchor="middle" font-family="var(--sans)" font-size="10" font-weight="600" fill="var(--tx2)">${axis} (cyc) · ${fixedName}=${fmt(fixedVal)} fixed</text>`;
  document.getElementById("heatmap").innerHTML=HM_DEFS+s+ax;
  document.getElementById("hm-sub").textContent=`${axis} coverage · ${fixedName}=${fmt(fixedVal)} · ${mapScopeLabel()}`;
  document.getElementById("hm-bin").textContent=`${nBins} bins`;
  renderLegend();
}
function drawEmptyHeat(){ document.getElementById("heatmap").innerHTML=
  `<rect x="${PLOTX}" y="${PLOTY}" width="${PLOTW}" height="${PLOTH}" fill="var(--panel)" stroke="var(--line)"/>`+
  `<text x="${PLOTX+PLOTW/2}" y="${PLOTY+PLOTH/2}" text-anchor="middle" fill="var(--tx3)" font-size="12" font-family="var(--sans)">no sweep data yet — Start Sweep</text>`;
  renderLegend(); }
function renderLegend(){ const items=[["no-data","dash"],["no-effect",""],["reset",""],["exception",""],["false-positive",""],["candidate",""],["confirmed",""]];
  document.getElementById("legendrow").innerHTML='<span class="lb">OUTCOME</span>'+items.map(([k,cls])=>{
    const o=OC[k]; return `<span class="legchip ${cls}"><span class="sw" style="background:${k==="no-data"?"transparent":o.c};${k==="no-data"?"border:1px dashed var(--noeff)":""}"></span>${o.g!==" "?o.g+" ":""}${o.lbl}</span>`;
  }).join("")+'<div class="spring"></div><span class="sub">faint dashed = not yet swept</span>'; }

/* ---------------- stat tiles ---------------- */
function renderTiles(){ const T=state.totals, tot=state.sumTotal||1;
  const defs=[["candidate","Candidate","b-caution","M12 6v7M12 17.5v.1","§Candidate§The connector observed a positive signal, but its full confirmation contract is not complete."],
    ["confirmed","Confirmed","b-ok","M5 12l4 4L19 6","§Confirmed§The persisted project connector and runtime evidence contract is complete."],
    ["no-effect","No effect","b-noeffect","M5 12h14","§No effect§The connector observed no target response or change. This is a measured negative result, distinct from an unswept cell and from a partial connection."],
    ["reset","Reset","b-reset","M20 5.5v5h-5 M19.6 10.4A8 8 0 1 0 12 20","§Reset§The glitch crashed or rebooted the target — too strong, no useful fault. The engine re-syncs and continues."],
    ["exception","Infrastructure","b-danger","M6 6l12 12M18 6 6 18","§Infrastructure§A glitcher, connector, scope, worker, or storage stage failed. Inspect the persisted error capture; never count it as a target fault."],
    ["false-positive","False-pos","b-fp","M8.6 8.6a3.4 3.4 0 1 1 4.8 3.1c-.9.5-1.4 1.2-1.4 2.3 M12 18.3v.1","§False-positive§The active project connector's complete confirmation contract failed."],
    ["no-data","No-data","b-nodata","M4 4h16v16H4z","§No-data§Parameter cells not yet swept. Tracked as a DISTINCT state — 'not measured' is never conflated with '0% success'."]];
  let ndFrac=0; if(state.grid&&state.grid.xs.length){ const cellCount=state.grid.xs.length*state.grid.ys.length; ndFrac=cellCount?1-Object.keys(state.grid.cells).length/cellCount:0; }
  const html=defs.map(([k,lbl,bcls,path,tip])=>{
    const o=OC[k]||OC["no-effect"]; const val=k==="no-data"?(ndFrac*100).toFixed(0)+"%":(T[k]||0).toLocaleString();
    const sub=k==="no-data"?"cells unswept":(((T[k]||0)/tot*100).toFixed(2)+"% of Σ");
    return `<div class="tile ${bcls}" data-tip="${esc(tip)}"><div class="k"><svg viewBox="0 0 24 24" fill="none" stroke="${o.c}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="${path}"/></svg>${lbl}</div>
      <div class="v">${val}</div><div class="s">${sub}</div></div>`;}).join("");
  document.getElementById("tiles").innerHTML=html;
}

/* ---------------- attempts table ---------------- */
/* Per-attempt stored waveform samples as an inline SVG sparkline.
   The private connector defines the meaning of the primary and observed traces. */
function waveSVG(ch1, ch3, w=104, h=22){
  const series=[ch1,ch3].filter(a=>Array.isArray(a)&&a.length>1);
  if(!series.length) return "";
  let lo=Infinity, hi=-Infinity;
  for(const s of series) for(const y of s){ if(y<lo)lo=y; if(y>hi)hi=y; }
  if(!isFinite(lo)||!isFinite(hi)) return "";
  if(hi-lo<0.05){ const m=(hi+lo)/2; lo=m-0.025; hi=m+0.025; }   // avoid a flat line filling the box
  const pad=(hi-lo)*0.08; lo-=pad; hi+=pad;
  const path=s=>s.map((y,i)=>{
    const x=(i/(s.length-1))*(w-2)+1;
    const py=h-1-((y-lo)/(hi-lo))*(h-2);
    return (i?"L":"M")+x.toFixed(1)+" "+py.toFixed(1);
  }).join("");
  const zeroY = (lo<=0&&hi>=0) ? (h-1-((0-lo)/(hi-lo))*(h-2)) : null;
  return `<svg class="wv" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">`
    + (zeroY!=null?`<line x1="0" y1="${zeroY.toFixed(1)}" x2="${w}" y2="${zeroY.toFixed(1)}" stroke="var(--tx3)" stroke-width=".5" stroke-dasharray="2 2" opacity=".55"/>`:"")
    + (ch3&&ch3.length>1?`<path d="${path(ch3)}" fill="none" stroke="#e879f9" stroke-width="1.1" opacity=".95"/>`:"")
    + (ch1&&ch1.length>1?`<path d="${path(ch1)}" fill="none" stroke="var(--acc)" stroke-width="1.1"/>`:"")
    + `</svg>`;
}

/* ---------- Waveform inspector (click a sparkline to open, zoom + pan) ---------- */
state.wz = {k:1, x:0, drag:null};

async function loadWave(id,openAfter){
  const a=(state.attempts||[]).find(r=>String(r.id)===String(id) || String(r.seq)===String(id));
  if(!a)return;
  if(a.wave||a.pinWave){ if(openAfter)openWave(id); return; }
  // Table refreshes replace every row.  Keep the load itself by attempt ID,
  // but always resolve against the *current* row instead of the detached DOM
  // node that happened to start the request.
  if(state.waveformLoading[id]){
    if(openAfter) state.waveformLoading[id].openAfter=true;
    return state.waveformLoading[id].promise;
  }
  const job={openAfter:!!openAfter,promise:null};
  state.waveformLoading[id]=job;
  const host=document.getElementById(`wave-thumb-${id}`);
  if(host)host.textContent="loading waveform…";
  job.promise=(async()=>{ try{
    // A 320-point trace is ample for a 104px sparkline and avoids parsing full
    // scope captures once per table row. The persisted artifact remains the
    // source of truth for the full inspector/export path.
    const controller=new AbortController();
    const deadline=setTimeout(()=>controller.abort(),8000);
    let response;
    try{
      response=await fetch(`/api/attempt/${encodeURIComponent(a.id)}/waveform?max_points=320`,{signal:controller.signal});
    }finally{ clearTimeout(deadline); }
    const data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||"waveform unavailable");
    const current=(state.attempts||[]).find(r=>String(r.id)===String(id) || String(r.seq)===String(id));
    if(current){
      current.wave=data.primary||null; current.pinWave=data.observed||null;
      current.waveLabels={primary:data.primary_label||"injection node",observed:data.observed_label||"observed target rail"};
      current.waveSampleCount=data.sample_count||null; current.waveDt=data.sample_interval_s||null;
      const currentHost=document.getElementById(`wave-thumb-${current.id}`);
      const svg=waveSVG(current.wave,current.pinWave);
      if(currentHost){
        currentHost.className="wvclick wave-cell";
        currentHost.title="click to inspect / zoom";
        currentHost.onclick=event=>{ event.stopPropagation(); openWave(current.id); };
        currentHost.innerHTML=svg||"waveform unavailable";
      }
    }
    if(job.openAfter)openWave(id);
  }catch(error){
    const currentHost=document.getElementById(`wave-thumb-${id}`);
    if(currentHost){
      currentHost.className="wave-load wave-cell wave-retry";
      currentHost.title="Waveform request timed out or failed. Click to retry.";
      currentHost.textContent="waveform unavailable · retry";
      currentHost.onclick=event=>{ event.stopPropagation(); loadWave(id,false); };
    }
  }finally{
    if(state.waveformLoading[id]===job)delete state.waveformLoading[id];
  }} )();
  return job.promise;
}
async function hydrateWaveforms(rows){
  // Waveforms are evidence, not an optional detail.  Populate every returned
  // attempt automatically, while keeping a small worker pool so a large
  // campaign never floods the artifact store or stalls the rest of the UI.
  const pending=(rows||[]).filter(a=>a.waveformAvailable&&!a.wave&&!a.pinWave&&!state.waveformLoading[a.id]);
  let cursor=0;
  const worker=async()=>{ while(cursor<pending.length){
    const attempt=pending[cursor++];
    await loadWave(attempt.id,false);
  }};
  // Keep artifact reads responsive on slower evidence volumes.  More parallel
  // reads make the individual first-loads less reliable, not faster.
  await Promise.all(Array.from({length:Math.min(4,pending.length)},worker));
}
function openWave(id){
  const a=(state.attempts||[]).find(r=>String(r.id)===String(id) || String(r.seq)===String(id));
  if(!a)return;
  if(!a.wave&&!a.pinWave){ if(a.waveformAvailable)loadWave(id,true); return; }
  state.wz={k:1,x:0,drag:null}; state.wzAttempt=a;
  let ov=document.getElementById("wave-ov");
  if(!ov){ ov=document.createElement("div"); ov.id="wave-ov"; ov.className="wave-ov";
    ov.addEventListener("mousedown",e=>{ if(e.target===ov) closeWave(); });
    document.body.appendChild(ov); }
  ov.innerHTML=`<div class="wave-box" role="dialog" aria-label="Glitch waveform">
      <div class="wave-hd">
        <div><b>Attempt ${a.seq??a.id}</b> <span class="sub">· width ${a.width??"?"} · offset ${fmt(a.offset??0)} · ${a.outcome??""}</span></div>
        <div class="wave-btns">
          <button title="Zoom out" onclick="wzZoom(1/1.6)">−</button><button title="Zoom in" onclick="wzZoom(1.6)">+</button>
          <button title="Show the whole trace" onclick="wzReset()">reset view</button><button title="Close" onclick="closeWave()">✕</button>
        </div>
      </div>
      <div class="wave-lg">
        <span><i style="background:var(--acc)"></i>${esc((a.waveLabels||{}).primary||"injection node")}</span>
        ${a.pinWave?`<span><i style="background:#e879f9"></i>${esc((a.waveLabels||{}).observed||"observed target rail")}${a.pinDip!=null?` (min ${(+a.pinDip).toFixed(3)} V)`:""}</span>`:`<span class="sub">secondary target-rail trace not captured for this attempt</span>`}
      </div>
      <div id="wave-plot" class="wave-plot"></div>
      <div class="wave-nav"><span class="sub">trace position</span><input id="wave-position" type="range" min="0" max="100" value="0" step="0.1" aria-label="Waveform trace position" oninput="wzSetPosition(this.value)"><span class="sub" id="wave-position-label">full trace</span></div>
      <div class="sub wave-ft">use + / − or Ctrl/⌘ + scroll to zoom · drag to pan · ${a.waveSampleCount?`${a.waveSampleCount.toLocaleString()} stored samples`:"normalized sample position"}${a.waveDt?` · ${(a.waveDt*1e9).toFixed(2)} ns/sample`:""}</div>
    </div>`;
  ov.classList.add("show");
  drawWaveBig();
  const plot=document.getElementById("wave-plot");
  plot.addEventListener("wheel",e=>{
    // Let a normal wheel/trackpad gesture continue to scroll the dialog. This
    // avoids trapping the operator in a zoomed trace; Ctrl/⌘ makes zooming
    // intentional and keeps the browser's normal scroll semantics intact.
    if(!e.ctrlKey&&!e.metaKey)return;
    e.preventDefault();
    const r=plot.getBoundingClientRect(), fx=(e.clientX-r.left)/r.width;
    wzZoom(e.deltaY<0?1.22:1/1.22, fx); },{passive:false});
  plot.addEventListener("pointerdown",e=>{ e.preventDefault(); plot.setPointerCapture(e.pointerId); state.wz.drag={pointerId:e.pointerId,sx:e.clientX,x0:state.wz.x}; });
  plot.addEventListener("pointermove",wzMove); plot.addEventListener("pointerup",wzUp); plot.addEventListener("pointercancel",wzUp);
  document.addEventListener("keydown",wzKey);
}
function wzMove(e){ const d=state.wz.drag; if(!d) return;
  const w=(document.getElementById("wave-plot")||{}).clientWidth||900;
  state.wz.x=d.x0+(e.clientX-d.sx)/w/state.wz.k; wzClamp(); drawWaveBig(); }
function wzUp(){ state.wz.drag=null; }
function wzKey(e){
  if(e.key==="Escape")closeWave();
  else if(e.key==="ArrowLeft"||e.key==="ArrowRight"){
    if(e.target&&e.target.tagName==="INPUT")return;
    const z=state.wz, step=Math.max(.02,(1-1/z.k)*.12);
    z.x+=(e.key==="ArrowLeft"?step:-step); wzClamp(); drawWaveBig(); e.preventDefault();
  }
}
function wzZoom(f,focus){ const z=state.wz, k0=z.k; z.k=Math.min(60,Math.max(1,z.k*f));
  const fx=(focus==null?0.5:focus);
  // keep the point under the cursor stationary while zooming
  z.x = z.x + (fx/z.k - fx/k0);
  wzClamp(); drawWaveBig(); }
function wzReset(){ state.wz.k=1; state.wz.x=0; drawWaveBig(); }
function wzClamp(){ const z=state.wz; const span=1/z.k; z.x=Math.min(0,Math.max(-(1-span),z.x)); }
function wzSetPosition(value){ const z=state.wz, max=1-1/z.k; z.x=-max*(+value/100); wzClamp(); drawWaveBig(); }
function updateWaveNavigation(){
  const z=state.wz, max=1-1/z.k, nav=document.getElementById("wave-position"), label=document.getElementById("wave-position-label");
  if(!nav||!label)return;
  const percent=max?Math.max(0,Math.min(100,(-z.x/max)*100)):0;
  nav.disabled=max<=.0001; nav.value=percent;
  label.textContent=max?`${percent.toFixed(0)}% across trace`:"full trace";
}
function closeWave(){ const ov=document.getElementById("wave-ov"); if(ov) ov.classList.remove("show"); document.removeEventListener("keydown",wzKey); }

function drawWaveBig(){
  const host=document.getElementById("wave-plot"); const a=state.wzAttempt; if(!host||!a) return;
  const W=900,H=340,L=52,R=14,T=14,B=30, pw=W-L-R, ph=H-T-B;
  const series=[a.wave,a.pinWave].filter(s=>Array.isArray(s)&&s.length>1);
  if(!series.length){ host.innerHTML='<div class="sub" style="padding:20px">no samples stored</div>'; return; }
  let lo=Infinity,hi=-Infinity; for(const s of series) for(const y of s){ if(y<lo)lo=y; if(y>hi)hi=y; }
  const pad=Math.max(0.02,(hi-lo)*0.08); lo-=pad; hi+=pad;
  const z=state.wz, vx=-z.x*z.k;                       // visible fraction -> transform
  const yOf=v=>T+ph-((v-lo)/(hi-lo))*ph;
  const path=s=>s.map((y,i)=>{ const x=L+(i/(s.length-1))*pw; return (i?"L":"M")+x.toFixed(2)+" "+yOf(y).toFixed(2); }).join("");
  // y grid + labels
  let grid="",n=5;
  for(let i=0;i<=n;i++){ const v=lo+(hi-lo)*i/n, y=yOf(v);
    grid+=`<line x1="${L}" y1="${y.toFixed(1)}" x2="${W-R}" y2="${y.toFixed(1)}" stroke="var(--line)" stroke-width=".6"/>`
        + `<text x="${L-8}" y="${(y+3.5).toFixed(1)}" text-anchor="end" class="wa">${v.toFixed(2)}V</text>`; }
  if(lo<=0&&hi>=0){ const y0=yOf(0); grid+=`<line x1="${L}" y1="${y0.toFixed(1)}" x2="${W-R}" y2="${y0.toFixed(1)}" stroke="var(--tx3)" stroke-width="1" stroke-dasharray="4 3"/>`; }
  // Legacy waveform arrays do not carry a sample interval; label normalized sample position.
  let xl="";
  for(let i=0;i<=6;i++){ const f=i/6, gf=(-z.x)+f/z.k, pct=gf*100, x=L+f*pw;
    xl+=`<line x1="${x.toFixed(1)}" y1="${T}" x2="${x.toFixed(1)}" y2="${T+ph}" stroke="var(--line)" stroke-width=".4"/>`
      + `<text x="${x.toFixed(1)}" y="${H-10}" text-anchor="middle" class="wa">${pct.toFixed(1)}%</text>`; }
  host.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet">
    <clipPath id="wclip"><rect x="${L}" y="${T}" width="${pw}" height="${ph}"/></clipPath>
    ${grid}${xl}
    <g clip-path="url(#wclip)"><g transform="translate(${(L+vx*pw-L*z.k).toFixed(2)},0) scale(${z.k},1)">
      ${a.pinWave&&a.pinWave.length>1?`<path d="${path(a.pinWave)}" fill="none" stroke="#e879f9" stroke-width="${(1.6/z.k).toFixed(3)}" vector-effect="non-scaling-stroke"/>`:""}
      ${a.wave&&a.wave.length>1?`<path d="${path(a.wave)}" fill="none" stroke="var(--acc)" stroke-width="${(1.6/z.k).toFixed(3)}" vector-effect="non-scaling-stroke"/>`:""}
    </g></g>
    <rect x="${L}" y="${T}" width="${pw}" height="${ph}" fill="none" stroke="var(--line)"/>
  </svg>`;
  updateWaveNavigation();
}

function renderTable(){ let rows=state.attempts;  // server already filtered by outcome
  const cols="56px 72px 84px 52px 48px 118px 126px 118px minmax(240px,1fr)";
  const cnt=document.getElementById("att-count");
  const filterName=state.confirmationFilter||state.filter;
  if(cnt) cnt.textContent=(filterName?(rows.length+" "+filterName):`${(state.attemptsTotal||rows.length).toLocaleString()} records · ${(state.coverageValid??"?")} valid coverage · ${state.invalidAttempts||0} invalid`)
    +(rows.length<(state.attemptsTotal||0)&&!state.filter?" · latest "+rows.length:"");
  document.getElementById("atbody").innerHTML=rows.slice(0,500).map(a=>{ const invalid=a.attemptValid===false;
    const tier=invalid?"invalid_infrastructure":a.outcome==="success"?(a.verified?"confirmed":"candidate"):a.outcome;
    const o=OC[tier]||OC["no-effect"];
    const dipTxt = (a.dip!=null && a.dip!=="") ? `dip ${(+a.dip).toFixed(2)}V${a.depth!=null?` (Δ${(+a.depth).toFixed(2)})`:""}` : "";
    const timing=[a.pulseWidthNs!=null?`pulse ${a.pulseWidthNs}ns`:"",a.injectionDelayUs!=null?`trigger→injection ${a.injectionDelayUs}µs`:"",a.signalMin!=null?`signal min ${a.signalMin}V`:""].filter(Boolean).join(" · ");
    const outcomeDetail={partial_connection:"partial target connection; not a clean no-effect",target_response_failed_runtime:"target response failed runtime verification",confirmation_evidence_failed:"target response passed; required confirmation evidence failed",unconfirmed_response:"unconfirmed target response",no_target_response:"no target response observed"}[a.outcomeDetail];
    let note = invalid?`INVALID INFRASTRUCTURE ROW · excluded from valid-shot coverage`:tier==="confirmed"?`complete connector + runtime contract`:tier==="candidate"?`connector candidate; inspect missing gates`:a.outcome==="reset"?`reset/target-state failure`:a.outcome==="exception"?`infrastructure result`:(outcomeDetail||a.oracle||a.notes||`negative connector baseline`);
    if(a.validity==="refresh_pending") note=`VALIDITY PENDING PERSISTED AUX REFRESH · ${note}`;
    else if(a.validity==="legacy_validity_unknown") note=`LEGACY VALIDITY UNKNOWN · ${note}`;
    // Show the connector-selected observation trace next to the primary trace when available.
    const pinTxt = (a.pinDip!=null && a.pinDip!=="") ? ` · <b style="color:#e879f9">pin ${(+a.pinDip).toFixed(2)}V</b>` : "";
    // clickable: opens the zoomable inspector for this attempt's real captured samples
    const rawSvg = waveSVG(a.wave, a.pinWave);
    const svg = rawSvg ? `<span class="wvclick wave-cell" id="wave-thumb-${a.id}" title="click to inspect / zoom" onclick="event.stopPropagation();openWave('${a.id??a.seq}')">${rawSvg}</span>` :
      (a.waveformAvailable?`<span class="wave-load wave-cell" id="wave-thumb-${a.id}" title="loading persisted waveform">waveform loading…</span>`:"");
    const dipCell=(a.dip!=null && a.dip!=="")
      ? `${(+a.dip).toFixed(2)} V${a.depth!=null?` · Δ${(+a.depth).toFixed(2)} V`:""}` : "—";
    const pinCell=(a.pinDip!=null && a.pinDip!=="")
      ? `<br><span style="color:#e879f9">pin ${(+a.pinDip).toFixed(2)} V</span>` : "";
    const verification=invalid?"shot delivery invalid":tier==="confirmed"?"connector verified":tier==="candidate"?"connector candidate · verification incomplete":note;
    const verificationGates=a.evidenceTotal
      ? `${a.evidencePassed}/${a.evidenceTotal} required checks${a.evidenceFailed.length?` · failed: ${a.evidenceFailed.join(", ")}`:""}` : "";
    const verificationCell=`<span class="attempt-primary">${esc(verification)}</span>${verificationGates?`<span class="attempt-meta">${esc(verificationGates)}</span>`:""}`;
    const ph=a.phase||{}, tr=a.trigger||{}, os=a.oracleState||{};
    const delivery=[ph.width_steps!=null?`phase ${ph.width_steps}/${ph.offset_steps} of ${ph.phase_shift_steps}`:"",
      tr.module?`trig ${tr.module}/${tr.edge}@${tr.level_v}V`:"",os.failure_stage?`connector failed:${os.failure_stage}`:os.highest_passed_stage?`connector through:${os.highest_passed_stage}`:"",
      os.runtime_confirmed!=null?`runtime:${os.runtime_confirmed}`:"",a.evidenceTotal?`evidence ${a.evidencePassed}/${a.evidenceTotal}${a.evidenceFailed.length?` failed:${a.evidenceFailed.join(",")}`:""}`:""].filter(Boolean).join(" · ");
    const ev = `<span class="attempt-primary">${esc(note)}</span>`+
      (delivery?`<span class="attempt-meta">${esc(delivery)}</span>`:"")+
      ((timing||dipTxt||svg)?`<span class="attempt-physical">${svg?svg+" ":""}${dipTxt?`<b style="color:var(--acc)">${dipTxt}</b>${pinTxt}`:""}${timing?`${dipTxt?" · ":""}${timing}`:""}</span>`:"");
    const evidenceClick=a.id?` onclick="inspectEvidence(${Number(a.id)})" title="inspect persisted connector, delivery, and required-evidence gates" style="cursor:pointer;color:var(--tx3);overflow:hidden"`:
      ` style="color:var(--tx3);overflow:hidden"`;
    const pulseDisplay=a.width??"";
    const offsetDisplay=fmt(a.offset??0);
    return `<div class="tbl-r ${invalid?'invalid-attempt':''}" style="grid-template-columns:${cols}">
      <span style="color:var(--tx3)">${a.seq??a.id}</span><span style="color:var(--tx)">${pulseDisplay}</span>
      <span style="color:var(--tx)">${offsetDisplay}</span><span>${(a.voltage??"").toString().slice(0,4)}</span>
      <span>${a.repeat??1}</span>
      <span class="pill" style="color:${o.c}"><span class="sw" style="background:${o.c}"></span>${o.g} ${tier}</span>
      <span class="attempt-physical"><b style="color:var(--acc)">${dipCell}</b>${pinCell}</span>
      <span>${svg||'<span class="sub">—</span>'}</span>
      <span${evidenceClick}>${verificationCell}</span></div>`;}).join("");
  renderFilters();
}
function renderFilters(){ const cls=["candidate","confirmed","false-positive","reset","exception","no-effect"];
  document.getElementById("filters").innerHTML=`<span class="filterchip ${state.filter===null?'on':''}" onclick="setFilter(null)">all</span>`+
    cls.map(c=>`<span class="filterchip ${(state.confirmationFilter===c||(state.filter===c&&!state.confirmationFilter))?'on':''}" onclick="setFilter('${c}')">${OC[c].g} ${c} ${state.totals[c]||0}</span>`).join("")+
    `<span class="filterchip dash" title="Explicit attempt_valid=false rows remain in history but never count as valid-shot coverage">! invalid excluded ${state.invalidAttempts||0}</span>`; }
function setFilter(f){ state.confirmationFilter=(f==="candidate"||f==="confirmed")?f:null; state.filter=state.confirmationFilter?"success":f; renderFilters(); loadAttempts(); }

/* ---------------- rolling hit-rate sparkline ---------------- */
function drawSpark(rr){ const svg=document.getElementById("spark");
  const data=(rr&&rr.cumulative_series&&rr.cumulative_series.length)?rr.cumulative_series:cumRate();
  if(!data.length){ svg.innerHTML=""; return; } const W=368,H=58, max=Math.max(...data,0.001);
  const pts=data.map((v,i)=>`${(i/(data.length-1||1)*W).toFixed(1)},${(H-(v/max)*(H-8)-4).toFixed(1)}`).join(" ");
  svg.innerHTML=`<line x1="0" y1="30" x2="${W}" y2="30" stroke="var(--line2)" stroke-dasharray="3 3"/>
    <polyline points="${pts}" fill="none" stroke="var(--ok)" stroke-width="1.7"/>
    <circle cx="${W}" cy="${(H-(data[data.length-1]/max)*(H-8)-4).toFixed(1)}" r="2.6" fill="var(--ok)"/>`;
  document.getElementById("rr-arrow").textContent=(rr&&rr.trend_slope>=0)?"▲":"▼"; }
function cumRate(){ let s=0,out=[]; state.ratehist.forEach((v,i)=>{s+=v;out.push(s/(i+1)*100);}); return out; }

/* ---------------- agent activity log ---------------- */
function logAgent(tool,detail){ const el=document.getElementById("agentlog"); if(!el)return;
  const t=new Date().toLocaleTimeString();
  el.innerHTML=`<div><span class="t">${t}</span> <span class="ok">→</span> ${tool} <span class="t">${detail?("· "+JSON.stringify(detail).slice(0,42)):""}</span></div>`+el.innerHTML;
  if(el.children.length>40) el.removeChild(el.lastChild); }

/* ---------------- instruments ---------------- */
function applyScopePolicyControls(policy){ policy=policy||{}; const access=policy.companion_access_allowed!==false;
  const targetBlocked=!!(policy.target_state&&policy.target_state.blocking);
  setUiDisabled(document.getElementById("scope-bind-btn"),policy.bind_allowed===false);
  setUiDisabled(document.getElementById("scope-unbind-btn"),targetBlocked);
  ["mode-embed","mode-mirror","scope-open-btn"].forEach(id=>setUiDisabled(document.getElementById(id),!access));
  if(!access){ stopMirror(); const fr=document.getElementById("scope-iframe"), img=document.getElementById("scope-mirror"), ov=document.getElementById("scope-overlay"), txt=document.getElementById("scope-overlay-txt");
    if(fr){ fr.setAttribute("src","about:blank"); fr.style.display="none"; } if(img)img.style.display="none";
    if(ov){ ov.style.display="flex"; ov.style.pointerEvents="none"; }
    if(txt) txt.textContent="Companion scope unavailable: "+(policy.reason||"project-managed evidence is using the scope");
  }
}
function updateTrace(){ const c=state.lastCap||{}, m=state.lastMeas||{};
  const rows=[["samples",c.samples!=null?c.samples.toLocaleString():"—"],
    ["dt (sample interval)",c.dt_s!=null?(c.dt_s*1e9).toFixed(1)+" ns":"—"],
    ["window",c.dt_s!=null&&c.samples!=null?(c.dt_s*c.samples*1e6).toFixed(1)+" µs":"—"],
    ["Vpp CH1",m.vpp!=null?m.vpp+" V":(c.vpp!=null?c.vpp+" V":"—")],
    ["Vmax / Vmin",m.vmax!=null?(m.vmax+" / "+m.vmin+" V"):"—"],
    ["trigger offset",c.t0_s!=null?(c.t0_s*1e6).toFixed(1)+" µs":"—"],
    ["clipped",c.clipped!=null?String(c.clipped):"—"],
    ["waveform",c.waveform_uri?"resource ✓":"—"]];
  const el=document.getElementById("trace-rows"); if(el) el.innerHTML=rows.map(([k,v])=>
    `<div style="display:flex;justify-content:space-between;gap:10px;padding:7px 12px;border-bottom:1px solid var(--line)"><span class="sub">${k}</span><span style="font:600 12px/1.3 var(--mono);color:var(--tx)">${v}</span></div>`).join(""); }
async function loadInstruments(){ const b=await (await fetch("/api/bootstrap")).json(); const sc=b.scope||{};
  const policy=b.scope_policy||{}; state.scopePolicy=policy;
  applyTargetState(b.target_state||policy.target_state||{});
  document.getElementById("scope-bind").textContent=sc.bound?"BOUND":"UNBOUND";
  document.getElementById("scope-bind").className="chip "+(sc.bound?"ok":"");
  document.getElementById("scope-dot").style.background=sc.bound?"var(--ok)":"var(--noeff)";
  document.getElementById("scope-driver").textContent=sc.driven_by_mcp?"MCP":(sc.bound?"idle":"—");
  document.getElementById("tr-idn").textContent=sc.idn||"—";
  document.getElementById("tr-res").textContent=sc.resource||"—";
  document.getElementById("scope-lock").textContent=policy.project_evidence_owned?"project evidence owns the scope":(sc.bound?(sc.driven_by_mcp?"held by GlitchLab":"bound · idle"):"not bound");
  document.getElementById("scope-policy").textContent=policy.reason||"companion session available";
  const bindBtn=document.getElementById("scope-bind-btn"), unbindBtn=document.getElementById("scope-unbind-btn"), mirrorBtn=document.getElementById("mode-mirror");
  if(bindBtn) bindBtn.style.display=(!sc.bound&&policy.bind_allowed!==false)?"":"none";
  if(unbindBtn) unbindBtn.style.display=sc.bound?"":"none";
  if(mirrorBtn) mirrorBtn.style.display=policy.companion_capture_allowed===false?"none":"";
  applyScopePolicyControls(policy);
  document.getElementById("trace-att").textContent=sc.bound?"live":"";
  state.scopeUrl=b.scope_webcontrol||"";
  document.getElementById("scope-url").textContent=state.scopeUrl+" — live scope screen";
  state.scopeBound=!!sc.bound;
  if(state.scopeMode==="mirror"&&policy.companion_capture_allowed===false) state.scopeMode="embed";
  if(policy.companion_access_allowed===false) applyScopePolicyControls(policy);
  else if(state.scopeUrl) scopeMode(state.scopeMode||"embed");
  else { const ov=document.getElementById("scope-overlay"), txt=document.getElementById("scope-overlay-txt");
    if(ov)ov.style.display="flex"; if(txt)txt.textContent="scope Web Control URL is not configured"; }
  const cm=b.capability_manifest||{};
  const limits=cm.limits_in_force||{}, input=limits.scope_input||{};
  document.getElementById("rated-max").textContent=input.rated_max_input_v!=null?input.rated_max_input_v+" V declared":"unknown";
  document.getElementById("scope-input-mode").textContent=input.require_probe_ratio?"probe ratio declaration required":"probe ratio policy not configured";
  document.getElementById("bench").innerHTML=[
    [sc.idn||"Configured oscilloscope",sc.resource||"not bound",sc.bound?"var(--ok)":"var(--noeff)",sc.bound?"BOUND":"UNBOUND"],
    ["Glitcher",(cm.glitcher&&cm.glitcher.id)||"unconfigured",(cm.glitcher&&cm.glitcher.bound)?"var(--ok)":"var(--caution)",(cm.glitcher&&cm.glitcher.bound)?"BOUND":"CHECK"],
    [((cm.rig&&cm.rig.target_model)||"target")+" target",(cm.rig&&cm.rig.inject_point)||"configured injection point","var(--ok)","DUT"]
  ].map(([n,r,col,tag])=>`<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line)">
    <span style="width:7px;height:7px;border-radius:50%;background:${col};flex:none"></span>
    <div style="flex:1;min-width:0"><div style="font:600 11px/1.3 var(--mono);color:var(--tx)">${n}</div><div class="sub" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r}</div></div>
    <span style="font:600 9px/1 var(--mono);color:${col};letter-spacing:.06em">${tag}</span></div>`).join("");
  updateTrace();
}

/* ---------------- scope viewer modes ---------------- */
function scopeUrl(){ return state.scopeUrl||"about:blank"; }
// The embedded live view loads through GlitchLab's own origin (/scope/live), which re-serves the
// device's control.html with its blocking disconnect alert() neutralised and its sockets pinned to the
// scope IP. "open ↗" still opens the raw device page (scopeUrl()).
function scopeEmbedUrl(){ return "/scope/live"; }
function scopeMode(m){ state.scopeMode=m;
  if(state.scopePolicy&&state.scopePolicy.companion_access_allowed===false){
    applyScopePolicyControls(state.scopePolicy); toast("Scope access disabled: "+(state.scopePolicy.reason||"project-managed evidence owns the scope"),"caution"); return; }
  if(m==="mirror"&&state.scopePolicy&&state.scopePolicy.companion_capture_allowed===false){
    state.scopeMode="embed"; toast("Snapshot capture disabled: "+(state.scopePolicy.reason||"project evidence owns the scope"),"caution"); m="embed"; }
  const img=document.getElementById("scope-mirror"), fr=document.getElementById("scope-iframe");
  const bm=document.getElementById("mode-mirror"), be=document.getElementById("mode-embed");
  if(!img||!fr) return;
  bm&&bm.classList.toggle("acc",m==="mirror"); be&&be.classList.toggle("acc",m==="embed");
  const ov=document.getElementById("scope-overlay"), txt=document.getElementById("scope-overlay-txt");
  if(m==="embed"){ img.style.display="none"; fr.style.display="block"; stopMirror();
    // If this window is hidden, stay disconnected so an event-driven loadInstruments (e.g. scope_bound)
    // can't steal the single video socket from a foreground window. The reveal handler reconnects.
    if(document.hidden){ state._scopeHidden=true;
      if(fr.getAttribute("src")!=="about:blank") fr.setAttribute("src","about:blank"); return; }
    const wasReclaim=state._scopeReclaim; clearScopeReclaim();
    if(fr.getAttribute("src")!==scopeEmbedUrl()){
      if(ov){ ov.style.display="flex"; if(txt) txt.textContent="connecting to the live scope…"; }
      fr.onload=()=>{ setTimeout(()=>{ if(state.scopeMode==="embed"&&!state._scopeReclaim&&ov) ov.style.display="none"; },700); };
      fr.setAttribute("src",scopeEmbedUrl());
    } else if(wasReclaim){ reloadScopeIframe(); }   // a reclaim was pending on the same src — actually reconnect
    else if(ov){ ov.style.display="none"; }
  } else { clearScopeReclaim(); fr.style.display="none"; img.style.display="block"; if(ov)ov.style.display="flex"; startMirror(); }
}
function openScopeWindow(){ if(state.scopePolicy&&state.scopePolicy.companion_access_allowed===false){
  toast("Scope Web Control disabled: "+(state.scopePolicy.reason||"project-managed evidence owns the scope"),"caution"); return; }
  if(scopeUrl()!=="about:blank") window.open(scopeUrl(),"_blank"); }
// Force a fresh live connection (re-grabs the scope's single video socket for THIS window).
function reloadScopeIframe(){ const fr=document.getElementById("scope-iframe"); if(!fr) return;
  if(state.scopePolicy&&state.scopePolicy.companion_access_allowed===false){ applyScopePolicyControls(state.scopePolicy); return; }
  clearScopeReclaim();
  const ov=document.getElementById("scope-overlay"), txt=document.getElementById("scope-overlay-txt");
  if(ov) ov.style.display="flex"; if(txt) txt.textContent="reconnecting to the live scope…";
  fr.setAttribute("src","about:blank");
  setTimeout(()=>{ if(state.page==="instruments"&&state.scopeMode==="embed") fr.setAttribute("src",scopeEmbedUrl()); },120); }
// The device lost its single video socket (another window took it). Show a non-blocking, clickable
// overlay instead of the device's un-dismissable alert().
function showScopeReclaim(){ if(state.scopeMode!=="embed") return; state._scopeReclaim=true;
  const ov=document.getElementById("scope-overlay"), txt=document.getElementById("scope-overlay-txt");
  const sp=ov&&ov.firstElementChild; if(sp) sp.style.display="none";
  if(txt){ txt.innerHTML="live view was taken by another window<br><span style='color:var(--acc);text-decoration:underline'>click to reclaim it here</span>"; }
  if(ov){ ov.style.display="flex"; ov.style.pointerEvents="auto"; ov.style.cursor="pointer";
    ov.onclick=()=>reloadScopeIframe(); } }
function clearScopeReclaim(){ state._scopeReclaim=false; clearTimeout(state._scopeRetry);
  const ov=document.getElementById("scope-overlay"); if(!ov) return;
  const sp=ov.firstElementChild; if(sp) sp.style.display="";
  ov.style.pointerEvents="none"; ov.style.cursor=""; ov.onclick=null; }
// A disconnect alert arrived from the shim. Tell apart "another window took the socket" (reclaimable) from
// "the scope is unreachable" (auto-retry with honest text) — don't blame contention for a real outage.
function handleScopeAlert(){ if(state.scopeMode!=="embed") return;
  fetch("/api/scope/reachable").then(r=>r.json()).then(j=>{
    if(j&&j.reachable===false) showScopeUnreachable(); else showScopeReclaim();
  }).catch(()=>showScopeReclaim()); }
function showScopeUnreachable(){ if(state.scopeMode!=="embed") return; state._scopeReclaim=true;
  if(state.scopePolicy&&state.scopePolicy.companion_access_allowed===false){ applyScopePolicyControls(state.scopePolicy); return; }
  const ov=document.getElementById("scope-overlay"), txt=document.getElementById("scope-overlay-txt");
  const sp=ov&&ov.firstElementChild; if(sp) sp.style.display="";     // keep the spinner — we're retrying
  if(txt) txt.textContent="scope unreachable — retrying…";
  if(ov){ ov.style.display="flex"; ov.style.pointerEvents="none"; ov.style.cursor=""; ov.onclick=null; }
  clearTimeout(state._scopeRetry);
  state._scopeRetry=setTimeout(()=>{ if(state.page==="instruments"&&state.scopeMode==="embed") reloadScopeIframe(); },4000); }
// Hear the embed shim's disconnect message, and release the scope's single video socket while this
// window is hidden/minimised so background windows stop stealing it from the foreground one.
function initScopeGate(){
  window.addEventListener("message",e=>{ const fr=document.getElementById("scope-iframe");
    if(!fr||e.source!==fr.contentWindow) return;   // only our sandboxed /scope/live iframe (its origin is "null")
    const d=e&&e.data; if(d&&d.t==="glitchlab-scope-alert") handleScopeAlert(); });
  document.addEventListener("visibilitychange",()=>{
    const fr=document.getElementById("scope-iframe"); if(!fr) return;
    if(document.hidden){ if(state.scopeMode==="embed"&&fr.getAttribute("src")!=="about:blank"){
        state._scopeHidden=true; fr.setAttribute("src","about:blank"); } }
    else if(state._scopeHidden){ state._scopeHidden=false;
      if(state.page==="instruments"&&state.scopeMode==="embed"&&(!state.scopePolicy||state.scopePolicy.companion_access_allowed!==false)){ clearScopeReclaim();
        fr.setAttribute("src",scopeEmbedUrl()); } }
  });
}
function startMirror(){ stopMirror(); refreshMirror(); state.mirrorTimer=setInterval(refreshMirror,1500); }
function stopMirror(){ if(state.mirrorTimer){ clearInterval(state.mirrorTimer); state.mirrorTimer=null; } }
function refreshMirror(){ if(state.page!=="instruments"||state.scopeMode!=="mirror") { stopMirror(); return; }
  if(state.scopePolicy&&state.scopePolicy.companion_capture_allowed===false){ stopMirror(); scopeMode("embed"); return; }
  const img=document.getElementById("scope-mirror"), ov=document.getElementById("scope-overlay"),
        txt=document.getElementById("scope-overlay-txt"); if(!img) return;
  const test=new Image();
  test.onload=()=>{ img.src=test.src; if(ov) ov.style.display="none"; };
  test.onerror=()=>{ if(ov) ov.style.display="flex";
    if(txt) txt.textContent=state.scopeBound?"waiting for scope frame…":"scope not bound — use bind"; };
  test.src="/api/scope/screenshot.png?t="+Date.now();
}
/* ---------------- analysis ---------------- */
async function loadAnalysis(){
  const sc=curScope(), hero=document.getElementById("analysis-hero"), insight=document.getElementById("an-insights");
  if(!sc){ if(hero) hero.innerHTML=""; if(insight) insight.textContent="Select a campaign to analyze its stored attempts."; return; }
  const current=(state.campaigns||[]).find(c=>c.id===sc.id), label=current?current.name:(sc.name||sc.id);
  document.getElementById("ana-scope").textContent=`${sc.kind} · ${label}`;
  document.getElementById("an-target-chip").textContent="target: "+((current&&current.target)||"current selection");
  try{
    const [summary,rolling,throughput,workflow]=await Promise.all([
      fetch(summaryUrl()).then(r=>r.json()),
      fetch(statsUrl("rolling_rate")).then(r=>r.json()),
      fetch(statsUrl("throughput")).then(r=>r.json()),
      fetch(workflowUrl()).then(r=>r.json())]);
    if(summary.error) throw new Error(summary.error);
    const totals=summary.totals||{}, coverage=summary.coverage||{}, counts=workflow.counts||{};
    const attempts=totals.attempts||0, candidates=totals.success||0, confirmed=counts.confirmed||0;
    hero.innerHTML=[
      ["Attempts",attempts.toLocaleString(),`${coverage.cells_with_data||0} parameter cells`],
      ["Candidate rate",attempts?(100*candidates/attempts).toFixed(2)+"%":"0.00%",`${candidates} candidate outcomes`],
      ["Fully confirmed",confirmed.toLocaleString(),"complete connector evidence"],
      ["Throughput",(throughput.attempts_per_sec||0).toFixed(2)+"/s",throughput.avg_attempt_ms?`${throughput.avg_attempt_ms} ms average`:"no duration data"]
    ].map(([l,v,s])=>`<div class="herocard"><div class="hlab">${l}</div><div class="hval">${v}</div><div class="hsub">${s}</div></div>`).join("");
    outcomeChart("an-outcomes",totals);
    responseChart("an-width",(summary.marginals||{}).width||[],"pulse cycles");
    responseChart("an-offset",(summary.marginals||{}).offset||[],"offset");
    trendChart("an-trend",rolling);
    const hot=summary.hotspot, box=summary.suggested_refine_bbox, lines=[];
    lines.push(`<div><span class="insight-label">Coverage</span>${coverage.cells_with_data||0} of ${coverage.cells||0} cells sampled; minimum ${coverage.min_trials_per_cell||0} trial(s) per sampled cell. ${coverage.low_confidence_cells||0} cells remain low confidence.</div>`);
    lines.push(hot?`<div><span class="insight-label">Best observed cell</span>pulse ${hot.width}, offset ${hot.offset}: ${(100*hot.rate).toFixed(1)}% candidates across ${hot.trials} trials.</div>`:'<div><span class="insight-label">Best observed cell</span>No candidate hotspot has been measured yet.</div>');
    lines.push(box?`<div><span class="insight-label">Suggested next area</span>pulse ${box.width[0]}–${box.width[1]}, offset ${box.offset[0]}–${box.offset[1]}. Increase repeats before narrowing further.</div>`:'<div><span class="insight-label">Suggested next area</span>Keep broad coverage until a candidate cluster appears.</div>');
    if(!(totals["no-effect"]||0)&&(totals["false-positive"]||0)) lines.push(`<div><span class="insight-label">Why no “no effect”?</span>This selection contains no clean connector misses. Its ${totals["false-positive"]} false-positive rows all produced some target response but failed later confirmation; inspect each row for partial-connection or runtime-failure detail.</div>`);
    lines.push(`<div><span class="insight-label">Evidence</span>${candidates} candidate outcomes; ${confirmed} fully confirmed. Candidates are not promoted without the active connector’s complete evidence contract.</div>`);
    insight.innerHTML=lines.join("");
  }catch(e){ if(hero) hero.innerHTML=""; if(insight) insight.textContent="Analysis could not be loaded: "+e; ["an-outcomes","an-width","an-offset","an-trend"].forEach(id=>emptyChart(id,"analysis unavailable")); }
}
function chartFrame(){ return '<line x1="38" y1="158" x2="405" y2="158" stroke="var(--line2)"/><line x1="38" y1="16" x2="38" y2="158" stroke="var(--line2)"/>'; }
function emptyChart(id,message){ const svg=document.getElementById(id); if(svg) svg.innerHTML=chartFrame()+`<text x="220" y="92" text-anchor="middle" fill="var(--tx3)" font-size="10" font-family="var(--mono)">${esc(message)}</text>`; }
function outcomeChart(id,totals){ const svg=document.getElementById(id); if(!svg)return;
  const rows=Object.entries(totals||{}).filter(([k,v])=>k!=="attempts"&&v>0).sort((a,b)=>b[1]-a[1]).slice(0,6);
  if(!rows.length){ emptyChart(id,"no attempts"); return; }
  const max=Math.max(...rows.map(r=>r[1]),1), barH=20, y0=24;
  svg.innerHTML=rows.map(([k,v],i)=>{ const y=y0+i*25,w=245*v/max,col=(OC[k]||OC["no-data"]).c; return `<text x="8" y="${y+13}" fill="var(--tx3)" font-size="9" font-family="var(--mono)">${esc(k)}</text><rect x="112" y="${y}" width="${w}" height="${barH}" rx="2" fill="${col}" opacity=".7"/><text x="${Math.min(398,118+w)}" y="${y+13}" fill="var(--tx)" font-size="9" font-family="var(--mono)">${v}</text>`; }).join("");
}
function responseChart(id,points,label){ const svg=document.getElementById(id); if(!svg)return; points=points||[];
  if(!points.length){ emptyChart(id,"no parameter data"); return; }
  const vals=points.map(p=>Number(p[0])), rates=points.map(p=>Number(p[1])||0), xmin=Math.min(...vals), xmax=Math.max(...vals), ymax=Math.max(...rates,.01);
  const x=v=>38+(xmax===xmin ? .5 : (v-xmin)/(xmax-xmin))*367, y=v=>158-v/ymax*134;
  const path=points.map((p,i)=>`${i?"L":"M"}${x(Number(p[0])).toFixed(1)} ${y(Number(p[1])||0).toFixed(1)}`).join(" ");
  svg.innerHTML=chartFrame()+`<path d="${path}" fill="none" stroke="var(--acc)" stroke-width="2"/>`+points.map(p=>`<circle cx="${x(Number(p[0])).toFixed(1)}" cy="${y(Number(p[1])||0).toFixed(1)}" r="2.5" fill="var(--acc)"/>`).join("")+`<text x="38" y="178" fill="var(--tx3)" font-size="9" font-family="var(--mono)">${xmin}</text><text x="405" y="178" text-anchor="end" fill="var(--tx3)" font-size="9" font-family="var(--mono)">${xmax} ${esc(label)}</text><text x="42" y="13" fill="var(--tx3)" font-size="9" font-family="var(--mono)">${(ymax*100).toFixed(1)}%</text>`;
}
function trendChart(id,rr){ const svg=document.getElementById(id); if(!svg)return; const cumulative=(rr&&rr.cumulative_series)||[], rolling=(rr&&rr.rolling_series)||[];
  if(!cumulative.length){ emptyChart(id,"no attempts"); return; }
  const ymax=Math.max(...cumulative,...rolling,.01), path=(data,col)=>{ const pts=data.map((v,i)=>`${i?"L":"M"}${(38+i/Math.max(1,data.length-1)*367).toFixed(1)} ${(158-v/ymax*134).toFixed(1)}`).join(" "); return `<path d="${pts}" fill="none" stroke="${col}" stroke-width="2"/>`; };
  svg.innerHTML=chartFrame()+path(cumulative,"var(--ok)")+path(rolling,"var(--acc)")+`<text x="42" y="13" fill="var(--tx3)" font-size="9" font-family="var(--mono)">${(ymax*100).toFixed(1)}%</text><text x="405" y="178" text-anchor="end" fill="var(--tx3)" font-size="9" font-family="var(--mono)">last ${cumulative.length} attempts</text><text x="48" y="150" fill="var(--ok)" font-size="9" font-family="var(--mono)">cumulative</text><text x="112" y="150" fill="var(--acc)" font-size="9" font-family="var(--mono)">rolling</text>`;
}

/* ---------------- param db / settings / boot ---------------- */
async function loadParamDb(){ const body=document.getElementById("kg-body");
  try{ const d=await (await fetch("/api/parameter-profiles")).json(), rows=[];
    (d.configured||[]).forEach(p=>{ const ax=(p.parameters&&p.parameters.axes)||{}, pc=axisBounds(ax.pulse_cycles), eo=axisBounds(ax.ext_offset), proven=p.proven_result||{};
      rows.push({target:p.target_model,injection:p.injection_type,pulse:`${pc.min}..${pc.max} cyc`,offset:`${eo.min}..${eo.max}`,
        rate:proven.verified_successes!=null?`${proven.verified_successes} / ${proven.exact_cell_shots||"?"} documented prior`:p.status,
        provenance:`project recipe:${p.name} · ${p.status} · not evidence from this store`,confirmed:false}); });
    (d.stored||[]).forEach(p=>{ const k=p.known_good||{}, ax=k.axes||k, pc=axisBounds(ax.pulse_cycles??ax.width), eo=axisBounds(ax.ext_offset??ax.offset);
      rows.push({target:p.target_model,injection:p.injection_type,pulse:Number.isFinite(pc.min)?`${pc.min}..${pc.max} cyc`:"—",offset:Number.isFinite(eo.min)?`${eo.min}..${eo.max}`:"—",
        rate:p.status,provenance:`stored #${p.id} · ${p.status}`,confirmed:p.status==="fully_confirmed"}); });
    body.innerHTML=rows.length?rows.map(p=>`<div class="tbl-r" style="grid-template-columns:90px 1fr 1fr 1fr 1fr 1fr"><span style="color:var(--acc);font-weight:600">${esc(p.target||"unknown")}</span><span>${esc(p.injection||"—")}</span><span style="color:var(--tx)">${esc(p.pulse)}</span><span style="color:var(--tx)">${esc(p.offset)}</span><span style="color:${p.confirmed?'var(--ok)':'var(--caution)'};font-weight:600">${esc(p.rate)}</span><span>${esc(p.provenance)}</span></div>`).join(""):
      '<div class="sub" style="padding:16px">No configured or confirmation-linked parameter presets.</div>';
  }catch(e){ body.innerHTML='<div class="sub" style="padding:16px">Parameter profiles unavailable.</div>'; } }
async function loadSettings(){ const b=await (await fetch("/api/bootstrap")).json();
  const cm=b.capability_manifest||{}, rig=cm.rig||{}, lim=cm.limits_in_force||{}, gl=lim.glitch||{}, power=lim.target_power||{}, rec=lim.recovery||{}, rate=lim.rate||{};
  document.getElementById("rig-summary").innerHTML=kv({name:rig.name||"unnamed rig",target:rig.target_model||"unknown",package:rig.target_package||"—",glitcher:rig.glitcher||"unconfigured"});
  document.getElementById("rig-glitch-limits").innerHTML=kv({"pulse cycles max":gl.pulse_cycles_max??gl.width_cycles_max??"—","offset min":gl.ext_offset_min??"—","offset max":gl.ext_offset_max??"—","repeats max":gl.repeat_max??"—","simultaneous outputs":gl.hp_lp_both_forbidden?"forbidden":"allowed"});
  document.getElementById("rig-power-limits").innerHTML=kv({"nominal Vcc":power.vcc_nominal_v!=null?power.vcc_nominal_v+" V":"—","maximum Vcc":power.vcc_max_v!=null?power.vcc_max_v+" V":"—"});
  document.getElementById("rig-rate-limits").innerHTML=kv({"minimum recovery":rec.min_seconds_between_cycles!=null?rec.min_seconds_between_cycles+" s":"—","cycles / minute max":rec.max_cycles_per_minute??"—","attempts / second max":rate.max_attempts_per_second??"—"});
  const ns=b.notifications||{}, nbox=document.getElementById("notification-status"), nbtn=document.getElementById("notification-test");
  const nen=document.getElementById("notification-enabled"), ntopic=document.getElementById("notification-topic"), nbase=document.getElementById("notification-base");
  if(nen)nen.checked=!!ns.enabled; if(ntopic)ntopic.placeholder=ns.configured?`configured (${ns.topic||"private"})`:"enter a private topic"; if(nbase)nbase.value=ns.base_url||"https://ntfy.sh";
  if(ns.enabled){ nbox.innerHTML=`<span class="notify-state ok">ENABLED</span><span>destination ${esc(ns.topic||"configured")} · ${esc(ns.base_url||"")}</span>${ns.last_success_at?`<span>last delivered ${esc(ns.last_success_at)}</span>`:""}${ns.last_error?`<span class="notify-error">last error: ${esc(ns.last_error)}</span>`:""}`; if(nbtn)nbtn.classList.add("acc"); }
  else { nbox.innerHTML='<span class="notify-state caution">DISABLED</span><span>Enable alerts and enter a private topic above. The topic is never included in the plugin or campaign exports.</span>'; if(nbtn)nbtn.classList.remove("acc"); }
  const au=await (await fetch("/api/audit")).json();
  document.getElementById("audit-body").innerHTML=(au.audit||[]).map(a=>`<div class="tbl-r" style="grid-template-columns:1fr 90px 90px 1fr">
    <span style="color:var(--tx)">${a.tool}</span><span style="color:${a.danger==='DANGER'?'var(--danger)':a.danger==='CAUTION'?'var(--caution)':'var(--ok)'};font-weight:600">${a.danger}</span>
    <span>${a.decision}</span><span class="sub">${a.violated_rule||""}</span></div>`).join(""); }

async function boot(){ const b=await (await fetch("/api/bootstrap")).json(); state.boot=b;
  state.projects=b.projects||[]; state.rigProject=b.active_project; state.activeProject=state.rigProject; updateProjName();
  const persistedStatus=b.active&&b.active.sweep_status;
  if(persistedStatus==="candidate-preserved") state.lastSweepStatus="candidate preserved · inspect evidence";
  else if(persistedStatus==="infrastructure-failure") state.lastSweepStatus="infrastructure stop · inspect before resume";
  else if(persistedStatus==="preflight-failure") state.lastSweepStatus="preflight stop · 0 valid";
  applyTargetState(b.target_state||{});
  applyDanger(b.danger_state||{});
  if(b.capability_manifest){ const cm=b.capability_manifest; const tm=(cm.rig&&cm.rig.target_model)||"target";
    document.getElementById("crumb-camp").textContent=tm+" · fault injection";
    applyProjectProfile(b.project_profile||{},cm.glitcher&&cm.glitcher.connect_result); }
  if(b.active&&b.active.sweep_id){ state.activeSweep=b.active.sweep_id; state.scope={kind:"sweep",id:b.active.sweep_id}; refreshLive();
    try{ const t=await (await fetch("/api/sweep/"+b.active.sweep_id+"/timing")).json();
      state.running=!!(t&&t.running); if(t&&t.total!=null) state.sweepTotal=t.total;
      if(state.running&&t.elapsed_s!=null) state.t0=Date.now()-t.elapsed_s*1000; }catch(e){}
  }
  else { try{ const ov=await (await fetch("/api/overview")).json(); state.campaigns=ov.campaigns||[];
      const best=preferredCampaign(ov.campaigns,b.active&&b.active.campaign_id);
      if(best){ state.scope={kind:"campaign",id:best.id,name:best.name};
        document.getElementById("crumb-camp").textContent=best.name; refreshLive(); }
      else { drawEmptyHeat(); renderTiles(); } }catch(e){ drawEmptyHeat(); renderTiles(); } }
  await loadConnectors();
  setInterval(()=>{ if(!state.running&&!state.formDirty) loadConnectors(); },15000);
  populateControlFromLive(); updateControlUI(); loadWorkflow();
  const initialPage=(history.state&&history.state.page)||location.hash.slice(1)||state.page;
  navigate(PAGES[initialPage]?initialPage:"live",{replace:true});
}

/* ---------------- toast ---------------- */
function toast(msg,level){ const w=document.getElementById("toasts"); const t=document.createElement("div");
  t.className="toast "+(level||"info"); t.textContent=msg; w.appendChild(t);
  setTimeout(()=>{ t.style.opacity=0; setTimeout(()=>t.remove(),300); },3200); }

/* ---------------- hover tooltips ---------------- */
let _curTip=null;
function showTip(el){ const raw=el.getAttribute("data-tip"); if(!raw)return; const tip=document.getElementById("tip");
  let title="",body=raw; if(raw[0]==="§"){ const p=raw.slice(1).split("§"); title=p[0]; body=p.slice(1).join("§"); }
  tip.innerHTML=(title?`<div class="tt">${title}</div>`:"")+`<div class="tk">${body}</div>`;
  tip.classList.add("show"); positionTip(el); }
function hideTip(){ document.getElementById("tip").classList.remove("show"); }
function positionTip(el){ const tip=document.getElementById("tip"); const r=el.getBoundingClientRect();
  const tw=tip.offsetWidth, th=tip.offsetHeight; let x=r.left, y=r.bottom+8;
  if(x+tw>window.innerWidth-8) x=window.innerWidth-tw-8; if(x<8) x=8;
  if(y+th>window.innerHeight-8) y=r.top-th-8; if(y<8) y=8;
  tip.style.left=Math.round(x)+"px"; tip.style.top=Math.round(y)+"px"; }
function initTips(){
  document.addEventListener("mouseover",e=>{ const el=e.target.closest("[data-tip]"); if(el===_curTip)return; if(el){ _curTip=el; showTip(el); } });
  document.addEventListener("mouseout",e=>{ const el=e.target.closest("[data-tip]"); if(!el)return;
    const to=e.relatedTarget?(e.relatedTarget.closest?e.relatedTarget.closest("[data-tip]"):null):null;
    if(to!==el && _curTip===el){ hideTip(); _curTip=null; } });
  document.addEventListener("click",()=>{ hideTip(); _curTip=null;
    const m=document.getElementById("projmenu"); if(m) m.classList.remove("show"); });
}

document.addEventListener("input",e=>{ if(e.target.matches("[data-mcp-field],[data-connector-param]")) state.formDirty=true; });
document.addEventListener("change",e=>{ if(e.target.matches("[data-mcp-field],[data-connector-param],#f-connector")) state.formDirty=true; });
connect(); boot(); drawEmptyHeat(); renderTiles(); renderLegend(); initTips(); initScopeGate();
setInterval(reportState,4000);
