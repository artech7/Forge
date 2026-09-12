// Renders the interface against a fake DOM to catch missing functions and
// render-time errors before they show up as a blank page.
//   node check-ui.js
const fs = require('fs');
const els = {};
// appendChild/removeChild/children are here because notify() uses them to
// stack toasts. Without them any code path that raises a toast died with
// an uncatchable crash instead of a named failure — which is how a real
// bug in duplicateLib first showed up as "host.appendChild is not a
// function" pointing at the harness rather than at the code under test.
const el = id => els[id] || (els[id] = {innerHTML:'', textContent:'', scrollTop:0,
  classList:{add(){},remove(){},toggle(){},contains(){return false}}, remove(){ delete els[id]; },
  parentNode:{insertBefore(){}}, scrollIntoView(){},
  children: [], appendChild(c){ this.children.push(c); },
  removeChild(c){ this.children = this.children.filter(x => x !== c); },
  get firstChild(){ return this.children[0]; },
  querySelector: () => null, querySelectorAll: () => []});

// A real page always has a body; code that toggles a class on it (the
// sign-in screen does) would otherwise only fail here, never in a
// browser. Tracks its classes so an assertion can read them back.
const bodyClasses = new Set();
const body = {
  classList: {
    add: c => bodyClasses.add(c), remove: c => bodyClasses.delete(c),
    toggle: c => bodyClasses.has(c) ? bodyClasses.delete(c) : bodyClasses.add(c),
    contains: c => bodyClasses.has(c),
  },
  style: {}, appendChild(){}, removeChild(){}, children: [],
};

global.document = {
  body,
  getElementById: id => (id === 'wizerr' ? null : el(id)),
  querySelector: sel => (sel === '.wiz-nav' ? el('wiznav') : null),
  querySelectorAll: () => [], addEventListener: () => {},
  createElement: () => ({style:{}, scrollIntoView(){}, classList:{add(){}}}),
  documentElement: {dataset: {}}};
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
  try {
    const result = fn();
    // An async check (or one returning a promise chain) must be awaited
    // before declaring success — otherwise its assertions run in the
    // background, after this call has already logged "ok", and a later
    // check reads DOM state the async one hasn't finished writing yet.
    if (result && typeof result.then === 'function') {
      return result.then(() => console.log('  ok      ' + label),
        e => { failures++; console.log('  FAILED  ' + label + ' -> ' + e.message); });
    }
    console.log('  ok      ' + label);
  } catch (e) { failures++; console.log('  FAILED  ' + label + ' -> ' + e.message); }
}

