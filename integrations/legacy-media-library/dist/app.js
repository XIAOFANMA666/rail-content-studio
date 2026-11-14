const $ = (id) => document.getElementById(id);
const labels = {queued:'排队中',processing:'处理中',ready:'处理完成',failed:'处理失败',pending:'待审核',hold:'待补证',rejected:'已拒绝',approved:'已入库'};
let assets = [], tab = 'home', category = '', snapshot = '';
const el = (name, text, className) => {
  const node = document.createElement(name);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
};
function button(text, click, style) {
  const node = el('button',text,style); node.type = 'button'; node.onclick = click; return node;
}
async function request(path, payload) {
  const options = payload instanceof FormData ? {method:'POST',body:payload} :
    payload ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)} : {};
  const response = await fetch(path,options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error);
  return body;
}
function notice(message) { $('notice').textContent = message; }
async function refresh() {
  try {
    const next = await request('/api/library');
    const serialized = JSON.stringify(next);
    if (serialized !== snapshot) { assets = next; snapshot = serialized; render(); }
  }
  catch (error) { notice('读取失败：'+error.message); }
}
let batches = [], selectedBatch = null, scope = null, webContext = null, currentJob = null;
const pages = ['home','import','library','web','review'];
function go(next, filter=null) {
  tab=next;
  scope=next==='review'?filter:null;
  if (next==='review') {
    $('review-filter').value='';
    $('review-source').value='';
  }
  for(const page of pages) $(page).hidden=page!==next;
  $('task-nav').hidden=next==='home';
  for(const item of $('task-nav').querySelectorAll('button')) item.classList.toggle('active',item.dataset.go===next);
  notice('');
  render();
  if(next==='import') refreshBatches();
  window.scrollTo(0,0);
}
for (const item of document.querySelectorAll('[data-go]')) item.onclick=()=>{
  if(item.closest('#home') && item.dataset.go==='web' && webContext) {
    webContext=null;
    $('commons-form').reset();
    $('agent-status').hidden=true;
    $('web-context').replaceChildren();
    $('web-message').textContent='';
  }
  go(item.dataset.go);
};
try { $('upload').elements.uploader.value=localStorage.getItem('media-uploader')||''; } catch {}
function jobCandidates(job) {
  const ids=job.result.pageIds;
  return commonsData.candidates.filter(c=>c.job_id===job.id || ids?.includes(c.page_id));
}
function scopedAsset(id) {
  if (!scope) return true;
  return scope.ids.has(id) || (scope.job && jobCandidates(scope.job).some(c=>c.asset===id));
}
$('upload').onsubmit = async (event) => {
  event.preventDefault();
  const form = event.currentTarget, submit = form.querySelector('button');
  submit.disabled = true;
  $('upload-message').textContent = '正在上传…';
  try {
    const result = await request('/api/upload?batch=1',new FormData(form));
    selectedBatch = result.id;
    await refreshBatches();
    try { localStorage.setItem('media-uploader',form.elements.uploader.value); } catch {}
    $('upload-message').textContent = '已收到本批文件，处理进度见下方。';
    renderBatch();
    form.elements.files.value = '';
    await refresh();
  } catch (error) { $('upload-message').textContent = error.message; }
  finally { submit.disabled = false; }
};
const categories = (p) => [...new Set([p.location,p.line,...p.tags].filter(Boolean))];
const media = (asset,name) => '/media/'+asset.id+'/'+name;
const span = (part) => part.end ? part.start.toFixed(2)+'–'+part.end.toFixed(2)+' 秒' : '图片';
function fill(container, items, empty) {
  container.replaceChildren(...(items.length ? items : [el('p',empty,'empty')]));
}
function card(asset, segment) {
  const p = segment.payload, box = el('article',null,'card');
  const poster = el('img'); poster.src = media(asset,p.poster); poster.alt = p.description; poster.loading = 'lazy';
  const tags = el('div',null,'tags');
  for (const t of categories(p)) tags.append(el('span',t));
  box.append(poster,el('h3',asset.name),el('span',labels[segment.state]+' · '+span(p),'badge'),el('p',p.description),tags,
    ...(commonsData.candidates.some(c=>c.asset===asset.id)?[el('span','Commons','badge')]:[]),
    button(segment.state === 'approved' ? '查看素材与来源' : '打开审核',()=>detail(asset,segment)));
  return box;
}
async function refreshBatches() {
  try { batches=await request('/api/import-batches'); if(tab==='import') renderBatch(); }
  catch(err) { if(tab==='import') notice('读取批次失败：'+err.message); }
}
function renderBatch() {
  const current=batches.find(b=>b.id===selectedBatch)||batches[0];
  $('batch-history').replaceChildren(...batches.map(b=>button(
    '#'+b.id+' · '+b.items.length+' 项 · '+(b.origin||'未填写来源')+' · '+b.created+' UTC',
    ()=>{selectedBatch=b.id;renderBatch();}, b===current?'primary':''
  )));
  const batch=current?.items||[];
  const box=$('batch-result');
  if (!batch.length) return;
  const duplicates=batch.filter(x=>x.duplicate).length;
  const failed=batch.filter(x=>x.error || assets.find(a=>a.id===x.id)?.status==='failed').length;
  const ready=batch.filter(x=>assets.find(a=>a.id===x.id)?.status==='ready').length;
  const ids=new Set(batch.map(x=>x.id).filter(Boolean));
  const details=el('details',null,'panel');
  details.open=failed>0;
  details.append(el('summary','查看本批明细'));
  for(const item of batch) {
    const asset=assets.find(a=>a.id===item.id), row=el('div',null,'job');
    row.append(el('strong',item.name),el('span',item.error||asset?.error||(item.duplicate?'重复，已补记来源':labels[asset?.status]||'已排队')));
    if(asset?.status==='failed') row.append(button('重试',async ()=>{
      try { await request('/api/retry',{id:asset.id}); await refresh(); }
      catch(err) { notice(err.message); }
    }));
    details.append(row);
  }
  box.replaceChildren(el('p','批次 #'+current.id+' · '+batch.length+' 项 · 处理完成 '+ready+' 项 · 重复 '+duplicates+' 项 · 失败 '+failed+' 项'),
    button('审核这批素材',()=>go('review',{title:'导入批次 #'+current.id,ids,leads:new Set(),back:'import'}),'primary'),details);
  box.querySelector('button').disabled=!ready;
}
function reviewItems() {
  const state=$('review-filter').value, source=$('review-source').value;
  const webAssets=new Set(commonsData.candidates.map(c=>c.asset));
  return assets.filter(a=>scopedAsset(a.id)&&(!source||webAssets.has(a.id))).flatMap(a=>
    a.segments.filter(s=>s.state!=='approved'&&(!state||s.state===state)).map(s=>[a,s]));
}
function render() {
  const all = assets.flatMap(a=>a.segments.map(s=>[a,s]));
  const pending = all.filter(([,s])=>s.state !== 'approved');
  renderCommons();
  $('count').textContent = pending.filter(([,s])=>s.state !== 'rejected').length +
    commonsData.candidates.filter(c=>!c.asset&&c.state==='hold').length;
  if (tab==='import') renderBatch();
  if (tab === 'import') fill($('jobs'),assets.map(a=>{
    const row = el('div',null,'job');
    row.append(el('strong',a.name),el('span',labels[a.status],'badge'),el('span',a.segments.length+' 段 · '+a.sources.length+' 条来源'));
    if (a.error) row.append(el('span',a.error,'error'));
    if (a.status === 'failed') row.append(button('重试',async ()=>{
      try { await request('/api/retry',{id:a.id}); await refresh(); }
      catch (error) { notice(error.message); }
    }));
    return row;
  }),'上传后可在这里查看处理进度。');
  if (tab === 'review') {
    const bar=$('review-scope');
    bar.replaceChildren();
    if(scope) bar.append(el('strong',scope.title),button('查看全部审核',()=>go('review')),
      button('返回任务',()=>go(scope.back)));
    if(scope?.job) bar.append(searchAgainButton(scope.job));
    fill($('queue'),reviewItems().map(([a,s])=>card(a,s)),'当前范围没有待审文件。网页线索在下方。');
  }
  if (tab === 'library') {
    const approved = all.filter(([,s])=>s.state === 'approved');
    const tags = [...new Set(approved.flatMap(([,s])=>categories(s.payload)))].sort();
    if (!tags.includes(category)) category = '';
    $('categories').replaceChildren(...['',...tags].map(t=>button(t||'全部分类',()=>{category=t;render();},category===t?'active':'')));
    const query = $('query').value.trim().toLowerCase();
    fill($('results'),approved.filter(([a,s])=>{
      const tags = categories(s.payload);
      return (!category||tags.includes(category)) && [a.name,s.payload.description,...tags].join(' ').toLowerCase().includes(query);
    }).map(([a,s])=>card(a,s)),'审核通过的素材会出现在这里。');
  }
}
$('query').oninput = render;
$('review-filter').onchange = render;
$('review-source').onchange = render;
let lastReviewer='';
function detail(asset,segment) {
  const p = segment.payload, box = $('content'), form = el('form');
  const webSources = commonsData.candidates.filter(c=>c.asset===asset.id);
  const web = webSources.length===1 ? webSources[0].payload : null;
  const title = el('div',null,'toolbar');
  title.append(el('h2',asset.name),button('关闭',()=>$('detail').close()));
  const viewer = el(p.end?'video':'img',null,'viewer');
  if (p.end) {
    viewer.controls = true; viewer.preload = 'metadata';
    viewer.src = media(asset,'original'+asset.extension)+'#t='+p.start+','+p.end;
    viewer.poster = media(asset,p.poster);
  } else viewer.src = media(asset,p.poster);
  const original = el('a','打开原文件'); original.href = media(asset,'original'+asset.extension); original.target='_blank'; original.rel='noopener';
  box.replaceChildren(title,el('p',labels[segment.state]+' · '+span(p)+' · '+asset.info.width+' × '+asset.info.height),
    viewer,original,el('h3','导入来源'),
    el('p',p.authorizationVerified ? '授权已人工核验' : '授权待核实 · 原作者：'+(p.author||'未知')+' · 原始发布页：'+(p.originalUrl||'未知')),
    el('div',asset.sources.map(s=>[s.name,s.origin||'来源未填写',s.uploader||'导入人未填写',s.created+' UTC'].join(' · ')).join('\n'),'detail-sources'),
    el('p',(p.risks||[]).join('；') || '代表帧无自动风险提示，仍需查看完整素材。'),form);
  const evidence=el('details',null,'panel');
  evidence.append(el('summary','查看 Commons 来源证据'));
  for (const source of webSources) evidence.append(commonsEvidence(source));
  if(webSources.length) form.before(evidence);
  const fields = el('div',null,'fields');
  function field(name,label,value,type='text') {
    const wrapper = el('label',label);
    const control = el(type==='textarea'?'textarea':'input');
    if (type!=='textarea') control.type=type;
    control.name=name; control.value=value??'';
    if(type==='number') control.step='any';
    wrapper.append(control); fields.append(wrapper);
    return control;
  }
  field('description','画面描述',p.description,'textarea');
  field('tags','标签（用逗号分隔）',p.tags.join('，'));
  const extras=el('details',null,'panel'), extraFields=el('div',null,'fields');
  extras.append(el('summary','地点、来源与授权'),extraFields);
  function extra(name,label,value,type) {
    const control=field(name,label,value,type);
    extraFields.append(control.parentElement);
    return control;
  }
  extra('location','已核实地点',p.location);
  extra('locationEvidence','地点核验依据',p.locationEvidence);
  extra('line','线路',p.line);
  extra('author','原作者',p.author || web?.author);
  extra('originalUrl','发布页或授权证据页',p.originalUrl || web?.discovery);
  extra('license','授权凭据或说明',p.license || (web ? [web.license,web.licenseUrl].filter(Boolean).join(' · ') : ''));
  extra('usage','允许使用的平台、地区或用途',p.usage);
  extra('riskNotes','人工风险备注',p.riskNotes);
  const start=extra('start','起点（秒）',p.start,'number'), end=extra('end','终点（秒）',p.end,'number');
  if (!p.end) start.parentElement.hidden=end.parentElement.hidden=true;
  field('reviewer','审核人',segment.reviewer||lastReviewer);
  field('note','审核理由 / 待补证说明',segment.note);
  const confirm = el('label'), checkbox = el('input');
  checkbox.type='checkbox'; checkbox.checked=p.authorizationVerified===true;
  confirm.append(checkbox,' 我已人工核对授权，素材可在所填范围内使用');
  const publication = el('label'), publishCheck = el('input');
  publishCheck.type='checkbox'; publishCheck.checked=p.publicationApproved===true;
  publication.append(publishCheck,' 我已核对对外发布权限，允许在上述范围内发布');
  const status = el('p',null,'error'); status.setAttribute('role','status');
  const actions = el('div',null,'actions');
  for (const [state,name] of [['approved','通过入库'],['hold','待补证'],['rejected','拒绝'],['pending','保存为待审']]) {
    actions.append(button(name,async ()=>{
      const data=Object.fromEntries(new FormData(form));
      data.tags=data.tags.split(/[,，]/);
      Object.assign(data,{id:segment.id,state,authorizationVerified:checkbox.checked,publicationApproved:publishCheck.checked});
      for (const b of actions.children) b.disabled=true;
      const items=tab==='review'&&scope ? reviewItems().filter(([,s])=>s.state==='pending') : [];
      const index=items.findIndex(([,s])=>s.id===segment.id);
      const next=items[index+1] || (index>0 ? items[0] : null);
      try {
        await request('/api/review',data);
        if (data.reviewer) lastReviewer=data.reviewer;
        await refresh();
        notice('已保存：'+labels[state]);
        if (next && state!=='pending') detail(...next);
        else $('detail').close();
        if (lastSearch) runSearch(lastSearch);
      } catch (error) {
        status.textContent=error.message;
        if (/授权|地点|片段范围/.test(error.message)) extras.open=true;
      }
      finally { for (const b of actions.children) b.disabled=false; }
    },state==='approved'?'primary':''));
  }
  form.onsubmit = (event)=>event.preventDefault();
  extras.append(confirm,publication);
  form.append(fields,extras,status,actions);
  if (!$('detail').open) $('detail').showModal();
}
$('detail').addEventListener('close',()=>$('content').replaceChildren());
refresh();
setInterval(refresh,1800);

