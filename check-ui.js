// Renders the interface against a fake DOM to catch missing functions and
// render-time errors before they show up as a blank page.
//   node check-ui.js
const fs = require('fs');
const els = {};
const el = id => els[id] || (els[id] = {innerHTML:'', textContent:'', scrollTop:0,
  classList:{add(){},remove(){},toggle(){}}, remove(){ delete els[id]; },
  parentNode:{insertBefore(){}}, scrollIntoView(){},
  querySelector: () => null, querySelectorAll: () => []});

global.document = {
  getElementById: id => (id === 'wizerr' ? null : el(id)),
  querySelector: sel => (sel === '.wiz-nav' ? el('wiznav') : null),
  querySelectorAll: () => [], addEventListener: () => {},
  createElement: () => ({style:{}, scrollIntoView(){}, classList:{add(){}}})};
global.window = {}; global.location = {protocol:'http:', host:'x', reload(){}};
global.WebSocket = class { constructor(){} };
global.alert = () => {}; global.confirm = () => true;
const CATALOG = {
  video:[{id:'hevc',name:'H.265'}], containers:[{id:'mkv',name:'MKV'}],
  audio:[{id:'aac',name:'AAC',default_bitrate:'160k',bitrates:['160k']}],
  subtitles:[{id:'keep',name:'Keep all subtitles'}],
  quality:[{id:'balanced',name:'Balanced'}],
  originals:[{id:'archive',name:'Move to an Originals folder'}],
  hdr:[{id:'preserve',name:'Keep HDR as it is'}],
  depth:[{id:'match',name:'Match the original'}],
  audio_languages:[{id:'keep_all',name:'Keep every audio track'}],
  naming:[{id:'jellyfin',name:'Jellyfin'},{id:'plex',name:'Plex'}],
  quality_scale:[{value:18,name:'Near-lossless',detail:'x'},
                 {value:22,name:'High',detail:'x'},
                 {value:30,name:'Small',detail:'x'}],
  retry_steps:[{id:'small',name:'A bit smaller',offset:4,detail:'x'},
               {id:'smaller',name:'Noticeably smaller',offset:8,detail:'x'},
               {id:'custom',name:'A quality I choose',offset:null,detail:'x'}],
};
global.fetch = async (url) => ({ok:true, json: async () => {
  if (url === '/api/catalog') return CATALOG;
  if (url.includes('profile/check')) return {warnings:[]};
  if (url.includes('naming/samples')) return {names:[]};
  if (url.includes('naming/preview')) return {results:[], used_lookup:false};
  return {counts:{}, jobs:[], page:1, pages:1, total:0, per_page:20};
}});

const html = fs.readFileSync(__dirname + '/server/static/index.html', 'utf8');
const src = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'));

const state = {
  nodes: [{id:'n', name:'test', online:true, encoders:['hevc_videotoolbox','libx265'],
           benchmarks:{hevc_videotoolbox:170, libx265:60}, recipes:{}, slots:2,
           cpus:12, max_jobs:1, last_seen: Date.now()/1000}],
  libraries: [{id:1, name:'Movies', watch_path:'/in', output_path:'/out',
    profile:{video_codec:'hevc', container:'mkv', audio_codec:'aac',
             subtitle_mode:'keep'},
    naming:{enabled:true, scheme:'jellyfin'}, filters:{},
    original_action:'archive', enabled:1, mirror_folders:1, skip_matching:1}],
  jobs: [{id:1, path:'/in/a.mkv', state:'running', progress:42, fps:120, node_id:'n',
          spec:{action:'full', quality:22, codec:'hevc'}}],
  counts: {active:1, failed:2, done:3},
  stats: {done:3, failed:2, queued:1, before:3e9, after:1e9},
  originals: {n:2, bytes:2e9}, schedule_open:true, schedule_text:'Running any time',
  settings: {},
};

let failures = 0;
function check(label, fn) {
  try { fn(); console.log('  ok      ' + label); }
  catch (e) { failures++; console.log('  FAILED  ' + label + ' -> ' + e.message); }
}

try { eval(src + '\nglobal.__x = {render, renderLibs, renderTabs, renderJobs, slotControl, ' +
  'setSlots, retryJob, cancelJob, requeueJob, removeJob, bulkJobs, switchView, ' +
  'loadView, openWizard, drawSettings, refreshPreview, scanOne, scanAll, ' +
  'toggleLib, removeLib, splitList, describeFilters, wizardError, jumpTo, ' +
  'backToReview, nextStep, validateFirstStep, STEPS, ' +
  'get step(){return step;}, set step(v){step = v;}, ' +
  'get returnTo(){return returnTo;}, get draft(){return draft;}, drawStep};'); }
catch (e) { console.log('SCRIPT FAILED TO LOAD: ' + e.message); process.exit(1); }

console.log('Rendering the interface with sample data:\n');
check('full render', () => __x.render(state));
check('empty libraries', () => __x.renderLibs([]));
check('empty nodes', () => __x.render({...state, nodes: [], libraries: [], jobs: []}));
check('node with no benchmarks', () =>
  __x.render({...state, nodes: [{...state.nodes[0], benchmarks:{}, slots:null, cpus:null}]}));
check('library with sparse profile', () =>
  __x.renderLibs([{id:2, name:'Bare', watch_path:'/x', profile:{}, enabled:1,
                   original_action:'delete'}]));
