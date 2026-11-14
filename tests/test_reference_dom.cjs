/* Synthetic DOM flow; does not claim browser visual or real model validation. */
const assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');
const {JSDOM,VirtualConsole}=require('jsdom');
const html=execFileSync('.venv/bin/python',['-c','import studio; print(studio.page())'],{encoding:'utf8'});
const calls=[],errors=[];
const configFixture={ffmpeg:true,ffprobe:true,vision:true,search:true,skill:true,image_search:true};
const fixture={id:'a'.repeat(32),name:'fixture.mp4',status:'awaiting_materials',stage:'合成缺口状态',busy:false,
 reference_url:'/reference-files/'+'a'.repeat(32)+'/reference.mp4',matches:{},gaps:[{shot_id:'s01',reason:'合成缺口'}],
 analysis:{shots:[{id:'s01',start_frame:0,end_frame:30,subject:'<img src=x onerror=alert(1)>',shot_size:'中景',action:'驶过',direction:'向右',camera:'固定'}],text_events:[]},technical:{fps:30},differences:['字体未确认'],
 legacy_search_tasks:{s01:{task_id:11,mode:'agent'},s02:{task_id:12,mode:'fixed'}},
 material_search:{state:'waiting_retry',reason:'来源暂时限流',next_retry_at:'2026-11-14T12:00:00Z',retry_reason:'保留现有结果后重试',attempts:1},
 legacy_jobs:[{shot_id:'s01',id:11,state:'running',candidates:4,reason:'',attempts:1,search_rounds:2,agent_planning_calls:1,next_retry_at:1794657600,retry_reason:'调整搜索词',candidate_items:[
  {id:'lead1',title:'仅网页线索',kind:'web',state:'web_lead',source_url:'https://example.com/reference',downloaded:false,approved:false},
  {id:'video1',title:'下载完成待审',kind:'video',state:'pending_review',asset_id:'asset-fixture-1',source_url:'https://example.com/video',downloaded:true,approved:false},
  {id:'video2',title:'已审核视频',kind:'video',state:'approved',asset_id:'asset-fixture-2',source_url:'https://example.com/approved',downloaded:true,approved:true},
  {id:'image1',title:'<img src=x onerror=alert(3)>',kind:'image',state:'downloaded',source_url:'javascript:alert(3)',downloaded:true,approved:false}
 ]},
  {shot_id:'s02',id:12,state:'failed',candidates:0,reason:'未找到匹配素材',errors:['<img src=x onerror=alert(2)>']}]};