let lastSearch = null, searchVersion = 0;
const searchForm = $('shot-search');
searchForm.onsubmit = event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(searchForm));
  values.infer = searchForm.elements.infer.checked;
  lastSearch = values;
  runSearch(values);
};
async function runSearch(values) {
  const version = ++searchVersion, out = $('shot-results');
  out.replaceChildren(el('p','正在核对条件和素材…'));
  try {
    const result = await request('/api/search',values);
    if (version !== searchVersion) return;
    out.replaceChildren(
      el('p','必需条件：'+result.rules.join(' · ')),
      el('p','画面偏好：'+(result.preferences.join(' · ')||'按脚本文字查找')),
      el('h3','推荐镜头 · '+result.totalMatches)
    );
    if (!result.matches.length) {
      out.append(el('p','素材缺口：'+result.gaps.join('；'),'empty'));
      const actions=el('div',null,'actions');
      if (result.excluded.some(x=>x.reasons.includes('待补证')||x.reasons.some(r=>/未获对外发布|缺少已核验的使用授权/.test(r)))) {
        const ids=new Set(result.excluded.filter(x=>x.reasons.some(r=>/待补证|未获对外发布|缺少已核验的使用授权/.test(r))).map(x=>x.assetId));
        actions.append(button('去核对授权',()=>openAuthorization(ids)));
      }
      actions.append(button('网上找这个镜头',()=>prepareWeb(values,result),'primary'));
      out.append(actions);
    }
    const grid = el('div',null,'grid');
    for (const match of result.matches) {
      const asset = assets.find(a=>a.id===match.assetId);
      const segment = asset?.segments.find(s=>s.id===match.id);
      const box = asset && segment ? card(asset,segment) : el('article',match.name,'card');
      box.append(el('p','推荐理由：'+match.reasons.join('；')));
      if (segment?.payload.usage || segment?.payload.locationEvidence) {
        const info=el('details'); info.append(el('summary','使用范围与地点依据'));
        if(segment.payload.usage) info.append(el('p','使用范围：'+segment.payload.usage));
        if(segment.payload.locationEvidence) info.append(el('p','地点依据：'+segment.payload.locationEvidence));
        box.append(info);
      }
      if (match.missing.length) {
        box.append(el('p','尚未确认：'+match.missing.join('、')));
        for (const feature of match.missing) box.append(button('设为必需：'+feature,()=>{
          searchForm.elements.required.value=[values.required,feature].filter(Boolean).join('，');
          $('search-options').open=true;
          searchForm.requestSubmit();
        }));
      }
      grid.append(box);
    }
    out.append(grid);
    const exclusions = el('details',null,'exclusions');
    exclusions.open = !result.matches.length;
    exclusions.append(el('summary','排除原因 · '+result.totalExcluded));
    if (!result.excluded.length) exclusions.append(el('p','没有相关排除记录。'));
    const list = el('ul');
    for (const item of result.excluded) list.append(el('li',item.name+'：'+item.reasons.join('；')));
    exclusions.append(list);
    if (result.totalExcluded > result.excluded.length) exclusions.append(el('p','仅显示前 20 条。'));
    out.append(exclusions);
  } catch (error) {
    if (version === searchVersion) out.replaceChildren(el('p',error.message,'error'));
  }
}

