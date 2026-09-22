/* Shared behavior for Relay's four independently rendered pages. */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const page = document.body.dataset.page;
  const query = new URLSearchParams(location.search);
  const terminal = new Set(['success','business_outcome','failure']);
  const runIdPattern = /^[a-f0-9]{32}$/;
  const state = {mode:query.get('mode')==='replay'?'replay':'discover',run:null,runId:null,capability:null,workspace:null,loading:false,acting:false,fetching:false,workspaceFetching:false,pollTimer:null,workspaceTimer:null,pollErrors:0,eventsSignature:'',renderedEvents:[],historySignature:'',connected:false,selectionInitialized:false,capabilitySignature:''};
  const text = (id,value) => {if($(id))$(id).textContent=String(value??'—');};
  const on = (id,type,handler) => $(id)?.addEventListener(type,handler);
  const scenarioHelp = {normal:'A signed-in session with the member’s records available.',not_found:'Check how the workflow handles a member who is not in the records.',session:'The workflow must pause for an operator to restore the session.',transient:'A temporary interruption tests recovery within the workflow.',permission:'The application restricts access and may need operator attention.',error:'An application failure tests a safe, explicit stopping point.',dialog:'An unexpected application dialog pauses the workflow for review.',slow:'The application responds slowly so the workflow must wait appropriately.',ambiguous:'Multiple matching records test whether the workflow avoids guessing.'};
  const labels = {running:'Making progress.',awaiting_operator:'Over to you.',human_control:'You have control.',success:'Goal completed.',business_outcome:'A clear outcome.',failure:'Run stopped.'};
  const descriptions = {running:'The workflow is interacting with the application. Follow its progress below.',awaiting_operator:'The workflow needs a human decision before it can safely continue.',human_control:'The same session is yours to manage. Return control when the application is ready.',success:'The workflow finished and returned its result.',business_outcome:'The workflow reached a business outcome. Review the details below.',failure:'The workflow could not complete. The activity trail shows where it stopped.'};
  const owners = {automation:'Automation in control',awaiting_operator:'Waiting for operator',human:'Operator in control',finished:'Run finished'};
  const humanize = value => typeof value === 'string' ? value.replace(/[_-]+/g,' ').replace(/\b\w/g,c=>c.toUpperCase()) : value == null ? '—' : String(value);
  const stringValue = value => typeof value === 'string' ? value : typeof value === 'object' && value !== null ? JSON.stringify(value,null,2) : String(value ?? '');

  function rememberRun(id){try{if(id)sessionStorage.setItem('relay.run',id);else sessionStorage.removeItem('relay.run');}catch{}}
  function savedRun(){try{return sessionStorage.getItem('relay.run');}catch{return null;}}
  function runUrl(destination,id=state.runId){return '/'+destination+(id?'?run='+encodeURIComponent(id):'');}
  function error(message){text('error',message||'');$('error').hidden=!message;}
  let toastTimer;
  function toast(message){clearTimeout(toastTimer);text('toast',message);$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,3000);}
  async function api(path,options={}){
    const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),20000);
    try{
      const response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...options.headers},signal:controller.signal});
      let data;try{data=await response.json();}catch{throw new Error('The workspace returned an unreadable response.');}
      if(!response.ok){const failure=new Error(typeof data.detail==='string'?data.detail:'Request failed ('+response.status+').');failure.status=response.status;throw failure;}
      return data;
    }catch(err){if(err.name==='AbortError')throw new Error('The request timed out. Check the execution page before starting another run.');throw err;}
    finally{clearTimeout(timer);}
  }
  function connection(ok){state.connected=ok;$('connection-dot').className='connection-dot '+(ok?'connected':'disconnected');text('connection-label',ok?'Workspace connected':'Connection interrupted');}
  function setMode(mode){
    if(state.loading||state.workspace?.active_run_id)return;
    state.mode=mode;
    document.querySelectorAll('[data-mode]').forEach(button=>{const selected=button.dataset.mode===mode;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;});
    text('mode-description',mode==='discover'?'Learn a reusable workflow from a goal and the live application. Requires the local model.':'Run the saved workflow with new inputs. No model service is needed.');
    updateControls();
  }
  function updateControls(){
    const formBusy=state.loading||!state.workspace||Boolean(state.workspace.active_run_id);
    for(const id of ['goal','member-id','scenario','discover-tab','replay-tab','start'])if($(id))$(id).disabled=formBusy;
    document.querySelectorAll('[data-preset]').forEach(button=>button.disabled=formBusy);
    text('start-label',state.loading?'Starting…':state.workspace?.active_run_id?'A run is in progress':!state.workspace?'Connecting…':state.mode==='discover'?'Discover workflow':'Replay workflow');
    if(!$('operator-panel'))return;
    const owner=state.run?.owner;
    const awaiting=state.run?.status==='awaiting_operator'&&owner==='awaiting_operator';
    const human=state.run?.status==='human_control'&&owner==='human';
    $('claim').hidden=!awaiting;$('claim').disabled=!awaiting||state.acting;
    for(const id of ['restore','retry','refresh','resume']){$(id).hidden=!human;$(id).disabled=!human||state.acting;}
    const observation=state.run?.observation??state.run?.intervention?.observation;
    const available=new Set((observation?.controls??[]).map(control=>control.target?.name));
    $('restore').disabled=!human||state.acting||!available.has('Restore session');
    $('retry').disabled=!human||state.acting||!available.has('Retry');
    $('resume').disabled=!human||state.acting||observation?.condition!=='ready';
    $('resume').title=observation?.condition==='ready'?'Continue in this session':'Resolve the application state first';
    $('abort').disabled=(!awaiting&&!human)||state.acting;
    $('cancel-run').hidden=!state.run||terminal.has(state.run.status)||awaiting||human;
    $('cancel-run').disabled=state.acting;
  }
  function renderWorkspace(workspace){
    state.workspace=workspace;
    const active=workspace.runs.find(run=>run.id===workspace.active_run_id);
    $('active-run-link').hidden=!active;
    if(active){$('active-run-link').href=runUrl('execution',active.id);const paused=['awaiting_operator','human_control'].includes(active.status);$('active-run-link').classList.toggle('warn',paused);text('active-run-label',paused?'A run needs your attention':'Open active run');}
    if(page==='configure')text('setup-description',active?'Finish the active run before starting another.':'A goal, an input, a clear outcome.');
    if(page==='activity')renderHistory(workspace.runs);
    updateControls();
  }
  function updateRunLinks(){
    document.querySelectorAll('[data-run-link]').forEach(link=>link.href=runUrl(link.dataset.runLink));
    for(const target of ['execution','activity'])document.querySelector('[data-nav="'+target+'"]').href=runUrl(target);
  }
  function renderHistory(runs){
    const signature=JSON.stringify([state.runId,runs]);if(signature===state.historySignature)return;state.historySignature=signature;
    text('history-count',runs.length);const box=$('run-history');box.replaceChildren();
    if(!runs.length){const empty=document.createElement('div');empty.className='history-empty';empty.textContent='No runs yet.';const link=document.createElement('a');link.href='/configure';link.textContent='Create your first run ↗';empty.append(link);box.append(empty);return;}
    runs.forEach(run=>{const link=document.createElement('a');link.href=runUrl('activity',run.id);link.className='history-item';link.setAttribute('aria-current',String(run.id===state.runId));const title=document.createElement('div');title.className='history-title';title.textContent=humanize(run.mode)+' · '+({running:'Running',success:'Complete',failure:'Stopped',business_outcome:'Outcome',awaiting_operator:'Needs you',human_control:'Human control'}[run.status]??humanize(run.status));const dot=document.createElement('span');dot.className='status-dot '+run.status;dot.setAttribute('aria-hidden','true');title.append(dot);const meta=document.createElement('div');meta.className='history-meta';const id=document.createElement('span');id.className='mono';id.textContent=run.id.slice(0,8);const time=document.createElement('time');if(run.started_at){time.dateTime=run.started_at;time.textContent=new Date(run.started_at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});}else time.textContent='No actions';meta.append(id,time);link.append(title,meta);box.append(link);});
  }
  function matchesFilter(event,filter){
    const name=event.event??'';
    return filter==='all'||filter==='actions'&&name==='action_completed'||filter==='decisions'&&name==='model_decision'||filter==='handoffs'&&/intervention|control_transferred|operator_action|bounded_recovery/.test(name)||filter==='outcomes'&&/run_finished|checkpoint_verified|execution_stopped|abort_requested/.test(name);
  }
  function renderEvents(events){
    if(!$('activity'))return;const allEvents=events;const filter=$('event-filter')?.value??'all';events=events.filter(event=>matchesFilter(event,filter));const signature=filter+allEvents.length+JSON.stringify(events);if(signature===state.eventsSignature)return;state.eventsSignature=signature;
    const box=$('activity'),nearBottom=box.scrollHeight-box.scrollTop-box.clientHeight<65;
    const previous=state.renderedEvents;const appendOnly=previous.length>0&&events.length>=previous.length&&previous.every((event,index)=>JSON.stringify(event)===JSON.stringify(events[index]));const startIndex=appendOnly?previous.length:0;if(!appendOnly)box.replaceChildren();state.renderedEvents=events.map(event=>({...event}));$('event-count').textContent=events.length+' / '+allEvents.length+' EVENTS';
    if(!events.length){const empty=document.createElement('div');empty.className='empty';const title=document.createElement('h3');title.textContent=allEvents.length?'No matching events.':'Waiting for the first action.';const note=document.createElement('p');note.textContent=allEvents.length?'Choose another filter to inspect this run.':'Events will appear as the application responds.';empty.append(title,note);box.append(empty);return;}
    for(const event of events.slice(startIndex)){
      const row=document.createElement('div');const name=String(event.event??event.type??'Update');const severity=[name,event.status,event.code].filter(Boolean).join(' ');const isBad=/fail|abort|error/.test(severity),isWarn=/pause|human|operator|intervention|timeout|business_outcome/.test(severity);row.className='event'+(isBad?' bad':isWarn?' warn':'');
      const dot=document.createElement('span');dot.className='event-dot';dot.setAttribute('aria-hidden','true');dot.textContent=isBad?'×':isWarn?'•':'✓';
      const body=document.createElement('div'),title=document.createElement('div');title.className='event-title';title.textContent=humanize(name);body.append(title);
      const details=[];if(event.event==='action_completed'&&event.step!==undefined&&event.step!==null)details.push('Action '+String(Number(event.step)+1));if(event.action)details.push(humanize(event.action));if(event.target?.name)details.push(event.target.name);if(event.code)details.push(humanize(event.code));if(event.message)details.push(stringValue(event.message));if(event.reason)details.push(stringValue(event.reason));
      if(details.length){const detail=document.createElement('div');detail.className='event-details';detail.textContent=details.join(' · ');body.append(detail);}
      const time=document.createElement('time');time.className='event-time';if(event.at){const date=new Date(event.at);time.textContent=Number.isNaN(date.getTime())?String(event.at):date.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});if(!Number.isNaN(date.getTime()))time.dateTime=date.toISOString();}
      row.append(dot,body,time);box.append(row);
    }
    if(nearBottom)box.scrollTop=box.scrollHeight;
  }
  function renderResult(run){
    if(!$('result'))return;const result=run.result;$('result').hidden=!result;$('diagnostics').hidden=true;if(!result)return;
    renderDiagnostics(result.diagnostics);const outputs=result.outputs??{};const value=outputs.current_balance??result.current_balance;const hasBalance=value!==undefined&&value!==null;
    $('result').classList.toggle('neutral',!hasBalance);$('balance').hidden=!hasBalance;$('result-other').hidden=true;
    $('result-eyebrow').textContent=hasBalance?'Current savings balance':humanize(result.status??'Result');
    if(hasBalance){
      const amount=typeof value==='object'&&value!==null?(value.value??value.amount??value):value;const currency=outputs.currency??value?.currency;
      if(currency&&typeof amount==='number'){try{$('balance').textContent=new Intl.NumberFormat('en-US',{style:'currency',currency}).format(amount);}catch{$('balance').textContent=String(amount)+' '+String(currency);}}
      else $('balance').textContent=typeof amount==='string'&&/^-?\d+\.\d{2}$/.test(amount)?'$'+amount.replace(/\B(?=(\d{3})+(?!\d))/g,','):stringValue(amount);
    }
    $('result-context').textContent=result.message??result.reason??(result.code?humanize(result.code):hasBalance?'Returned by the application for this run.':'Review the returned outcome.');
    if(!hasBalance&&Object.keys(outputs).length){$('result-other').hidden=false;$('result-other').textContent=JSON.stringify(outputs,null,2);}
  }
  function renderOperator(run){
    if(!$('operator-panel'))return;const visible=['awaiting_operator','human_control'].includes(run.status);$('operator-panel').hidden=!visible;if(!visible)return;
    const human=run.owner==='human',intervention=run.intervention??{},observation=run.observation??intervention.observation??{};
    $('operator-heading').textContent=human?'You have the session.':'Your attention is needed.';$('operator-state').textContent=human?'Human control':'Awaiting operator';
    $('operator-subtitle').textContent=human?'Automation will wait until you return control.':'Automation is paused at a safe handoff.';
    $('operator-reason').textContent=intervention.reason?humanize(intervention.reason):'Review the current application state before continuing.';
    $('operator-note').textContent=human?(observation.condition==='ready'?'The application is ready. Return control to continue the workflow.':'Available controls match the current application state. Resolve the interruption, then return control.'):'Claim this session to make a correction, then return control to continue.';
    $('observation-title').textContent=typeof observation==='string'?'Application observation':observation.headings?.filter(Boolean).join(' / ')||observation.title||observation.heading||observation.page_title||(intervention.step!==undefined?'Paused at step '+intervention.step:'Session state');
    const url=observation.url??observation.current_url??observation.route;$('observation-url').hidden=!url;$('observation-url').textContent=url??'';
    const controls=Array.isArray(observation.controls)?observation.controls:[];$('observation-controls').hidden=!controls.length;$('observation-controls').textContent=controls.length?'Available in this session: '+controls.map(control=>control.target?.name??control.name??control.id).filter(Boolean).join(' · '):'';
    const details=typeof observation==='string'?observation:observation.text??observation.body??observation.content??observation.message??(observation.condition?'State: '+humanize(observation.condition):Object.keys(observation).length&&!observation.headings&&!observation.controls?observation:null);
    $('observation-detail').textContent=stringValue(details);$('observation-detail').hidden=!details||(typeof details==='object'&&!Object.keys(details).length);
    const sessionId=run.session_id??intervention.session_id??observation.session_id;$('session-id').hidden=!sessionId;$('session-id').textContent=sessionId?'SESSION '+sessionId:'';
  }

  function renderPath(run){
    if(!$('workflow-steps'))return;const events=Array.isArray(run.events)?run.events:[];
    const completed=events.filter(event=>event.event==='action_completed');
    const verified=events.some(event=>event.event==='checkpoint_verified');
    const steps=run.capability?.steps;
    const items=steps?steps.map((step,index)=>({name:step.target?.name??'Action '+(index+1),done:run.mode==='discover'?index<completed.length:completed.some(event=>event.step===index)})):completed.map(event=>({name:event.target?.name??humanize(event.action),done:true}));
    if(steps||verified)items.push({name:'Verify result',done:verified});
    else items.push({name:terminal.has(run.status)?humanize(run.result?.code??run.status):run.status==='awaiting_operator'||run.status==='human_control'?'Operator handoff':'Observe & decide',done:false});
    const list=$('workflow-steps');list.replaceChildren();
    items.forEach((item,index)=>{const li=document.createElement('li');li.className='workflow-step'+(item.done?' done':index===items.findIndex(entry=>!entry.done)?' current':'');const marker=document.createElement('span');marker.className='step-marker';marker.textContent=item.done?'✓':String(index+1);marker.setAttribute('aria-hidden','true');const name=document.createElement('span');name.className='step-name';name.textContent=item.name;li.setAttribute('aria-label',item.name+(item.done?' — completed':' — pending'));if(!item.done&&li.classList.contains('current'))li.setAttribute('aria-current','step');li.append(marker,name);list.append(li);});
    $('path-state').textContent=verified?'Checkpoint verified':run.status==='awaiting_operator'||run.status==='human_control'?'Paused · same session':terminal.has(run.status)?'Run ended':run.mode==='replay'?'Following saved workflow':'Building the workflow';
  }

  function renderDiagnostics(diagnostics){
    const box=$('diagnostics');if(!box)return;box.replaceChildren();box.hidden=!diagnostics;if(!diagnostics)return;
    const expected=diagnostics.expected??{},observed=diagnostics.observed??{};
    const rows=[['Step',diagnostics.step===undefined?'—':String(diagnostics.step+1)],['Expected',expected.state],['Target',expected.target?.name],['Observed',humanize(observed.condition)],['Route',observed.route]];
    for(const [label,value] of rows){if(!value)continue;const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;box.append(dt,dd);}
  }
  function renderCapability(capability,source){
    state.capability=capability;if(page!=='workflows')return;
    $('workflow-detail').hidden=!capability;$('workflow-unavailable').hidden=Boolean(capability);if(!capability)return;
    $('artifact-empty').hidden=true;$('artifact-content').hidden=false;
    text('artifact-name',capability.name);text('artifact-json',JSON.stringify(capability,null,2));
    text('workflow-title',humanize(capability.name));text('workflow-source',source==='example'?'RECORDED DISCOVERY EXAMPLE':'LATEST SUCCESSFUL DISCOVERY');
    text('workflow-version','Schema '+capability.schema_version+' · revision '+capability.revision);text('workflow-product',capability.product+' / '+capability.product_version);
    const renderSpecs=(id,specs)=>{const list=$(id);list.replaceChildren();Object.entries(specs??{}).forEach(([name,spec])=>{const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=name+' · '+spec.type;dd.textContent=spec.description??'';list.append(dt,dd);});};
    renderSpecs('workflow-inputs',capability.inputs);renderSpecs('workflow-outputs',capability.outputs);
    text('workflow-checkpoint',capability.checkpoint.name+' · '+(capability.checkpoint.frame??'Main application'));
    text('workflow-step-count',capability.steps.length+' ACTIONS');
    const list=$('saved-steps');list.replaceChildren();capability.steps.forEach((step,index)=>{const li=document.createElement('li'),marker=document.createElement('span'),body=document.createElement('div'),name=document.createElement('strong'),detail=document.createElement('small');marker.className='step-marker';marker.textContent=String(index+1);name.textContent=humanize(step.action)+' · '+step.target.name;detail.textContent=[step.target.frame,step.input_ref?'Input: '+step.input_ref:'Exact '+(step.target.role??step.target.kind)+' target'].filter(Boolean).join(' · ');body.append(name,detail);li.append(marker,body);list.append(li);});
  }
  function renderSession(run){
    if(!$('session-details'))return;
    const observation=run.observation??run.intervention?.observation;
    $('session-empty').hidden=Boolean(observation);$('session-details').hidden=!observation;
    text('session-screen',observation?.headings?.join(' / ')||'Opening application');text('session-condition',humanize(observation?.condition));text('session-correlation',run.session_id);text('session-recoveries',(run.result?.recoveries??run.events.filter(event=>event.event==='bounded_recovery').length)+' / '+(run.result?.handoffs??run.events.filter(event=>event.event==='intervention_requested').length));
    const done=terminal.has(run.status),paused=['awaiting_operator','human_control'].includes(run.status);
    text('next-heading',run.status==='success'?'Ready for another run.':paused?'Your session is waiting.':done?'An explicit stopping point.':'Every action leaves a trail.');
    text('next-description',run.status==='success'?'Reuse the saved workflow with a fresh member input.':paused?'Use the controls above to resolve the interruption.':done?'Review the event trail and outcome before trying again.':'Open activity to inspect actions, model decisions, and handoffs.');
    $('next-link').href=run.status==='success'?'/configure?mode=replay':runUrl('activity');text('next-link',run.status==='success'?'Replay with new inputs ↗':'Inspect activity ↗');
  }
  function render(run){
    $('selection-empty')?.remove();document.querySelectorAll('.execution-grid,.activity-overview,.activity-card').forEach(el=>el.hidden=false);state.run=run;state.runId=run.id;rememberRun(run.id);updateRunLinks();
    if(state.workspace?.active_run_id===run.id&&terminal.has(run.status)){state.workspace.active_run_id=null;$('active-run-link').hidden=true;}
    text('selected-run-label','RUN / '+run.id);
    if($('execution-panel')){
      $('execution-panel').dataset.status=run.status;renderPath(run);text('status-label',labels[run.status]??humanize(run.status));text('status-description',descriptions[run.status]??'Inspect this run’s event trail.');
      text('owner-label',owners[run.owner]??humanize(run.owner));text('metric-step',run.events.filter(event=>event.event==='action_completed').length);text('metric-calls',run.model_calls);text('metric-mode',humanize(run.mode));$('run-id-row').hidden=false;text('run-id',run.id);
      const tag=$('live-tag');tag.textContent={running:'LIVE',success:'COMPLETE',business_outcome:'OUTCOME',failure:'STOPPED'}[run.status]??'PAUSED';tag.className='live-tag'+(run.status==='running'?' active':run.status==='failure'?' error-state':['awaiting_operator','human_control'].includes(run.status)?' warn':'');
      renderResult(run);renderOperator(run);renderSession(run);
    }
    if(page==='activity'){
      text('activity-status',labels[run.status]??humanize(run.status));text('activity-summary',humanize(run.mode)+' · '+run.id.slice(0,12)+' · '+(run.result?.code?humanize(run.result.code):owners[run.owner]));text('activity-calls',run.model_calls);text('activity-actions',run.events.filter(event=>event.event==='action_completed').length);text('activity-handoffs',run.result?.handoffs??run.events.filter(event=>event.event==='intervention_requested').length);
      $('download-events').disabled=!run.events.length;renderEvents(run.events);
    }
    updateControls();
  }
  function showMissingRun(){
    for(const selector of ['.execution-grid','.activity-overview','.activity-card'])document.querySelector(selector)?.setAttribute('hidden','');
    if($('cancel-run'))$('cancel-run').hidden=true;
    if($('selection-empty'))return;
    const box=document.createElement('section');box.id='selection-empty';box.className='card workflow-unavailable';const title=document.createElement('h2'),note=document.createElement('p'),link=document.createElement('a');title.textContent='This run is no longer available.';note.textContent='Runs are kept for this workspace session. Select another from Activity or start a fresh run.';link.href='/configure';link.className='secondary';link.textContent='Start a new run ↗';box.append(title,note,link);$('page-content').prepend(box);
  }
  async function refreshCapability(){
    if(page!=='workflows')return;
    try{const data=await api('/api/capability');const signature=JSON.stringify(data);if(signature!==state.capabilitySignature){state.capabilitySignature=signature;renderCapability(data.capability,data.source);}error('');}
    catch(err){error(err.message);$('workflow-detail').hidden=true;$('workflow-unavailable').hidden=false;state.capabilitySignature='';}
  }
  async function poll(){
    if(state.fetching||!state.runId)return;
    clearTimeout(state.pollTimer);state.fetching=true;const id=state.runId;
    try{const run=await api('/api/runs/'+id);if(state.runId!==id)return;render(run);if(state.pollErrors)error('');state.pollErrors=0;}
    catch(err){state.pollErrors++;if(err.status===404){state.run=null;state.runId=null;rememberRun(null);showMissingRun();error('This run is no longer in this workspace session. Choose a recent run or start a new one.');updateRunLinks();}else error('Unable to refresh the run. '+err.message);}
    finally{state.fetching=false;if(state.runId&&(!state.run||!terminal.has(state.run.status)))state.pollTimer=setTimeout(poll,Math.min(5000,900*(state.pollErrors+1)));}
  }
  async function refreshWorkspace(initial=false){
    if(state.workspaceFetching)return;state.workspaceFetching=true;
    try{
      const workspace=await api('/api/workspace');connection(true);renderWorkspace(workspace);
      if(!state.selectionInitialized&&['execution','activity'].includes(page)){state.selectionInitialized=true;
        const requested=query.get('run'),saved=savedRun();
        if(requested&&!runIdPattern.test(requested)){showMissingRun();error('The run address is invalid. Select a recent run from Activity.');}
        else {state.runId=requested??workspace.active_run_id??(workspace.runs.some(run=>run.id===saved)?saved:null)??workspace.latest_run_id;if(state.runId){updateRunLinks();await poll();renderHistoryIfPresent(workspace.runs);}}
      }
    }catch(err){connection(false);if(initial)error('Unable to connect to the workspace. '+err.message);}
    finally{state.workspaceFetching=false;clearTimeout(state.workspaceTimer);state.workspaceTimer=setTimeout(()=>refreshWorkspace(false),5000);}
  }
  function renderHistoryIfPresent(runs){if(page==='activity')renderHistory(runs);}
  async function act(action,body){
    if(state.acting||!state.runId)return;state.acting=true;error('');updateControls();
    try{const response=await api('/api/runs/'+state.runId+'/'+action,{method:'POST',...(body?{body:JSON.stringify(body)}:{})});render(response);await poll();await refreshWorkspace();}
    catch(err){error(err.message);await poll();}
    finally{state.acting=false;updateControls();}
  }
  function download(data,name,type){const blob=new Blob([data],{type});const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  on('run-form','submit',async event=>{
    event.preventDefault();if(state.loading||!state.workspace||state.workspace.active_run_id||!$('run-form').reportValidity())return;
    const goal=$('goal').value.trim(),member=$('member-id').value.trim();if(!goal||!member){error('Enter a goal and a member ID.');return;}
    state.loading=true;error('');updateControls();
    try{const result=await api('/api/runs',{method:'POST',body:JSON.stringify({mode:state.mode,goal,inputs:{member_id:member},scenario:$('scenario').value})});if(!runIdPattern.test(result.id))throw new Error('No valid run ID was returned.');rememberRun(result.id);location.assign(runUrl('execution',result.id));}
    catch(err){error(err.message);await refreshWorkspace();state.loading=false;updateControls();}
  });
  document.querySelectorAll('[data-mode]').forEach(button=>{button.addEventListener('click',()=>setMode(button.dataset.mode));button.addEventListener('keydown',event=>{if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();if(button.disabled)return;setMode(state.mode==='discover'?'replay':'discover');$(state.mode+'-tab').focus();}});});
  document.querySelectorAll('[data-preset]').forEach(button=>button.addEventListener('click',()=>{if(state.workspace?.active_run_id)return;setMode('replay');$('member-id').value=button.dataset.preset==='not_found'?'9999':'1002';$('scenario').value=button.dataset.preset;text('scenario-help',scenarioHelp[button.dataset.preset]);$('start').focus();toast('Replay preset ready. Start when you are ready.');}));
  on('scenario','change',()=>text('scenario-help',scenarioHelp[$('scenario').value]));
  on('claim','click',()=>{if(state.run?.owner==='awaiting_operator')act('claim');});
  on('restore','click',()=>{if(state.run?.owner==='human')act('operator',{action:'restore_session'});});
  on('retry','click',()=>{if(state.run?.owner==='human')act('operator',{action:'retry'});});
  on('resume','click',()=>{if(state.run?.owner==='human')act('resume');});
  for(const id of ['abort','cancel-run'])on(id,'click',()=>{if(state.run&&!terminal.has(state.run.status))act('abort');});
  on('refresh','click',poll);
  on('event-filter','change',()=>{if(state.run){state.eventsSignature='';state.renderedEvents=[];renderEvents(state.run.events);}});
  on('download-events','click',()=>{if(state.run)download(state.run.events.map(event=>JSON.stringify(event)).join('\n')+'\n','relay-'+state.run.id.slice(0,8)+'-events.jsonl','application/x-ndjson');});
  on('download-artifact','click',()=>{if(state.capability)download(JSON.stringify(state.capability,null,2)+'\n','relay-workflow.json','application/json');});
  on('copy-artifact','click',async()=>{if(!state.capability)return;try{await navigator.clipboard.writeText(JSON.stringify(state.capability,null,2));toast('Workflow JSON copied.');}catch{error('Clipboard access is unavailable. Download the workflow JSON instead.');}});
  document.querySelector('[data-nav="'+page+'"]').setAttribute('aria-current','page');
  document.addEventListener('visibilitychange',()=>{if(!document.hidden){refreshWorkspace();refreshCapability();if(state.runId&&!terminal.has(state.run?.status))poll();}});
  window.addEventListener('pagehide',()=>{clearTimeout(state.pollTimer);clearTimeout(state.workspaceTimer);});
  window.addEventListener('pageshow',event=>{if(event.persisted){state.loading=false;refreshWorkspace();refreshCapability();if(state.runId)poll();}});
  setMode(state.mode);updateControls();refreshWorkspace(true);
  refreshCapability();
})();