const sink=new VirtualConsole();sink.on('jsdomError',e=>{if(!e.message.includes('CSS'))errors.push(e.message)});
const dom=new JSDOM(html,{url:'http://localhost:18773/#reference',runScripts:'dangerously',pretendToBeVisual:true,virtualConsole:sink,beforeParse(w){
 w.matchMedia=()=>({matches:false,addEventListener(){}});w.scrollTo=()=>{};
 w.HTMLMediaElement.prototype.pause=function(){};w.HTMLMediaElement.prototype.load=function(){};
 w.HTMLMediaElement.prototype.play=()=>Promise.resolve();w.HTMLDialogElement.prototype.showModal=function(){this.open=true};
 w.fetch=async(url,options)=>{calls.push({url,options});let data={};
  if(url==='/api/reference/config')data=configFixture;
  if(url==='/api/reference/jobs')data={items:[fixture]};
  if(String(url).startsWith('/api/reference/job?'))data=fixture;
  if(url==='/api/reference/import')data=fixture;
  if(url==='/api/reference/resume')data={...fixture,status:'matching',busy:true};
  if(url==='/api/media/config')data={pexels:false,vision:false,ffmpeg:true};
  return {ok:true,json:async()=>data};};
}});
const settle=()=>new Promise(r=>setTimeout(r,40));
(async()=>{try{const w=dom.window,d=w.document;await settle();assert.deepEqual(errors,[]);
 assert.equal(d.querySelector('#referenceView').hidden,false);
 assert.match(d.querySelector('#refConfig').textContent,/已配置/);
 assert.match(d.querySelector('#refImageSearch').textContent,/图片检索模块可用/);
 assert.match(d.querySelector('#refFlow').textContent,/分镜解析/);
 assert.equal(d.querySelector('#refStepRender').textContent,'未开始');
 const file=new w.File(['synthetic-video'],'fixture.mp4',{type:'video/mp4'});
 Object.defineProperty(d.querySelector('#refFile'),'files',{value:[file]});
 d.querySelector('#refConsent').checked=true;
 d.querySelector('#refImportForm').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await settle();
 assert(calls.some(c=>c.url==='/api/reference/import'&&c.options.body===file));
 assert.match(d.querySelector('#refJob').textContent,/等待补库与审批/);
 assert.equal(d.querySelector('#refStepSearch').textContent,'搜索任务执行中');
 assert.equal(d.querySelector('#refStepRender').textContent,'未完成');
 assert.match(d.querySelector('#refLegacyJobs').textContent,/搜索 Agent/);
 assert.match(d.querySelector('#refLegacyJobs').textContent,/固定检索/);
 assert.match(d.querySelector('#refLegacyJobs').textContent,/搜索失败/);
 assert.match(d.querySelector('#refLegacyJobs').textContent,/未找到匹配素材/);
 assert.equal(d.querySelector('#refLegacyJobs a').href,'http://localhost:8765/');
 assert.equal(d.querySelectorAll('#refLegacyJobs .ref-candidate').length,4);
 const task=d.querySelector('#refLegacyJobs [data-shot="s01"]');
 assert.match(task.textContent,/4 条来源记录 · 3 条已下载 · 1 条已下载且来源授权审批通过/);
 assert.match(task.textContent,/这里只取得网页线索，尚不能作为剪辑素材/);
 assert.match(task.textContent,/待审核/);
 assert.match(task.textContent,/图片不能替代/);
 assert.match(task.textContent,/已尝试 1 次/);
 assert.equal(task.querySelector('.ref-search-activity').textContent,'已搜索2轮 · 搜索Agent调整1次');
 assert.match(task.textContent,/下次重试/);
 assert.match(task.textContent,/调整搜索词/);
 assert.match(d.querySelector('#refSearchSchedule').textContent,/等待重试/);
 assert.match(d.querySelector('#refSearchSchedule').textContent,/来源暂时限流/);
 assert.equal(d.querySelectorAll('#refLegacyJobs a[href^="javascript:"]').length,0);
 assert.equal(task.querySelector('a[href="https://example.com/video"]').rel,'noopener noreferrer');
 assert.equal(d.querySelectorAll('#refJob img').length,0);
 assert.equal(d.querySelector('#refEditorWrap').hidden,false);
 w.refRender({...fixture,legacy_jobs:[],legacy_search_tasks:{}});
 assert.match(d.querySelector('#refLegacyJobs').textContent,/当前未返回旧素材库搜索任务/);
 w.refRender({...fixture,legacy_jobs:[{shot_id:'s01',state:'done',candidates:0}]});
 assert.equal(d.querySelector('#refStepSearch').textContent,'任务已结束，未返回候选');
 assert.equal(d.querySelector('.ref-search-activity'),null);
 w.refRender({...fixture,legacy_jobs:[{shot_id:'s01',state:'done',candidates:0,search_rounds:0,agent_planning_calls:0}]});
 assert.equal(d.querySelector('.ref-search-activity').textContent,'已搜索0轮 · 搜索Agent调整0次（未调用改词模型）');
 w.refRender({...fixture,legacy_jobs:[{shot_id:'s01',state:'done',candidates:0,search_rounds:1}]});
 assert.equal(d.querySelector('.ref-search-activity').textContent,'已搜索1轮');
 w.refRender({...fixture,legacy_jobs:[{shot_id:'s01',state:'done',candidates:1,candidate_items:[{title:'网页线索',kind:'web',state:'web_lead',downloaded:false,approved:false}]}]});
 assert.equal(d.querySelector('#refStepSearch').textContent,'已有来源线索，尚未取得素材');
 assert.equal(d.querySelector('#refStepReview').textContent,'取得素材后审核');
 assert.match(d.querySelector('#refLegacyJobs').textContent,/0 条已下载 · 0 条已下载且来源授权审批通过/);
 w.refRender({...fixture,legacy_jobs:[{shot_id:'s01',state:'done',candidates:2}]});
 assert.equal(d.querySelector('#refStepSearch').textContent,'已返回候选记录，下载未确认');
 assert.match(d.querySelector('#refLegacyJobs').textContent,/不能确认素材已到手/);
 const reviewedCandidate={id:'content1',asset_id:'content-asset',title:'内容核对夹具',kind:'video',state:'pending_review',downloaded:true,approved:false};
 const renderContent=(status,phase,approved=false,scope='')=>w.refRender({...fixture,legacy_jobs:[{shot_id:'s01',state:'done',candidates:1,candidate_items:[{...reviewedCandidate,approved,content_review:status,content_phase:phase,content_rejection_scope:scope}]}],material_search:{content_reviews:{s01:{'content-asset':{status,reason:'合成画面核对依据'}}}}});
 renderContent('deferred','complete',false,'sampled_frames_only');
 assert.match(d.querySelector('.ref-content-review').textContent,/抽样待定位，需进一步定位可用片段/);
 assert.doesNotMatch(d.querySelector('.ref-content-review').textContent,/deferred/);
 assert.equal(d.querySelector('#refStepReview').textContent,'已下载，画面待进一步定位');
 renderContent('deferred','complete');
 assert.match(d.querySelector('.ref-content-review').textContent,/待进一步定位可用片段/);
 renderContent('deferred','budget_exhausted');
 assert.match(d.querySelector('.ref-content-review').textContent,/本轮画面检查预算已用完，候选保留/);
 assert.doesNotMatch(d.querySelector('.ref-content-review').textContent,/deferred|budget_exhausted/);
 assert.equal(d.querySelector('#refStepReview').textContent,'画面检查预算已用完，候选保留');
 renderContent('rejected','complete');
 assert.match(d.querySelector('.ref-content-review').textContent,/已检画面不符合分镜/);
 assert.equal(d.querySelector('#refStepReview').textContent,'已检画面不符，继续补搜');
 assert.match(d.querySelector('.ref-candidate').textContent,/来源授权审批：待审核/);
 renderContent('rejected','complete',false,'sampled_frames_only');
 assert.match(d.querySelector('.ref-content-review').textContent,/已抽样画面不符合分镜，未检查区间未下结论/);
 assert.doesNotMatch(d.querySelector('.ref-content-review').textContent,/sampled_frames_only/);
 renderContent('content_match','complete');
 assert.match(d.querySelector('.ref-content-review').textContent,/分镜画面初筛通过/);
 assert.equal(d.querySelector('#refStepReview').textContent,'画面初筛通过，待来源授权审批');
 renderContent('rejected','complete',true);
 assert.match(d.querySelector('.ref-candidate').textContent,/来源授权审批：已通过/);
 assert.equal(d.querySelector('#refStepReview').textContent,'已检画面不符，继续补搜');
 renderContent('error','failed');
 assert.equal(d.querySelector('#refStepReview').textContent,'画面核对受阻');
 configFixture.image_search=false;await w.refConfigLoad();
 assert.match(d.querySelector('#refImageSearch').textContent,/未发现图片检索模块/);
 delete configFixture.image_search;await w.refConfigLoad();
 assert.match(d.querySelector('#refImageSearch').textContent,/状态尚未返回/);
 w.refRender(fixture);
 d.querySelector('#refResume').click();await settle();
 assert(calls.some(c=>c.url==='/api/reference/resume'));
 assert.equal(d.querySelector('#refResume').disabled,true);
 assert.deepEqual(errors,[]);console.log('PASS: reference import, per-shot sources, deferred/budget content states, separate source approval, scheduling, resume and escaping (synthetic DOM)');
 }finally{dom.window.close()}})().catch(e=>{console.error(e);process.exitCode=1});