let commonsData = {jobs:[],candidates:[]}, commonsSnapshot='';
function openAuthorization(ids) {
  const pending=assets.filter(a=>ids.has(a.id)&&a.segments.some(s=>s.state==='hold'));
  if(pending.length) return go('review',{title:'需核对授权的素材',ids,leads:new Set(),back:'library'});
  const asset=assets.find(a=>ids.has(a.id));
  const segment=asset?.segments.find(s=>s.state==='approved') || asset?.segments[0];
  if(segment) detail(asset,segment);
}
function prepareWeb(values,result) {
  webContext=values;
  const form=$('commons-form');
  form.elements.script.value=values.script;
  form.elements.purpose.value=values.purpose;
  form.elements.seconds.value=values.seconds||'';
  form.elements.videoOnly.checked=Number(values.seconds)>0;
  const context=$('web-context');
  context.replaceChildren(el('p','已带入条件：'+result.rules.join(' · ')),
    el('p','要补的缺口：'+result.gaps.join('；')),
    button('返回修改条件',()=>go('library')),
    button('改为独立采集',()=>{webContext=null;context.replaceChildren();}));
  $('web-message').textContent='';
  go('web');
}
$('commons-form').elements.mode.onchange=async event=>{
  const status=$('agent-status');
  status.hidden=event.currentTarget.value!=='agent';
  if(status.hidden) return;
  status.textContent='正在检查本机配置…';
  try {
    const info=await request('/api/commons/agent-status');
    status.textContent=info.configured ? '已配置 '+info.provider+' · '+info.model :
      info.message+'。当前可用固定查询回退。';
  } catch(err) { status.textContent=err.message; }
};
$('commons-form').onsubmit=async event=>{
  event.preventDefault();
  const form=event.currentTarget, submit=form.querySelector('button.primary');
  const values=Object.fromEntries(new FormData(form));
  values.videoOnly=form.elements.videoOnly.checked || Number(values.seconds)>0;
  submit.disabled=true;
  $('web-message').textContent='正在建立任务…';
  try {
    const result=await request('/api/commons/start',{...webContext,...values,direct:!webContext});
    currentJob=result.id;
    $('web-message').textContent='已创建采集任务 #'+result.id;
    await refreshCommons();
    renderCommons();
  } catch(err) { $('web-message').textContent=err.message; }
  finally { submit.disabled=false; }
};
$('video-link-form').onsubmit=async event=>{
  event.preventDefault();
  const form=event.currentTarget, submit=form.querySelector('button');
  submit.disabled=true;
  try {
    const result=await request('/api/commons/submit-link',{...Object.fromEntries(new FormData(form)),job:currentJob});
    currentJob=result.id;
    form.reset();
    $('video-link-message').textContent='已提交，点击下方「审核本次候选」查看。';
    await refreshCommons();
    renderCommons();
  } catch(err) { $('video-link-message').textContent=err.message; }
  finally { submit.disabled=false; }
};
let jobsHistoryOpen=false;
const jobLabels={queued:'等待搜索',running:'正在搜索与采集',done:'已完成',partial:'部分完成',failed:'采集失败'};
async function refreshCommons() {
  try {
    const next=await request('/api/commons');
    const snapshot=JSON.stringify(next);
    if (snapshot!==commonsSnapshot) {
      commonsSnapshot=snapshot; commonsData=next; render();
    }
  } catch (error) { if (tab==='library') notice('读取采集任务失败：'+error.message); }
}
function commonsEvidence(candidate) {
  const p=candidate.payload, block=el('section',null,'panel');
  const source=el('a','打开来源页'); source.href=p.discovery;
  source.target='_blank'; source.rel='noopener noreferrer';
  block.append(el('strong',(p.provider||'Wikimedia Commons')+' · 页面声明待人工核对'),el('p',p.title),source);
  const values=[
    ['发现时间',p.fetchedAt],['搜索词',p.query],['原脚本',candidate.job.script],
    ['任务条件',candidate.job.rules.join(' · ')],['任务缺口',candidate.job.gaps.join('；')],
    ['网页描述',p.description],['网页署名',p.author],['上传者',p.uploader],['网页所列来源',p.originalClaim],
    ['网页许可证',p.license],['许可证链接',p.licenseUrl],['限制',p.restrictions],['署名说明',p.attribution]
  ];
  for(const [label,value] of values) block.append(el('p',label+'：'+(value||'未提供')));
  if(candidate.note) block.append(el('p',candidate.note));
  return block;
}
function searchAgainButton(job) {
  const values=job.payload.search;
  return button(values?'重跑原搜索':'打开历史脚本',()=>{
    searchForm.reset();
    const input=values||{script:job.payload.script,purpose:job.payload.purpose};
    for(const [key,value] of Object.entries(input)) {
      const field=searchForm.elements.namedItem(key);
      if(!field) continue;
      if(field.type==='checkbox') field.checked=value===true;
      else field.value=value;
    }
    $('search-options').open=true;
    go('library');
    if(values) searchForm.requestSubmit();
    else {
      lastSearch=null;
      searchVersion++;
      $('shot-results').replaceChildren();
      notice('早期任务未保存完整表单，请核对「更多条件」后搜索。原任务条件：'+job.payload.rules.join(' · '));
    }
  });
}
function renderCommons() {
  const jobs=$('commons-jobs'), leads=$('commons-leads');
  if (tab==='web') {
    jobs.replaceChildren();
    const active=commonsData.jobs.find(j=>j.id===currentJob)||commonsData.jobs[0];
    const openJob=(job)=>{
      const candidates=jobCandidates(job);
      go('review',{title:'采集任务 #'+job.id,ids:new Set(candidates.map(c=>c.asset).filter(Boolean)),
        leads:new Set(candidates.map(c=>c.page_id)),job,back:'web'});
    };
    function row(job,featured) {
      const item=el(featured?'section':'details',null,'panel'), r=job.result;
      item.append(el(featured?'h3':'summary','#'+job.id+' · '+jobLabels[job.state]+' · '+job.payload.script));
      const info=el('details'); info.append(el('summary','查询与筛选条件'),
        el('p',job.payload.retryPage?'重试下载候选 #'+job.payload.retryPage:'查询：'+job.payload.queries.join(' / ')),el('p','条件：'+job.payload.rules.join(' · ')));
      item.append(info,searchAgainButton(job));
      if(job.payload.mode==='agent' && !job.payload.retryPage) {
        const gaps=r.remainingGaps||[], verification=r.verification||[];
        if(r.stopReason && r.remainingGaps) {
          const summary=[r.found?'保留 '+r.found+' 条候选':'暂无候选',
            gaps.length?gaps.join('；'):r.found?'等待人工核验':'',
            r.stopReason.includes('限流')?'Commons 暂时不可用，可稍后重新采集':''].filter(Boolean);
          item.append(el('p',summary.join(' · ')));
        }
        const trace=el('details'), list=el('ol');
        trace.append(el('summary','Agent 搜索过程'));
        for(const round of r.rounds||[]) {
          const label=round.action==='version'?'检查视频版本 #'+round.pageId:round.query;
          list.append(el('li',[label,round.reason,round.result].filter(Boolean).join(' · ')));
        }
        trace.append(list);
        if(verification.length) trace.append(el('p','待核验 · '+verification.join('；')));
        if(r.excluded?.length) {
          const rejected=el('ul');
          for(const candidate of r.excluded) rejected.append(el('li',candidate.title+' · '+candidate.reason));
          trace.append(el('p','本次排除 '+r.excluded.length+' 条'),rejected);
        }
        if(r.stopReason) trace.append(el('p','结束原因 · '+r.stopReason));
        item.append(trace);
      }
      if(r.supplemental?.complete) {
        const extra=r.supplemental;
        item.append(el('p',extra.provider+' 补查 · '+(extra.error?'查询失败 '+extra.error:
          '搜索 '+extra.query+' · 返回 '+extra.returned+' 条 · 新增 '+extra.added+' 条候选')));
      }
      if(job.payload.videoOnly && ['done','partial','failed'].includes(job.state) && !r.found)
        item.append(el('p','没有匹配的视频'));
      for(const candidate of jobCandidates(job)) {
        if(candidate.payload.mime!=='video/webm' || !candidate.asset) continue;
        const asset=assets.find(a=>a.id===candidate.asset);
        if(asset && asset.status!=='ready') {
          item.append(el('p',asset.name+' · '+(asset.status==='failed'?asset.error:'正在准备 MP4 预览')));
          if(asset.status==='failed') item.append(button('重试视频处理',async ()=>{
            try { await request('/api/retry',{id:asset.id}); await refresh(); }
            catch(err) { notice(err.message); }
          }));
        }
      }
      if(r.found!=null) item.append(el('p','找到 '+r.found+' 条 · 下载送处理 '+r.downloaded+' 条 · 待补证线索 '+r.lead+' 条 · 重复 '+r.duplicate+' 条'));
      if(r.errors?.length) item.append(el('p',r.errors.join('；'),'error'));
      if(jobCandidates(job).length) item.append(button('审核本次候选',()=>openJob(job),'primary'));
      else if(r.duplicate && !r.pageIds) item.append(el('p','这条历史任务仅记录重复数量。原候选可在最初的任务中查看。'));
      return item;
    }
    if(active) jobs.append(row(active,true));
    const older=commonsData.jobs.filter(j=>j!==active);
    if(older.length) {
      const history=el('details',null,'panel'); history.open=jobsHistoryOpen;
      history.append(el('summary','历史任务 · '+older.length));
      history.ontoggle=()=>{if(history.isConnected) jobsHistoryOpen=history.open;};
      for(const job of older) history.append(row(job,false));
      jobs.append(history);
    }
  }
  if (tab!=='review') return;
  const state=$('review-filter').value;
  const items=commonsData.candidates.filter(c=>!c.asset && (!scope||scope.leads.has(c.page_id)) && (!state || c.state===state));
  leads.replaceChildren();
  if(items.length) leads.append(el('h3','网页素材线索'));
  for (const item of items) {
    const panel=el('details',null,'panel');
    panel.append(el('summary',labels[item.state]+' · '+item.payload.title),commonsEvidence(item));
    const note=el('input'); note.value=item.note; note.setAttribute('aria-label','处理理由');
    const files=el('select'); files.setAttribute('aria-label','已核对的本地文件');
    const blank=el('option','请选择已核对的对应文件'); blank.value=''; files.append(blank);
    for(const asset of assets.filter(a=>a.status==='ready')) {
      const option=el('option',asset.name); option.value=asset.id; files.append(option);
    }
    const message=el('p',null,'error');
    async function save(path,data) {
      try {
        await request('/api/commons/'+path,{id:item.page_id,...data});
        await refreshCommons(); notice('已保存 Commons 审核记录');
      } catch(err) { message.textContent=err.message; }
    }
    panel.append(note,button('待补证',()=>save('lead',{state:'hold',note:note.value})),
      button('拒绝',()=>save('lead',{state:'rejected',note:note.value})),files,
      button('关联已核对文件',()=>save('link',{asset:files.value})),message);
    if(item.state==='hold' && item.note.startsWith('下载失败：')) {
      const running=commonsData.jobs.some(j=>j.payload.retryPage===item.page_id&&['queued','running'].includes(j.state));
      const retry=button(running?'正在重试':'重试下载',async ()=>{
        retry.disabled=true;
        try { await request('/api/commons/retry',{id:item.page_id}); await refreshCommons(); }
        catch(err) { message.textContent=err.message; retry.disabled=false; }
      });
      retry.disabled=running;
      panel.append(retry);
    }
    leads.append(panel);
  }
}
refreshCommons();
setInterval(refreshCommons,2200);