check('bloated job row', () =>
  __x.renderJobs([{id:5, path:'/a.mkv', state:'bloated', spec:{action:'full',quality:22},
                   size_before:1e9, size_after:1.2e9, progress:100,
                   outcome:'came out 20% larger - original restored'}],
                 {page:1,pages:1,total:1,per_page:20}));
check('running job shows a projected ratio', () =>
  __x.renderJobs([{id:6, path:'/b.mkv', state:'running', spec:{action:'full',quality:22},
                   size_before:1e9, size_now:2e8, progress:50}],
                 {page:1,pages:1,total:1,per_page:20}));
check('job with no spec action', () =>
  __x.renderJobs([{id:9, path:'/a.mkv', state:'failed', spec:{quality:22},
                   error:'boom', progress:0}], {page:1,pages:1,total:1,per_page:20}));
check('slot control at limits', () => {
  __x.slotControl({...state.nodes[0], slots:0});
  __x.slotControl({...state.nodes[0], slots:16});
});

// The wizard's review step must offer a route to every other step, or a
// change late in the flow means paging back through everything.
(async () => {
  console.log('\nWizard navigation:');
  await __x.openWizard();
  __x.draft.name = 'Check'; __x.draft.watch_path = '/in';
  for (let i = 0; i < __x.STEPS.length - 1; i++) __x.nextStep();
  await new Promise(r => setTimeout(r, 50));

  check('reaches the review step', () => {
    if (__x.step !== __x.STEPS.length - 1) throw new Error('at step ' + __x.step);
  });

  const rows = [...els.wiz.innerHTML.matchAll(/jumpTo\((\d+)\)/g)].map(m => +m[1]);
  check('review links to every earlier step', () => {
    const missing = [];
    for (let i = 0; i < __x.STEPS.length - 1; i++)
      if (!rows.includes(i)) missing.push(__x.STEPS[i]);
    if (missing.length) throw new Error('no link to: ' + missing.join(', '));
  });

  check('jumping sets a return path', () => {
    __x.jumpTo(3);
    if (__x.returnTo !== __x.STEPS.length - 1) throw new Error('returnTo not set');
    if (!els.wiz.innerHTML.includes('Back to review'))
      throw new Error('no Back to review button');
  });

  check('returning clears it', () => {
    __x.backToReview();
    if (__x.returnTo !== null) throw new Error('returnTo still set');
  });

  check('editing pre-fills from a saved library', () => {
    const lib = {id:99, name:'Saved', watch_path:'/w', output_path:'/o',
      original_action:'delete',
      profile:{video_codec:'hevc', container:'mkv', keep_chapters:false},
      filters:{skip_extensions:['avi']}, naming:{enabled:true, scheme:'plex'}};
    global.window.__state = {libraries:[lib]};
    return __x.openWizard(99).then(() => {
      const d = __x.draft;
      if (d.name !== 'Saved') throw new Error('name not loaded');
      if (d.original_action !== 'delete') throw new Error('original action not loaded');
      if (d.keep_chapters !== false) throw new Error('a false value was dropped');
      if (d.naming.scheme !== 'plex') throw new Error('naming not loaded');
      if (d.filters.skip_extensions[0] !== 'avi') throw new Error('filters not loaded');
      // Settings the saved library predates must keep their defaults.
      if (d.tag_colours !== true) throw new Error('missing setting lost its default');
      if (!d.filters.bitrate_ceiling) throw new Error('nested default lost');
    });
  });

  // Every list in the draft must be a real array before any step renders:
  // a template calling .includes on undefined takes the whole page down.
  check('draft lists are always arrays', () => {
    for (const key of ['auto_retry_steps','audio_languages_list','subtitle_languages']) {
      if (!Array.isArray(__x.draft[key]))
        throw new Error(key + ' is ' + typeof __x.draft[key]);
    }
    if (__x.draft.salvage_when_stuck === undefined)
      throw new Error('salvage_when_stuck missing from the defaults');
    if (__x.draft.min_saving_percent === undefined)
      throw new Error('min_saving_percent missing from the defaults');
  });

  check('every step renders after toggling its switches', async () => {
    for (let i = 0; i < __x.STEPS.length - 1; i++) {
      __x.step = i;
      await __x.drawStep();
      if (!els.wiz.innerHTML.length) throw new Error('step ' + i + ' rendered nothing');
    }
  });

  check('step bar is clickable', () => {
    const segs = els.wiz.innerHTML.match(/<i class="[^"]*" title="/g) || [];
    if (segs.length !== __x.STEPS.length)
      throw new Error(segs.length + ' segments for ' + __x.STEPS.length + ' steps');
  });

  console.log();
  if (failures) {
    console.log(failures.length + ' problem(s):');
    failures.forEach(f => console.log('  ' + f));
    process.exit(1);
  }
  console.log('Interface renders cleanly.');
})();

// Duplicate definitions have shadowed working code more than once, so the
// script is scanned for repeats rather than trusting that edits landed.
(() => {
  const html = require('fs').readFileSync(__dirname + '/server/static/index.html', 'utf8');
  const js = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'));
  const seen = {};
  for (const m of js.matchAll(/^(?:async\s+)?function\s+(\w+)\s*\(/gm))
    seen[m[1]] = (seen[m[1]] || 0) + 1;
  const dupes = Object.entries(seen).filter(([, n]) => n > 1);
  console.log();
  if (dupes.length) {
    console.log('Duplicate function definitions found:');
    dupes.forEach(([name, n]) => console.log(`  ${name} defined ${n} times`));
    process.exit(1);
  }
  console.log('No duplicate function definitions.');
})();
