/* DOM integration only: mocked APIs/media playback, no browser layout assertion. */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const {JSDOM, VirtualConsole} = require('jsdom');

const html = execFileSync('python3', ['-c', 'import studio; print(studio.page())'], {encoding: 'utf8'});
const errors = [], calls = [];
const consoleSink = new VirtualConsole();
consoleSink.on('jsdomError', error => {
  // jsdom does not implement all CSS used by the existing page.
  if (!String(error.message).includes('CSS')) errors.push(error.message);
});
const fixture = {id: 'px999001', title: '合成 DOM 测试夹具', status: 'review', revision: 1,
  source_url: 'https://www.pexels.com/video/999001/', tags: ['train'], places: [], issues: [],
  technical: {width: 640, height: 360, duration: 8, fps: 15, bytes: 1000}, description: '测试',
  frames: [], events: [], analysis: null, analysis_error: '模拟：模型未配置',
  candidate_file: 'output/media/candidates/px999001/source.mp4'};
const dom = new JSDOM(html, {
  url: 'http://localhost:18770/#assets', runScripts: 'dangerously', pretendToBeVisual: true,
  virtualConsole: consoleSink,
  beforeParse(window) {
    window.matchMedia = () => ({matches: false, addEventListener() {}});
    window.scrollTo = () => {};
    window.HTMLMediaElement.prototype.pause = function() {};
    window.HTMLMediaElement.prototype.play = function() { return Promise.resolve(); };
    window.HTMLMediaElement.prototype.load = function() {};
    window.HTMLDialogElement.prototype.showModal = function() { this.open = true; };
    window.HTMLDialogElement.prototype.close = function() { this.open = false; };
    window.fetch = async (url, options) => {
      if (options?.body) calls.push({url, body: JSON.parse(options.body)});
      let data = {};
      if (url === '/api/media/config') data = {pexels: false, planner: false, vision: false, ffmpeg: true};
      else if (url === '/api/media/tasks') data = {items: []};
      else if (String(url).startsWith('/api/media/list')) data = {
        items: [fixture], total: 1, query_basis: '模拟检索', query_terms: []};
      else if (String(url).startsWith('/api/media/detail')) data = fixture;
      else if (url === '/api/media/clips') data = {clips: []};
      else if (url === '/api/script-library') data = {sessions: [], vocabulary: {tags: [], places: []}};
      else if (url === '/api/video-trends') data = {items: [], errors: [], platforms: []};
      else if (url === '/api/topic-feed') data = {items: [], recent: [], history: []};
      return {ok: true, json: async () => data};
    };
  }
});
const settle = () => new Promise(resolve => setTimeout(resolve, 50));
(async () => {
  try {
    const w = dom.window, d = w.document;
    await settle();
    assert.deepEqual(errors, []);
    assert.equal(d.querySelector('#mlPanel').hidden, false);
    assert.equal(d.querySelector('#mlSearchGo').disabled, true);
    w.mlTab('pending');
    await settle();
    assert.equal(d.querySelectorAll('#mlResults .ml-card').length, 1);
    await w.mlOpen(fixture.id);
    assert.equal(d.querySelector('#mlDialog').open, true);
    assert.match(d.querySelector('#mlDialog').textContent, /模型未配置/);
    d.querySelector('#mlActor').value = 'DOM 测试';
    d.querySelector('#mlNote').value = '合成测试记录，不代表真实审批';
    d.querySelector('#mlRightsNote').value = '仅测试夹具';
    d.querySelector('#mlWatched').checked = true;
    d.querySelector('#mlRights').checked = true;
    const button = d.querySelector('[data-ml-decision="approve"]');
    d.querySelector('#mlDecisionForm').dispatchEvent(new w.SubmitEvent('submit', {
      bubbles: true, cancelable: true, submitter: button}));
    await settle();
    const call = calls.find(item => item.url === '/api/media/decide');
    assert.equal(call.body.action, 'approve');
    assert.equal(call.body.revision, 1);
    assert.equal(call.body.watched, true);
    assert.equal(call.body.rights_confirmed, true);
    assert.deepEqual(call.body.tags, ['train']);
    assert.equal(d.querySelector('#mlDialog').open, false);
    assert.deepEqual(errors, []);
    console.log('PASS: startup, unavailable-key state, pending list, detail and approval request');
    console.log('Scope: jsdom + synthetic APIs; visual browser and real external APIs not tested.');
  } finally { dom.window.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