try { eval(src + '\nglobal.__x = {render, renderLibs, renderTabs, renderJobs, slotControl, ' +
  'setSlots, retryJob, cancelJob, requeueJob, removeJob, bulkJobs, switchView, ' +
  'loadView, openWizard, drawSettings, refreshPreview, scanOne, scanAll, ' +
  'toggleLib, removeLib, splitList, describeFilters, wizardError, jumpTo, ' +
  'backToReview, nextStep, validateFirstStep, STEPS, GROUPS, groupIndexOf, ' +
  'SETTINGS_TABS, HEALTH_TABS, duplicateLib, watchInterval, everyPhrase, saveLibrary, ' +
  'set SET(v){SET = v;}, ' +
  'get settingsSection(){return settingsSection;}, ' +
  'set settingsSection(v){settingsSection = v;}, ' +
  'get editingId(){return editingId;}, ' +
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

  await check('editing pre-fills from a saved library', () => {
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

  await check('every step renders after toggling its switches', async () => {
    for (let i = 0; i < __x.STEPS.length - 1; i++) {
      __x.step = i;
      await __x.drawStep();
      if (!els.wiz.innerHTML.length) throw new Error('step ' + i + ' rendered nothing');
    }
  });

  await check('step bar is clickable', async () => {
    // The bar only shows dots for the steps inside the CURRENT group
    // (see the comment in bar()) rather than all of them at once, so
    // land on a group with more than one step to have something to count.
    const group = __x.GROUPS.find(g => g.steps.length > 1);
    __x.step = group.steps[0];
    await __x.drawStep();
    const segs = els.wiz.innerHTML.match(/<i class="[^"]*" title="/g) || [];
    if (segs.length !== group.steps.length)
      throw new Error(segs.length + ' segments for a ' + group.steps.length + '-step group');
  });

  // Settings was never rendered here — only name-checked in the export
  // list — so collapsing three credential tabs into one could have
  // dropped a field with nothing to notice. Every tab now gets drawn.
  console.log('\nSettings panels:');
  __x.SET = {schedule:{}, originals:{}, tmdb:{enabled:true, key:'k'},
             bazarr:{url:'b', api_key:'k'}, radarr:{url:'r', api_key:'k'},
             sonarr:{url:'s', api_key:'k'}, auto_fail:{}, scan_seconds:30};

  for (const [id, label] of __x.SETTINGS_TABS)
    check(`${label} renders`, () => {
      __x.settingsSection = id;
      __x.drawSettings();
      if (!els.wiz.innerHTML) throw new Error('rendered nothing');
    });

  // The point of the merge was that nothing was lost in it. Each of
  // these lived on a tab of its own before.
  check('Connections still holds every credential it absorbed', () => {
    __x.settingsSection = 'connections';
    __x.drawSettings();
    const h = els.wiz.innerHTML;
    for (const want of ['TMDB key', 'Bazarr', 'Radarr', 'Sonarr',
                        'testTmdb', 'testBazarr', "testArr('radarr')",
                        "testArr('sonarr')", 'Path mapping'])
      if (!h.includes(want)) throw new Error('lost: ' + want);
  });

  check('every tab id has a panel behind it', () => {
    // A renamed tab whose panel kept the old id renders an empty page
    // with no error, which is exactly the failure a render check misses.
    const src = require('fs').readFileSync(
      __dirname + '/server/static/index.html', 'utf8');
    const panels = new Set([...src.matchAll(/settingsSection === '(\w+)'/g)]
      .map(m => m[1]));
    for (const [id] of __x.SETTINGS_TABS)
      if (!panels.has(id)) throw new Error('no panel for tab ' + id);
    for (const id of panels)
      if (!__x.SETTINGS_TABS.some(([t]) => t === id) && id !== 'access')
        throw new Error('panel ' + id + ' has no tab');
  });

  console.log('\nDuplicating a library:');
  // Earlier checks render states with no libraries in them, and
  // duplicateLib reads the live state rather than a passed-in object.
  global.window.__state = state;
  await __x.duplicateLib(1);
  check('copies the recipe', () => {
    if (__x.draft.video_codec !== 'hevc') throw new Error('lost video codec');
    if (__x.draft.original_action !== 'archive') throw new Error('lost originals');
  });
  check('clears the three things that make it a different library', () => {
    for (const f of ['name', 'watch_path', 'output_path'])
      if (__x.draft[f] !== '') throw new Error(f + ' carried over: ' + __x.draft[f]);
  });
  check('saves as new rather than editing the original', () => {
    if (__x.editingId !== null) throw new Error('still editing ' + __x.editingId);
  });
  check('starts at the first step, not review', () => {
    if (__x.step !== 0) throw new Error('at step ' + __x.step);
  });

  // originals_path is a column on the libraries table, not a profile key,
  // so draftFromLibrary's "copy everything the profile has" loop was never
  // going to find it. Reopening a library showed the box empty, and
  // saveLibrary sends '' as null -- so changing anything else silently
  // cleared where that library keeps its originals, and they went back to
  // landing inside the watched folder.
  // A tab renamed in one place and referenced by its old name in
  // another reads as a pointer to a screen that isn't there. Every
  // "Library Health -> X" in the interface must name a real tab.
  check('cross-references name a Library Health tab that exists', () => {
    const src = require('fs').readFileSync(
      __dirname + '/server/static/index.html', 'utf8');
    const labels = new Set(__x.HEALTH_TABS.map(([, l]) => l));
    for (const m of src.matchAll(/Library Health → ([^<,.]+)/g)) {
      const named = m[1].trim();
      if (!labels.has(named))
        throw new Error(`points at "${named}", which is not a tab`);
    }
  });
  check('no two health tabs read as the same thing', () => {
    // The pair that prompted this: one tab about tracks being in the
    // wrong language, one about there being no audio stream at all.
    // Both were named after audio, so both read as "audio is missing".
    const labels = __x.HEALTH_TABS.map(([, l]) => l);
    const audio = labels.filter(l => /audio/i.test(l));
    if (audio.length > 2)
      throw new Error('too many tabs named after audio: ' + audio.join(', '));
    const lang = labels.find(l => /language/i.test(l));
    if (!lang) throw new Error('nothing names the language check');
    if (/audio/i.test(lang))
      throw new Error(`"${lang}" is about languages but named after audio`);
  });

  console.log('\nWhere originals are kept survives a round trip:');
  const kept = {id:7, name:'Films', watch_path:'/w', output_path:'',
    original_action:'archive', originals_path:'/originals',
    profile:{video_codec:'hevc'}, filters:{}, naming:{}};
  global.window.__state = {libraries:[kept]};
  await __x.openWizard(7);
  check('reopening shows the path that was saved', () => {
    if (__x.draft.originals_path !== '/originals')
      throw new Error('got ' + JSON.stringify(__x.draft.originals_path));
  });
  await check('saving an unrelated change does not clear it', async () => {
    let sent = null;
    const realFetch = global.fetch;
    global.fetch = async (url, opts) => {
      if (opts && opts.method === 'PATCH') sent = JSON.parse(opts.body);
      return {ok:true, json: async () => ({})};
    };
    try {
      __x.draft.audio_bitrate = '192k';        // change something else
      await __x.saveLibrary();
    } finally { global.fetch = realFetch; }
    if (!sent) throw new Error('no PATCH was sent');
    if (sent.originals_path !== '/originals')
      throw new Error('sent ' + JSON.stringify(sent.originals_path));
  });
  await check('duplicating carries the originals path over', async () => {
    global.window.__state = {libraries:[kept]};
    await __x.duplicateLib(7);
    if (__x.draft.originals_path !== '/originals')
      throw new Error('got ' + JSON.stringify(__x.draft.originals_path));
  });

  console.log('\nHints quote the setting rather than a fixed number:');
  check('reads the real scan interval out of live state', () => {
    global.window.__state = {settings:{scan_seconds:300}};
    if (__x.watchInterval() !== 'every 5 minutes')
      throw new Error(__x.watchInterval());
    global.window.__state = {settings:{scan_seconds:30}};
    if (__x.watchInterval() !== 'every 30 seconds')
      throw new Error(__x.watchInterval());
  });
  check('copes with the setting missing entirely', () => {
    global.window.__state = {};
    if (__x.watchInterval() !== 'every 30 seconds')
      throw new Error(__x.watchInterval());
  });
  check('no hint hardcodes the scan interval any more', () => {
    const src = require('fs').readFileSync(
      __dirname + '/server/static/index.html', 'utf8');
    const m = src.match(/checks it every \d+ seconds/);
    if (m) throw new Error('still hardcoded: ' + m[0]);
  });

  console.log();
  if (failures) {
    // Each failure already printed its own detail line as it happened;
    // failures is just a count, not a list, so there's nothing to repeat.
    console.log(failures + ' problem(s).');
    process.exit(1);
  }
  console.log('Interface renders cleanly.');
  // Raising a toast leaves its dismiss timer pending, which held node
  // open for the full 5.5s after the last check passed. The duplicate
  // scan below is synchronous and has already run by this point, so
  // there is nothing left to wait for.
  process.exit(0);
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
