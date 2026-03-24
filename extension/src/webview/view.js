// @ts-nocheck
/* global acquireVsCodeApi, CSS */

const vscode = acquireVsCodeApi();
const feed         = document.getElementById('feed');
const inputEl      = document.getElementById('input');
const sendBtn      = document.getElementById('sendBtn');
const stopBtn      = document.getElementById('stopBtn');
const progressBar  = document.getElementById('progressBar');
const filesPanel   = document.getElementById('filesPanel');
const filesPanelBody    = document.getElementById('filesPanelBody');
const filesPanelSummary = document.getElementById('filesPanelSummary');
const fpKeepBtn    = document.getElementById('fpKeepBtn');
const fpUndoBtn    = document.getElementById('fpUndoBtn');
const showLogsBtn       = document.getElementById('showLogsBtn');
const newChatBtn        = document.getElementById('newChatBtn');
const newChatConfirmEl  = document.getElementById('newChatConfirm');
const newChatConfirmOk  = document.getElementById('newChatConfirmOk');
const newChatConfirmCancel = document.getElementById('newChatConfirmCancel');
const slashSuggestionsEl = document.getElementById('slashSuggestions');
const slashPillEl        = document.getElementById('slashPill');
const slashPillLabel     = document.getElementById('slashPillLabel');
const slashPillX         = document.getElementById('slashPillX');
const modeBadgeEl        = document.getElementById('modeBadge');
const modeBadgeLabelEl   = document.getElementById('modeBadgeLabel');
const modeBadgeLockEl    = document.getElementById('modeBadgeLock');
const modeChangeConfirmEl  = document.getElementById('modeChangeConfirm');
const modeChangeTargetEl   = document.getElementById('modeChangeTarget');
const modeChangeOkEl       = document.getElementById('modeChangeOk');
const modeChangeCancelEl   = document.getElementById('modeChangeCancel');
let pendingMode = null;

const SEND_ICON_BUILD = '<svg viewBox="0 0 16 16"><path d="M1 1l14 7L1 15V9l10-2L1 7V1z"/></svg>';
const SEND_ICON_QUEUE = '<svg viewBox="0 0 16 16"><path d="M8 2l4 4H9v8H7V6H4l4-4z"/></svg>';

modeChangeOkEl.addEventListener('click', () => {
  modeChangeConfirmEl.style.display = 'none';
  if (pendingMode) {
    unlockMode();
    applyMode(pendingMode);
    pendingMode = null;
    doNewChat();
  }
});
modeChangeCancelEl.addEventListener('click', () => {
  modeChangeConfirmEl.style.display = 'none';
  pendingMode = null;
});

// Mode state
const MODE_META = {
  junior: { label: 'Junior', icon: '&#9671;', cls: 'm-junior',
            desc: 'Learning mode - step-by-step guidance, clarifications, human in the loop at every decision' },
  senior: { label: 'Senior', icon: '&#9670;', cls: 'm-senior',
            desc: 'Collaborative mode - human in the loop, reviews before executing significant changes' },
  expert: { label: 'Expert', icon: '&#9654;', cls: 'm-expert',
            desc: 'Autonomous mode - runs without interruption, only pauses for genuinely dangerous commands' },
};
let currentMode = 'senior';
let modeLocked  = false;

function applyMode(mode) {
  if (modeLocked) return;
  if (!MODE_META[mode]) return;
  currentMode = mode;
  const m = MODE_META[mode];
  modeBadgeEl.className = 'mode-badge ' + m.cls;
  modeBadgeLabelEl.textContent = m.label;
  modeBadgeEl.title = m.desc;
}

function lockMode() {
  modeLocked = true;
  modeBadgeEl.classList.add('locked');
  modeBadgeLockEl.style.display = '';
}

function unlockMode() {
  modeLocked = false;
  modeBadgeEl.classList.remove('locked');
  modeBadgeLockEl.style.display = 'none';
}

// Badge click: open /mode sub-suggestions (only when unlocked)
modeBadgeEl.addEventListener('click', () => {
  if (modeLocked) return;
  openModeSuggestions();
  inputEl.focus();
});

// Slash command registry
const SLASH_COMMANDS = [
  { name: '/mode',    desc: 'Switch mode for this chat' },
  { name: '/fix',     desc: 'Ask the agent to fix an issue in generated code' },
  { name: '/explain', desc: 'Explain the last generated code or output' },
  { name: '/retry',   desc: 'Retry the last failed build' },
  { name: '/clear',   desc: 'Clear conversation and start fresh' },
  { name: '/fun',     desc: 'Toggle the spirit of JAME' },
  { name: '/logs',    desc: 'Show backend output in the VS Code panel' },
];

// Active slash command state
let activeSlashCmd = null;
let suggestionIndex = -1;

// Slash suggestion helpers
function openSuggestions(query) {
  const matches = query === ''
    ? SLASH_COMMANDS
    : SLASH_COMMANDS.filter(c => c.name.startsWith('/' + query));
  if (matches.length === 0) { closeSuggestions(); return; }

  suggestionIndex = 0;
  slashSuggestionsEl.innerHTML = '';
  matches.forEach((cmd, i) => {
    const item = document.createElement('div');
    item.className = 'slash-suggestion-item' + (i === 0 ? ' active' : '');
    item.dataset.cmd = cmd.name;
    item.innerHTML =
      '<span class="slash-suggestion-name">' + escHtml(cmd.name) + '</span>' +
      '<span class="slash-suggestion-desc">' + escHtml(cmd.desc) + '</span>';
    item.addEventListener('mousedown', (e) => {
      e.preventDefault();
      selectSlashCommand(cmd.name);
    });
    slashSuggestionsEl.appendChild(item);
  });
  slashSuggestionsEl.classList.add('open');
}

function openModeSuggestions() {
  slashSuggestionsEl.innerHTML = '';
  const modes = ['junior', 'senior', 'expert'];
  suggestionIndex = modes.indexOf(currentMode);
  if (suggestionIndex === -1) suggestionIndex = 0;
  modes.forEach((mode, i) => {
    const m = MODE_META[mode];
    const item = document.createElement('div');
    item.className = 'slash-sub-item' + (i === suggestionIndex ? ' active' : '');
    item.dataset.mode = mode;
    item.innerHTML =
      '<span class="slash-sub-name ' + m.cls + '">' + m.icon + ' ' + escHtml(m.label) + '</span>' +
      '<span class="slash-sub-desc">' + escHtml(m.desc) + '</span>';
    item.addEventListener('mousedown', (e) => {
      e.preventDefault();
      selectMode(mode);
    });
    slashSuggestionsEl.appendChild(item);
  });
  slashSuggestionsEl.classList.add('open');
}

function selectMode(mode) {
  if (modeLocked) {
    pendingMode = mode;
    modeChangeTargetEl.textContent = MODE_META[mode] ? MODE_META[mode].label : mode;
    modeChangeConfirmEl.style.display = 'flex';
    closeSuggestions();
    return;
  }
  applyMode(mode);
  const raw = inputEl.value;
  const slashMatch = raw.match(new RegExp('^([ \\t]*)[/][^ \\t]*'));
  inputEl.value = slashMatch ? raw.slice(slashMatch[0].length).trimStart() : raw;
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
  closeSuggestions();
  inputEl.focus();
}

function closeSuggestions() {
  slashSuggestionsEl.classList.remove('open');
  slashSuggestionsEl.innerHTML = '';
  suggestionIndex = -1;
}

function moveSuggestion(dir) {
  const items = slashSuggestionsEl.querySelectorAll('.slash-suggestion-item, .slash-sub-item');
  if (items.length === 0) return false;
  items[suggestionIndex] && items[suggestionIndex].classList.remove('active');
  suggestionIndex = (suggestionIndex + dir + items.length) % items.length;
  items[suggestionIndex].classList.add('active');
  return true;
}

function selectActiveSuggestion() {
  const cmdItem = slashSuggestionsEl.querySelector('.slash-suggestion-item.active');
  if (cmdItem) { selectSlashCommand(cmdItem.dataset.cmd); return; }
  const modeItem = slashSuggestionsEl.querySelector('.slash-sub-item.active');
  if (modeItem) { selectMode(modeItem.dataset.mode); }
}

let _funMode = false;
const _jameWords = {
  normal: ['Just', 'A', 'Model-Driven', 'Engineer'],
  fun:    ['Just', 'Another', 'Mad', 'Engineer'],
};
function setJameTitle(fun) {
  const title = document.getElementById('jameTitle');
  if (!title) return;
  const words = fun ? _jameWords.fun : _jameWords.normal;
  title.innerHTML =
    '<span class="jame-letters"><span class="j">J</span><span class="a">A</span><span class="m">M</span><span class="e">E</span></span>' +
    '<span class="jame-acronym">' +
      '<span class="w-j">' + words[0] + '</span>' +
      '<span class="w-a">' + words[1] + '</span>' +
      '<span class="w-m">' + words[2] + '</span>' +
      '<span class="w-e">' + words[3] + '</span>' +
    '</span>';
}

function selectSlashCommand(name) {
  if (name === '/clear') { closeSuggestions(); inputEl.value = ''; doNewChat(); return; }
  if (name === '/logs') {
    const raw = inputEl.value;
    const slashMatch = raw.match(new RegExp('^([ \\t]*)[/][^ \\t]*'));
    inputEl.value = slashMatch ? raw.slice(slashMatch[0].length).trimStart() : raw;
    closeSuggestions();
    vscode.postMessage({ command: 'showLogs' });
    inputEl.focus();
    return;
  }
  if (name === '/fun') {
    _funMode = !_funMode;
    setJameTitle(_funMode);
    const raw = inputEl.value;
    const slashMatch = raw.match(new RegExp('^([ \\t]*)[/][^ \\t]*'));
    inputEl.value = slashMatch ? raw.slice(slashMatch[0].length).trimStart() : raw;
    closeSuggestions();
    addSysMsg(_funMode ? 'Just Another Mad Engineer - engaged.' : 'Back to Just A Model-Driven Engineer.', 'info');
    saveState();
    inputEl.focus();
    return;
  }
  if (name === '/mode') {
    const raw = inputEl.value;
    const slashMatch = raw.match(new RegExp('^([ \\t]*)[/][^ \\t]*'));
    inputEl.value = slashMatch ? raw.slice(slashMatch[0].length).trimStart() : raw;
    closeSuggestions();
    openModeSuggestions();
    return;
  }
  // Regular slash command - becomes a pill prefix
  activeSlashCmd = name;
  slashPillLabel.textContent = name;
  slashPillEl.classList.add('visible');
  const raw = inputEl.value;
  const slashMatch = raw.match(new RegExp('^([ \\t]*)[/][^ \\t]*'));
  inputEl.value = slashMatch ? raw.slice(slashMatch[0].length) : raw;
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
  closeSuggestions();
  inputEl.focus();
}

function clearSlashCommand() {
  activeSlashCmd = null;
  slashPillEl.classList.remove('visible');
  slashPillLabel.textContent = '';
}

// Dismiss pill when X is clicked
slashPillX.addEventListener('mousedown', (e) => {
  e.preventDefault();
  clearSlashCommand();
  inputEl.focus();
});

newChatConfirmOk.addEventListener('click', () => {
  newChatConfirmEl.style.display = 'none';
  doNewChat();
});
newChatConfirmCancel.addEventListener('click', () => {
  newChatConfirmEl.style.display = 'none';
});

let currentRunId     = null;
let ws               = null;
let isRunning        = false;
let generatedFiles   = [];
let projectDir       = null;
let currentIteration = 0;
let lastAgent        = null;
let tcAutoApprove    = false;
let tcPending        = null;
// files panel state
let fpFiles    = {};
let fpDecided  = new Set();

// State persistence (survives panel moves)
function saveState() {
  vscode.setState({
    feedHtml:    feed.innerHTML,
    fpFiles:     fpFiles,
    fpDecided:   [...fpDecided],
    fpVisible:   filesPanel.classList.contains('visible'),
    fpBodyHtml:  filesPanelBody.innerHTML,
    fpSummary:   filesPanelSummary.textContent,
    currentRunId: currentRunId,
    currentMode: currentMode,
    modeLocked:  modeLocked,
    funMode:     _funMode,
  });
}

// Restore persisted state on load
(function restoreState() {
  const s = vscode.getState();
  if (!s) return;
  if (s.feedHtml)   { feed.innerHTML = s.feedHtml; removeEmpty(); }
  if (s.fpFiles)    { fpFiles = s.fpFiles; }
  if (s.fpDecided)  { fpDecided = new Set(s.fpDecided); }
  if (s.fpBodyHtml) { filesPanelBody.innerHTML = s.fpBodyHtml; }
  if (s.fpSummary)  { filesPanelSummary.textContent = s.fpSummary; }
  if (s.fpVisible)  { filesPanel.classList.add('visible'); }
  if (s.currentRunId) { currentRunId = s.currentRunId; }
  if (s.currentMode) { applyMode(s.currentMode); }
  if (s.modeLocked)  { lockMode(); }
  if (s.funMode)     { _funMode = true; setJameTitle(true); }
  // Do not restore running UI on reload — the WebSocket is gone and the run cannot be resumed.
})();

// Keep All / Undo All buttons
fpKeepBtn.addEventListener('click', () => {
  const undecided = Object.keys(fpFiles).filter(p => !fpDecided.has(p));
  vscode.postMessage({ command: 'acceptAll', paths: undecided });
  undecided.forEach(p => {
    const row = filesPanelBody.querySelector('[data-path="' + CSS.escape(p) + '"]');
    if (row) row.remove();
    delete fpFiles[p];
    fpDecided.add(p);
  });
  updateFilesPanelSummary();
  if (Object.keys(fpFiles).length === 0) filesPanel.classList.remove('visible');
});
fpUndoBtn.addEventListener('click', () => {
  const undecided = Object.keys(fpFiles).filter(p => !fpDecided.has(p));
  vscode.postMessage({ command: 'discardAll', paths: undecided });
  undecided.forEach(p => {
    const row = filesPanelBody.querySelector('[data-path="' + CSS.escape(p) + '"]');
    if (row) row.remove();
    delete fpFiles[p];
    fpDecided.add(p);
  });
  updateFilesPanelSummary();
  if (Object.keys(fpFiles).length === 0) filesPanel.classList.remove('visible');
});

// Files panel helpers
function updateFilesPanelSummary() {
  const count = Object.keys(fpFiles).length;
  if (count === 0) {
    filesPanelSummary.textContent = '';
    saveState();
    return;
  }
  const totalLines = Object.values(fpFiles).reduce(function(a, b) { return a + b; }, 0);
  filesPanelSummary.textContent = count + ' file' + (count !== 1 ? 's' : '') + '  +' + totalLines;
  saveState();
}

function resetFilesPanel() {
  fpFiles = {};
  fpDecided = new Set();
  filesPanelBody.innerHTML = '';
  filesPanelSummary.textContent = '';
  fpKeepBtn.textContent = 'Keep All';
  fpKeepBtn.disabled = false;
  fpUndoBtn.textContent = 'Undo All';
  fpUndoBtn.disabled = false;
  filesPanel.classList.remove('visible');
  const v = filesPanel.querySelector('.fp-verdict');
  if (v) v.remove();
}

function addFileToPanel(relPath, content) {
  // Skip files the user already decided on (keep/undo) — prevents late-arriving
  // file_generated events from re-opening the panel after Keep All / Undo All.
  if (fpDecided.has(relPath)) return;
  const lines = content ? content.split('\n').length : 0;
  fpFiles[relPath] = lines;

  const existing = filesPanelBody.querySelector('[data-path="' + CSS.escape(relPath) + '"]');
  if (existing) existing.remove();

  const ext = (relPath.split('.').pop() || 'file').toLowerCase();
  const name = relPath.split('/').pop() || relPath;

  const row = document.createElement('div');
  row.className = 'fp-row';
  row.dataset.path = relPath;
  row.innerHTML =
    '<span class="fp-ext">' + escHtml(ext) + '</span>' +
    '<span class="fp-name" title="' + escHtml(relPath) + '">' + escHtml(name) + '</span>' +
    '<span class="fp-lines">+' + lines + '</span>' +
    '<span class="fp-action fp-keep" title="Accept this file">\u2713</span>' +
    '<span class="fp-action fp-undo" title="Discard this file">\u2717</span>';

  row.querySelector('.fp-name').addEventListener('click', (e) => {
    e.stopPropagation();
    vscode.postMessage({ command: 'openProposedChange', filePath: relPath, fileContent: content });
  });
  row.querySelector('.fp-keep').addEventListener('click', (e) => {
    e.stopPropagation();
    vscode.postMessage({ command: 'acceptFile', filePath: relPath });
    fpDecided.add(relPath);
    delete fpFiles[relPath];
    row.remove();
    updateFilesPanelSummary();
    if (Object.keys(fpFiles).length === 0) filesPanel.classList.remove('visible');
  });
  row.querySelector('.fp-undo').addEventListener('click', (e) => {
    e.stopPropagation();
    vscode.postMessage({ command: 'discardFile', filePath: relPath });
    fpDecided.add(relPath);
    delete fpFiles[relPath];
    row.remove();
    updateFilesPanelSummary();
    if (Object.keys(fpFiles).length === 0) filesPanel.classList.remove('visible');
  });

  filesPanelBody.appendChild(row);
  updateFilesPanelSummary();
  filesPanel.classList.add('visible');
}

// Junior mode variant: shows a locked placeholder row — no content visible,
// no open-diff action, no keep/undo (the file is the exercise stub to implement).
// exerciseFileContents: relPath → stub content (for opening on click)
const exerciseFileContents = {};

function addFileToPanelExercise(relPath, content) {
  if (fpDecided.has(relPath)) return;
  fpFiles[relPath] = 0;
  exerciseFileContents[relPath] = content || '';

  const existing = filesPanelBody.querySelector('[data-path="' + CSS.escape(relPath) + '"]');
  if (existing) existing.remove();

  const ext = (relPath.split('.').pop() || 'file').toLowerCase();
  const name = relPath.split('/').pop() || relPath;

  const row = document.createElement('div');
  row.className = 'fp-row fp-row-exercise';
  row.dataset.path = relPath;
  row.title = 'Click to open — implement the TODO stubs in this file';
  row.innerHTML =
    '<span class="fp-ext">' + escHtml(ext) + '</span>' +
    '<span class="fp-name" title="' + escHtml(relPath) + '">' + escHtml(name) + '</span>' +
    '<span class="fp-stub-badge">stub</span>';

  row.addEventListener('click', function() {
    vscode.postMessage({
      command: 'openExerciseFile',
      filePath: relPath,
      fileContent: exerciseFileContents[relPath] || '',
    });
  });

  filesPanelBody.appendChild(row);
  updateFilesPanelSummary();
  filesPanel.classList.add('visible');
}

function setFilesPanelVerdict(qaPassed) {
  let v = filesPanel.querySelector('.fp-verdict');
  if (!v) {
    v = document.createElement('div');
    filesPanel.appendChild(v);
  }
  v.className = 'fp-verdict ' + (qaPassed ? 'pass' : 'fail');
  v.textContent = qaPassed ? '\u2713 QA passed' : '\u2717 QA did not pass';
}

// Progress helpers
function setProgressIndeterminate() {
  progressBar.classList.add('indeterminate');
  progressBar.style.width = '';
}
function clearProgress() {
  progressBar.classList.remove('indeterminate');
  progressBar.style.width = '0%';
}

// Prompt history
let promptHistory = [];
let historyIndex  = -1;
let historyDraft  = '';

// Auto-resize textarea + slash detection
inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
  updateComposerActions();
  if (historyIndex !== -1) {
    historyIndex = -1;
    historyDraft = '';
  }
  if (!activeSlashCmd) {
    const val = inputEl.value;
    const slashMatch = val.match(new RegExp('^[ \\t]*[/]([a-zA-Z0-9_-]*)$'));
    if (slashMatch) {
      openSuggestions(slashMatch[1]);
    } else {
      closeSuggestions();
    }
  }
});

// Keyboard handling
inputEl.addEventListener('keydown', (e) => {
  if (slashSuggestionsEl.classList.contains('open')) {
    if (e.key === 'ArrowDown') { e.preventDefault(); moveSuggestion(1); return; }
    if (e.key === 'ArrowUp')   { e.preventDefault(); moveSuggestion(-1); return; }
    if (e.key === 'Escape')    { e.preventDefault(); closeSuggestions(); return; }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      selectActiveSuggestion();
      return;
    }
    if (e.key === 'Tab') {
      e.preventDefault();
      selectActiveSuggestion();
      return;
    }
  }

  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
    return;
  }

  if (e.key === 'Backspace' && activeSlashCmd && inputEl.value === '') {
    clearSlashCommand();
    return;
  }

  if (e.key === 'ArrowUp') {
    const selStart = inputEl.selectionStart;
    const beforeCaret = inputEl.value.substring(0, selStart);
    const onFirstLine = !beforeCaret.includes('\n');
    if (onFirstLine && promptHistory.length > 0) {
      e.preventDefault();
      if (historyIndex === -1) {
        historyDraft = inputEl.value;
        historyIndex = promptHistory.length - 1;
      } else if (historyIndex > 0) {
        historyIndex--;
      }
      inputEl.value = promptHistory[historyIndex];
      inputEl.style.height = 'auto';
      inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
      inputEl.setSelectionRange(0, 0);
    }
    return;
  }

  if (e.key === 'ArrowDown') {
    if (historyIndex === -1) return;
    const selStart = inputEl.selectionStart;
    const afterCaret = inputEl.value.substring(selStart);
    const onLastLine = !afterCaret.includes('\n');
    if (onLastLine) {
      e.preventDefault();
      if (historyIndex < promptHistory.length - 1) {
        historyIndex++;
        inputEl.value = promptHistory[historyIndex];
      } else {
        historyIndex = -1;
        inputEl.value = historyDraft;
        historyDraft = '';
      }
      inputEl.style.height = 'auto';
      inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
      const len = inputEl.value.length;
      inputEl.setSelectionRange(len, len);
    }
    return;
  }
});

sendBtn.addEventListener('click', send);
stopBtn.addEventListener('click', stop);
showLogsBtn.addEventListener('click', () => vscode.postMessage({ command: 'showLogs' }));
newChatBtn.addEventListener('click', newChat);

function updateComposerActions() {
  if (!isRunning) {
    sendBtn.style.display = 'flex';
    stopBtn.style.display = 'none';
    sendBtn.innerHTML = SEND_ICON_BUILD;
    sendBtn.title = 'Build (Enter)';
    return;
  }

  const hasText = Boolean(inputEl.value.trim());
  if (hasText) {
    sendBtn.style.display = 'flex';
    stopBtn.style.display = 'none';
    sendBtn.innerHTML = SEND_ICON_QUEUE;
    sendBtn.title = 'Queue instruction (Enter)';
    return;
  }

  sendBtn.style.display = 'none';
  stopBtn.style.display = 'flex';
  stopBtn.title = 'Cancel run';
}

// Running state
function setRunning(running) {
  isRunning = running;
  inputEl.disabled = false;
  updateComposerActions();
  inputEl.placeholder = running
    ? 'Add instruction for the next developer iteration...'
    : 'Describe what to build...';
  if (running) setProgressIndeterminate();
  else clearProgress();
  saveState();
}

// Utilities
function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Lightweight markdown renderer (no external lib - webview is sandboxed)
function renderMd(text) {
  if (!text) return '';
  let s = escHtml(String(text));
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/^- (.+)$/gm, '<li style="margin-left:12px;list-style:disc">$1</li>');
  s = s.replace(/\n/g, '<br>');
  return s;
}

function scrollFeed() { feed.scrollTop = feed.scrollHeight; saveState(); }

function removeEmpty() {
  const e = document.getElementById('emptyState');
  if (e) e.remove();
}

// User message bubble
function addUserMessage(text) {
  removeEmpty();
  breakAgentGroup();
  const div = document.createElement('div');
  div.className = 'user-msg';
  div.innerHTML = '<div class="user-bubble">' + escHtml(text) + '</div>';
  feed.appendChild(div);
  scrollFeed();
}

// System message
const SYS_ICONS = { ok: '\u2713', warn: '\u26a0', err: '\u2715', info: '\u2139' };
function addSysMsg(text, level) {
  breakAgentGroup();
  const div = document.createElement('div');
  div.className = 'sys-msg ' + (level || 'info');
  div.innerHTML =
    '<em class="sys-msg-icon">' + (SYS_ICONS[level] || '\u2139') + '</em>' +
    '<span class="sys-msg-text">' + escHtml(text) + '</span>';
  feed.appendChild(div);
  scrollFeed();
}

// Agent log row
let _lastRowAc = null;
let _currentAgentGroup = null; // the group wrapper div for the current agent streak
const AGENT_SHORT = {
  'architect':       'ARCH',
  'developer':       'DEV',
  'delivery':        'OPS',
  'quality-engineer':'QA',
  'qa':              'QA',
  'devops':          'OPS',
  'exercise':        'EX',
  'system':          'SYS',
};

/** Call whenever a non-agent element (clarify card, sys msg, etc.) is added to the feed. */
function breakAgentGroup() {
  _lastRowAc = null;
  _currentAgentGroup = null;
}

function addTlRow(agentDisplay, msg, thinking, phase) {
  removeEmpty();

  const agentKey = agentDisplay.toLowerCase().replace(/\s+/g, '-');
  const acClass = {
    'architect': 'ac-architect',
    'developer': 'ac-developer',
    'delivery':  'ac-delivery',
    'quality-engineer': 'ac-qa',
    'qa':        'ac-qa',
    'system':    'ac-system',
  }[agentKey] || 'ac-system';

  const isSame = _lastRowAc === acClass && _currentAgentGroup !== null;
  _lastRowAc = acClass;

  // "BUILD AND TEST" -> "BUILD_AND_TEST" -> phase-build_and_test
  const normalizedPhase = phase
    ? phase.toLowerCase().replace(/[- ]/g, '_').replace(/[^a-z_]/g, '')
    : null;

  const shortLabel = AGENT_SHORT[agentKey] || agentDisplay.slice(0, 4).toUpperCase();

  // Create or reuse the agent group wrapper (holds the continuous left border)
  if (!isSame) {
    _currentAgentGroup = document.createElement('div');
    _currentAgentGroup.className = 'agent-group ' + acClass + ' fade-in';
    feed.appendChild(_currentAgentGroup);
  }

  const row = document.createElement('div');
  row.className = 'agent-row' + (isSame ? ' same-agent' : '');
  row.innerHTML =
    '<span class="ar-tag" title="' + escHtml(agentDisplay) + '">' + escHtml(shortLabel) + '</span>' +
    (normalizedPhase ? '<span class="ar-phase phase-' + normalizedPhase + '">' + escHtml(phase) + '</span>' : '') +
    '<span class="ar-msg">' + renderMd(msg) + '</span>' +
    (thinking ? '<button class="ar-think-btn">\u25b6 thinking</button>' : '');

  _currentAgentGroup.appendChild(row);

  if (thinking) {
    const btn = row.querySelector('.ar-think-btn');
    const thinkDiv = document.createElement('div');
    thinkDiv.className = 'ar-think-content';
    thinkDiv.textContent = thinking;
    _currentAgentGroup.appendChild(thinkDiv);

    btn.addEventListener('click', () => {
      const shown = thinkDiv.classList.contains('shown');
      thinkDiv.classList.toggle('shown', !shown);
      btn.textContent = shown ? '\u25b6 thinking' : '\u25bc thinking';
    });
  }

  scrollFeed();
}

// File diff row (inline under agent row)
function addFileDiffRow(filePath, linesAdded, linesRemoved) {
  const ext = (filePath.split('.').pop() || 'file').toLowerCase();
  const name = filePath.split('/').pop() || filePath;
  const row = document.createElement('div');
  row.className = 'file-diff-row fade-in';
  row.innerHTML =
    '<span class="diff-ext">' + escHtml(ext) + '</span>' +
    '<span class="file-diff-name" title="' + escHtml(filePath) + '">' + escHtml(name) + '</span>' +
    (linesAdded   ? '<span class="diff-added">+' + linesAdded + '</span>'   : '') +
    (linesRemoved ? '<span class="diff-removed">-' + linesRemoved + '</span>' : '');
  row.querySelector('.file-diff-name').addEventListener('click', () => {
    vscode.postMessage({ command: 'openProposedChange', filePath: filePath, fileContent: '' });
  });
  // Append inside the current agent group if one exists, else directly to feed
  (_currentAgentGroup || feed).appendChild(row);
  scrollFeed();
}

// Iteration separator
function addIterSep(label) {
  breakAgentGroup();
  const div = document.createElement('div');
  div.className = 'iter-sep fade-in';
  div.innerHTML =
    '<div class="iter-sep-line"></div>' +
    '<span class="iter-sep-label">' + escHtml(label) + '</span>' +
    '<div class="iter-sep-line"></div>';
  feed.appendChild(div);
  scrollFeed();
}

// Actions bar (save / undo buttons after run)
function showActionsBar(files, dir) {
  const bar = document.createElement('div');
  bar.className = 'actions-bar fade-in';
  bar.innerHTML = '<div class="actions-bar-title">Generated Files</div><div class="actions-row"></div>';
  const row = bar.querySelector('.actions-row');

  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn btn-success';
  saveBtn.textContent = 'Save to workspace';
  saveBtn.addEventListener('click', () => {
    vscode.postMessage({ command: 'saveFiles', files: files, projectDir: dir });
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saved';
  });

  const undoBtn = document.createElement('button');
  undoBtn.className = 'btn btn-secondary';
  undoBtn.textContent = 'Undo';
  undoBtn.addEventListener('click', () => {
    vscode.postMessage({ command: 'undoFiles', files: files, projectDir: dir });
    undoBtn.disabled = true;
    undoBtn.textContent = 'Undone - QA in progress...';
  });

  row.appendChild(saveBtn);
  row.appendChild(undoBtn);
  feed.appendChild(bar);
  scrollFeed();
}

// Inline diff preview for a single file
function showInlineDiff(filePath) {
  vscode.postMessage({ command: 'openFileDiff', filePath: filePath, fileContent: '' });
}

// QA verdict
function showVerdict(passed) {
  breakAgentGroup();
  const div = document.createElement('div');
  div.className = 'verdict fade-in ' + (passed ? 'pass' : 'fail');
  div.textContent = passed
    ? '\u2713 QA passed - code meets quality standards.'
    : '\u2717 QA did not pass - review issues above.';
  feed.appendChild(div);
  scrollFeed();
}

// Clarification card
const CLARIFY_PAGE_SIZE = 4;

// Auto-approve modal (injected once into body)
const tcModalOverlay = document.createElement('div');
tcModalOverlay.className = 'tc-modal-overlay';
tcModalOverlay.innerHTML =
  '<div class="tc-modal">' +
    '<div class="tc-modal-title"><span class="tc-shield">\uD83D\uDEE1</span>Enable auto-approve for QA tools?</div>' +
    '<div class="tc-modal-body">' +
      'All subsequent <strong>ruff</strong> and <strong>pytest</strong> commands ' +
      'in this run will execute automatically without asking.<br><br>' +
      'These are read-only analysis tools and do <strong>not</strong> modify your files.' +
    '</div>' +
    '<div class="tc-modal-actions">' +
      '<button class="tc-modal-cancel">Cancel</button>' +
      '<button class="tc-modal-enable">Enable</button>' +
    '</div>' +
  '</div>';
document.body.appendChild(tcModalOverlay);
tcModalOverlay.querySelector('.tc-modal-cancel').addEventListener('click', () => {
  tcModalOverlay.classList.remove('open');
  tcPending = null;
});
tcModalOverlay.querySelector('.tc-modal-enable').addEventListener('click', () => {
  tcModalOverlay.classList.remove('open');
  tcAutoApprove = true;
  if (tcPending) {
    _tcSettle(tcPending.card, tcPending.actionsEl, tcPending.toolCallId, 'run', true);
    tcPending = null;
  }
});

function showToolCallCard(data) {
  breakAgentGroup();
  const payload      = (data && data.payload) || {};
  const toolName     = payload.tool_name || 'tool';
  const cmdBin       = payload.command   || toolName;
  const rawArgs      = payload.args      || [];
  // args[0] is the bin path itself - real display args start at index 1
  const displayArgs  = rawArgs.length > 1 ? rawArgs.slice(1) : rawArgs;
  const toolCallId   = payload.tool_call_id;
  const cmdShort     = cmdBin.split('/').pop() || cmdBin;
  const displayCmd   = [cmdShort].concat(displayArgs).join(' ');

  const label = document.createElement('div');
  label.className = 'tool-call-label';
  label.innerHTML = 'Running <code>' + escHtml(displayCmd) + '</code>';
  feed.appendChild(label);

  const card = document.createElement('div');
  card.className = 'tool-call-card fade-in';
  card.dataset.toolName = toolName;
  card.innerHTML =
    '<div class="tc-header">' +
      '<span class="tc-term-icon">&#x2395;</span>' +
      'Run <span class="tc-shell-pill">zsh</span> command?' +
    '</div>' +
    '<div class="tc-command">' +
      '<span class="tc-cmd-name">' + escHtml(cmdShort) + '</span>' +
      (displayArgs.length ? ' <span class="tc-cmd-args">' + escHtml(displayArgs.join(' ')) + '</span>' : '') +
    '</div>' +
    '<div class="tc-actions">' +
      '<div class="tc-split">' +
        '<button class="tc-allow">Allow</button>' +
        '<button class="tc-chevron" title="More options">&#9660;</button>' +
        '<div class="tc-dropdown">' +
          '<div class="tc-dropdown-item" data-action="auto-approve">&#x2713;&ensp;Enable auto-approve</div>' +
        '</div>' +
      '</div>' +
      '<button class="tc-skip">Skip</button>' +
    '</div>';
  feed.appendChild(card);
  scrollFeed();

  const actionsEl = card.querySelector('.tc-actions');
  const chevron   = card.querySelector('.tc-chevron');
  const dropdown  = card.querySelector('.tc-dropdown');

  if (tcAutoApprove) {
    _tcSettle(card, actionsEl, toolCallId, 'run', true);
    return;
  }

  card.querySelector('.tc-allow').addEventListener('click', () => {
    dropdown.classList.remove('open');
    _tcSettle(card, actionsEl, toolCallId, 'run', false);
  });
  card.querySelector('.tc-skip').addEventListener('click', () => {
    dropdown.classList.remove('open');
    _tcSettle(card, actionsEl, toolCallId, 'skip', false);
  });
  chevron.addEventListener('click', (e) => {
    e.stopPropagation();
    dropdown.classList.toggle('open');
  });
  card.querySelector('[data-action="auto-approve"]').addEventListener('click', () => {
    dropdown.classList.remove('open');
    tcPending = { toolCallId, card, actionsEl };
    tcModalOverlay.classList.add('open');
  });
  document.addEventListener('click', () => dropdown.classList.remove('open'), { once: true });
}

function _tcSettle(card, actionsEl, toolCallId, action, isAuto) {
  actionsEl.querySelectorAll('button').forEach((b) => { b.disabled = true; b.style.opacity = '0.4'; });
  const badge = document.createElement('span');
  badge.className = 'tc-badge ' + (isAuto ? 'auto' : action === 'run' ? 'allowed' : 'skipped');
  badge.textContent = isAuto ? 'Auto-approved' : action === 'run' ? '\u2713 Allowed' : '\u00d7 Skipped';
  if (isAuto) {
    badge.title = 'Click to cancel auto-approve';
    badge.addEventListener('click', () => {
      tcAutoApprove = false;
      // Restore the actions area to Allow / Skip buttons (does NOT re-run the settled command)
      actionsEl.innerHTML =
        '<div class="tc-split">' +
          '<button class="tc-allow">Allow</button>' +
          '<button class="tc-chevron" title="More options">&#9660;</button>' +
          '<div class="tc-dropdown">' +
            '<div class="tc-dropdown-item" data-action="auto-approve">&#x2713;&ensp;Enable auto-approve</div>' +
          '</div>' +
        '</div>' +
        '<button class="tc-skip">Skip</button>';
      const chevron  = actionsEl.querySelector('.tc-chevron');
      const dropdown = actionsEl.querySelector('.tc-dropdown');
      actionsEl.querySelector('.tc-allow').addEventListener('click', () => {
        dropdown.classList.remove('open');
        _tcSettle(card, actionsEl, toolCallId, 'run', false);
      });
      actionsEl.querySelector('.tc-skip').addEventListener('click', () => {
        dropdown.classList.remove('open');
        _tcSettle(card, actionsEl, toolCallId, 'skip', false);
      });
      chevron.addEventListener('click', (e) => {
        e.stopPropagation();
        dropdown.classList.toggle('open');
      });
      actionsEl.querySelector('[data-action="auto-approve"]').addEventListener('click', () => {
        dropdown.classList.remove('open');
        tcPending = { toolCallId, card, actionsEl };
        tcModalOverlay.classList.add('open');
      });
      document.addEventListener('click', () => dropdown.classList.remove('open'), { once: true });
    });
  }
  actionsEl.appendChild(badge);
  vscode.postMessage({ command: 'submitToolResponse', runId: currentRunId, toolCallId, action });
}

function showClarificationCard(question, options) {
  breakAgentGroup();
  const opts = options && options.length > 0 ? options : [];
  let page = 0;
  let selectedOpt = null;
  const totalPages = opts.length > 0 ? Math.ceil(opts.length / CLARIFY_PAGE_SIZE) : 0;

  const card = document.createElement('div');
  card.className = 'clarify-card fade-in';

  const qEl = document.createElement('div');
  qEl.className = 'clarify-q';
  qEl.textContent = question;
  card.appendChild(qEl);

  const optsEl = document.createElement('div');
  optsEl.className = 'clarify-opts';
  card.appendChild(optsEl);

  function renderPage() {
    optsEl.innerHTML = '';
    const start = page * CLARIFY_PAGE_SIZE;
    const slice = opts.slice(start, start + CLARIFY_PAGE_SIZE);
    slice.forEach((opt, i) => {
      const globalIdx = start + i;
      const parts = opt.split('|');
      const label = parts[0].trim();
      const desc  = parts[1] ? parts[1].trim() : '';

      const row = document.createElement('div');
      row.className = 'clarify-opt' + (selectedOpt === globalIdx ? ' selected' : '');
      row.innerHTML =
        '<span class="clarify-opt-num">' + (globalIdx + 1) + '</span>' +
        '<span class="clarify-opt-body">' +
          '<span class="clarify-opt-label">' + escHtml(label) + '</span>' +
          (desc ? '<span class="clarify-opt-desc">' + escHtml(desc) + '</span>' : '') +
        '</span>' +
        '<span class="clarify-opt-check">&#10003;</span>';
      row.addEventListener('click', () => {
        selectedOpt = globalIdx;
        submitAnswer(label);
      });
      optsEl.appendChild(row);
    });
  }

  const freeRow = document.createElement('div');
  freeRow.className = 'clarify-free-row';
  const textArea = document.createElement('textarea');
  textArea.className = 'clarify-text';
  textArea.placeholder = opts.length ? 'Or type your own answer...' : 'Type your answer...';
  textArea.rows = 1;
  const sendBtnClarify = document.createElement('button');
  sendBtnClarify.className = 'btn btn-primary';
  sendBtnClarify.style.cssText = 'padding:4px 10px;font-size:11px;align-self:flex-end';
  sendBtnClarify.textContent = 'Send';
  sendBtnClarify.addEventListener('click', () => submitAnswer(textArea.value));
  textArea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitAnswer(textArea.value); }
  });
  freeRow.appendChild(textArea);
  freeRow.appendChild(sendBtnClarify);
  card.appendChild(freeRow);

  let prevBtn, nextBtn, pageLabel;
  if (totalPages > 1) {
    const footer = document.createElement('div');
    footer.className = 'clarify-footer';
    prevBtn = document.createElement('button');
    prevBtn.className = 'clarify-nav';
    prevBtn.innerHTML = '&#8249;';
    prevBtn.addEventListener('click', () => { if (page > 0) { page--; renderPage(); updateNav(); } });
    nextBtn = document.createElement('button');
    nextBtn.className = 'clarify-nav';
    nextBtn.innerHTML = '&#8250;';
    nextBtn.addEventListener('click', () => { if (page < totalPages - 1) { page++; renderPage(); updateNav(); } });
    pageLabel = document.createElement('span');
    pageLabel.className = 'clarify-page';
    footer.appendChild(prevBtn);
    footer.appendChild(nextBtn);
    footer.appendChild(pageLabel);
    card.appendChild(footer);
  }

  function updateNav() {
    if (!prevBtn) return;
    prevBtn.disabled = page === 0;
    nextBtn.disabled = page === totalPages - 1;
    pageLabel.textContent = (page + 1) + ' / ' + totalPages;
  }

  function submitAnswer(answer) {
    const trimmed = answer.trim();
    if (!trimmed) return;

    // Collapse the card to a summary
    const summary = document.createElement('div');
    summary.className = 'clarify-summary';
    summary.innerHTML =
      '<div class="cs-q">' + escHtml(question) + '</div>' +
      '<div class="cs-a">' + escHtml(trimmed) + '</div>';
    card.replaceWith(summary);

    vscode.postMessage({ command: 'submitClarification', runId: currentRunId, answer: trimmed });
    scrollFeed();
  }

  if (opts.length > 0) renderPage();
  updateNav();

  feed.appendChild(card);
  scrollFeed();
}

// Specs review card — approve or request revision via edited MD file
let _specsFilePath = null;         // set when view.ts confirms the file was written
let _pendingReviseFromFile = null; // { originalSpecs, settle } — set while waiting for readSpecsFile response

function showSpecsReviewCard(question, specs) {
  breakAgentGroup();
  const card = document.createElement('div');
  card.className = 'specs-review-card fade-in';

  // Header
  const header = document.createElement('div');
  header.className = 'specs-review-header';
  header.innerHTML =
    '<svg class="specs-review-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
      '<rect x="2" y="1" width="10" height="14" rx="1.5"/>' +
      '<line x1="4.5" y1="5" x2="9.5" y2="5"/>' +
      '<line x1="4.5" y1="8" x2="9.5" y2="8"/>' +
      '<line x1="4.5" y1="11" x2="7.5" y2="11"/>' +
    '</svg>' +
    '<span class="specs-review-title">Specifications Review</span>';
  card.appendChild(header);

  // Save the file immediately to workspace so user can see it right away
  vscode.postMessage({ command: 'saveSpecsFile', fileContent: specs });

  // Instruction
  const hint = document.createElement('div');
  hint.className = 'specs-review-hint';
  hint.innerHTML =
    'The specifications have been saved to <code>.jame/specs-review.md</code> in your workspace. ' +
    'Open the file, edit if needed, then confirm below.';
  card.appendChild(hint);

  // Two-button row: Open | Confirm
  const actions = document.createElement('div');
  actions.className = 'specs-review-actions';

  const openBtn = document.createElement('button');
  openBtn.className = 'btn btn-secondary';
  openBtn.innerHTML =
    '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0">' +
      '<path d="M9 1H4a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V5z"/>' +
      '<polyline points="9 1 9 5 13 5"/>' +
    '</svg>' +
    'Open file';
  openBtn.addEventListener('click', function() {
    // Pass the already-known path if available, else let view.ts derive it
    vscode.postMessage({ command: 'openSpecsFile', filePath: _specsFilePath, fileContent: specs });
  });

  const confirmBtn = document.createElement('button');
  confirmBtn.className = 'btn btn-success';
  confirmBtn.innerHTML =
    '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0">' +
      '<polyline points="2 8 6 12 14 4"/>' +
    '</svg>' +
    'Confirm';

  actions.appendChild(openBtn);
  actions.appendChild(confirmBtn);
  card.appendChild(actions);

  function settle(action, feedback) {
    openBtn.disabled = true;
    confirmBtn.disabled = true;
    const summary = document.createElement('div');
    summary.className = 'clarify-summary';
    summary.innerHTML =
      '<div class="cs-q">Specifications review</div>' +
      '<div class="cs-a">' + escHtml(action === 'approve'
        ? 'Confirmed — proceeding to code generation.'
        : 'Revision requested.') + '</div>';
    card.replaceWith(summary);
    vscode.postMessage({
      command: 'submitSpecsReview',
      runId: currentRunId,
      action,
      feedback: action === 'revise' ? (feedback || '') : '',
    });
    scrollFeed();
  }

  confirmBtn.addEventListener('click', function() {
    // If user opened and possibly edited the file, read it back and check for changes
    if (_specsFilePath) {
      confirmBtn.disabled = true;
      confirmBtn.textContent = 'Reading…';
      vscode.postMessage({ command: 'readSpecsFile', filePath: _specsFilePath });
      _pendingReviseFromFile = { originalSpecs: specs, settle };
    } else {
      // File was never opened — straight approve
      settle('approve', '');
    }
  });

  feed.appendChild(card);
  scrollFeed();
}

// Iteration review card — senior mode: proceed or inject instructions
function showIterationReviewCard(question) {
  breakAgentGroup();
  const card = document.createElement('div');
  card.className = 'iter-review-card fade-in';

  const header = document.createElement('div');
  header.className = 'iter-review-header';
  header.innerHTML =
    '<span class="iter-review-icon">&#128269;</span>' +
    '<span class="iter-review-title">QA Iteration Review</span>';
  card.appendChild(header);

  // Strip the sentinel prefix for display
  const displayText = question.replace(/^QA iteration review:\s*/i, '');
  const qEl = document.createElement('div');
  qEl.className = 'iter-review-body';
  qEl.innerHTML = renderMd(displayText);
  card.appendChild(qEl);

  const instrRow = document.createElement('div');
  instrRow.className = 'iter-review-instr-row';
  instrRow.style.display = 'none';
  const instrArea = document.createElement('textarea');
  instrArea.className = 'clarify-text';
  instrArea.placeholder = 'Type instructions for the developer (optional)…';
  instrArea.rows = 2;
  instrRow.appendChild(instrArea);
  card.appendChild(instrRow);

  const actions = document.createElement('div');
  actions.className = 'iter-review-actions';

  const proceedBtn = document.createElement('button');
  proceedBtn.className = 'btn btn-success';
  proceedBtn.textContent = '▶ Proceed automatically';

  const instrBtn = document.createElement('button');
  instrBtn.className = 'btn btn-secondary';
  instrBtn.textContent = '✎ Add instructions';

  const sendInstrBtn = document.createElement('button');
  sendInstrBtn.className = 'btn btn-primary';
  sendInstrBtn.textContent = 'Send';
  sendInstrBtn.style.display = 'none';

  actions.appendChild(proceedBtn);
  actions.appendChild(instrBtn);
  actions.appendChild(sendInstrBtn);
  card.appendChild(actions);

  function settle(answer) {
    proceedBtn.disabled = true;
    instrBtn.disabled = true;
    sendInstrBtn.disabled = true;
    instrArea.disabled = true;
    const summary = document.createElement('div');
    summary.className = 'clarify-summary';
    summary.innerHTML =
      '<div class="cs-q">QA Iteration Review</div>' +
      '<div class="cs-a">' + escHtml(answer === 'proceed' ? 'Proceeding with automatic fixes.' : 'Instructions queued: ' + answer) + '</div>';
    card.replaceWith(summary);
    vscode.postMessage({ command: 'submitClarification', runId: currentRunId, answer });
    scrollFeed();
  }

  proceedBtn.addEventListener('click', () => settle('proceed'));

  instrBtn.addEventListener('click', () => {
    instrRow.style.display = '';
    instrBtn.style.display = 'none';
    sendInstrBtn.style.display = '';
    instrArea.focus();
  });

  sendInstrBtn.addEventListener('click', () => {
    const txt = instrArea.value.trim();
    settle(txt || 'proceed');
  });

  instrArea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      const txt = instrArea.value.trim();
      settle(txt || 'proceed');
    }
  });

  feed.appendChild(card);
  scrollFeed();
}

// Send / stop
function send() {
  const text = inputEl.value.trim();
  const slashCmd = activeSlashCmd;
  // Empty input while running: do nothing (stop is on the stop button, not send)
  if (!text && !slashCmd) return;

  const mode = currentMode;
  const displayText = slashCmd ? slashCmd + (text ? ' ' + text : '') : text;

  if (promptHistory.length === 0 || promptHistory[promptHistory.length - 1] !== displayText) {
    promptHistory.push(displayText);
  }
  historyIndex = -1;
  historyDraft = '';

  if (isRunning) {
    if (!currentRunId) {
      addSysMsg('No active run to queue into.', 'warn');
      return;
    }
    addUserMessage(displayText);
    inputEl.value = '';
    inputEl.style.height = 'auto';
    updateComposerActions();
    clearSlashCommand();
    closeSuggestions();
    vscode.postMessage({ command: 'queuePrompt', runId: currentRunId, prompt: displayText });
    return;
  }

  generatedFiles = [];
  projectDir = null;
  currentIteration = 0;
  lastAgent = null;
  _lastRowAc = null;
  resetFilesPanel();

  addUserMessage(displayText);
  inputEl.value = '';
  inputEl.style.height = 'auto';
  clearSlashCommand();
  closeSuggestions();
  lockMode();
  setRunning(true);

  vscode.postMessage({ command: 'startRun', userRequest: displayText, mode, slashCommand: slashCmd });
}

function stop() {
  if (!currentRunId) return;
  vscode.postMessage({ command: 'cancelRun', runId: currentRunId });
  addSysMsg('Cancellation requested...', 'warn');
}

// Junior mode: show a card with learning objectives + Submit button.
// Junior works on the exercise files via the files panel, then hits Submit.
const _EXERCISE_ICON_SVG =
  '<svg class="exercise-icon-svg" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">' +
  '<rect x="1" y="2" width="14" height="12" rx="2" stroke="currentColor" stroke-width="1.4"/>' +
  '<path d="M4 6h5M4 9h8M4 12h6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>' +
  '<path d="M11 3V1m0 0l-1.5 1.5M11 1l1.5 1.5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>' +
  '</svg>';

let _exerciseBarSubmitBtn = null;

function showSubmitExerciseCard(objectives) {
  const bar = document.getElementById('exerciseBar');
  if (!bar) return;

  bar.innerHTML = '';
  bar.style.display = '';

  // Left: icon + label + objectives pill
  const left = document.createElement('div');
  left.className = 'ex-bar-left';

  const titleRow = document.createElement('div');
  titleRow.className = 'ex-bar-title-row';
  titleRow.innerHTML = _EXERCISE_ICON_SVG + '<span class="ex-bar-title">Exercise ready</span>';
  left.appendChild(titleRow);

  if (objectives.length > 0) {
    const objWrap = document.createElement('div');
    objWrap.className = 'ex-bar-objectives';
    objectives.forEach(function(obj) {
      const chip = document.createElement('span');
      chip.className = 'ex-bar-obj-chip';
      chip.textContent = obj;
      objWrap.appendChild(chip);
    });
    left.appendChild(objWrap);
  }

  bar.appendChild(left);

  // Right: submit button
  const right = document.createElement('div');
  right.className = 'ex-bar-right';

  const submitBtn = document.createElement('button');
  submitBtn.className = 'btn btn-primary ex-bar-submit-btn';
  submitBtn.textContent = 'Submit';
  _exerciseBarSubmitBtn = submitBtn;

  submitBtn.addEventListener('click', function() {
    submitBtn.disabled = true;
    submitBtn.textContent = 'Submitting…';
    vscode.postMessage({ command: 'submitExercise', runId: currentRunId });
  });

  right.appendChild(submitBtn);
  bar.appendChild(right);
}

function hideExerciseBar() {
  const bar = document.getElementById('exerciseBar');
  if (bar) bar.style.display = 'none';
  _exerciseBarSubmitBtn = null;
}

// Show validation result in the feed (readable, structured).
function showSubmitResult(passed, score, feedback) {
  // Re-enable the submit button so user can retry
  if (_exerciseBarSubmitBtn) {
    _exerciseBarSubmitBtn.disabled = false;
    _exerciseBarSubmitBtn.textContent = 'Submit again';
  }

  const card = document.createElement('div');
  card.className = 'exercise-result-card fade-in ' + (passed ? 'result-pass' : 'result-fail');

  // Score row
  const scoreRow = document.createElement('div');
  scoreRow.className = 'er-score-row';
  const scoreIcon = document.createElement('span');
  scoreIcon.className = 'er-score-icon';
  scoreIcon.innerHTML = passed
    ? '<svg viewBox="0 0 16 16"><path d="M3 8l3.5 3.5L13 4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>'
    : '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>';
  const scoreLabel = document.createElement('span');
  scoreLabel.className = 'er-score-label';
  scoreLabel.innerHTML = passed
    ? '<strong>Passed</strong>'
    : '<strong>Not yet — keep going</strong>';
  const scoreBadge = document.createElement('span');
  scoreBadge.className = 'er-score-badge';
  scoreBadge.textContent = score + ' / 100';
  scoreRow.appendChild(scoreIcon);
  scoreRow.appendChild(scoreLabel);
  scoreRow.appendChild(scoreBadge);
  card.appendChild(scoreRow);

  if (feedback) {
    const fb = document.createElement('div');
    fb.className = 'er-feedback';
    // Split sections by double-newline for better readability
    feedback.trim().split(/\n{2,}/).forEach(function(block, i) {
      if (i > 0) {
        const sep = document.createElement('div');
        sep.className = 'er-sep';
        fb.appendChild(sep);
      }
      const p = document.createElement('p');
      p.className = 'er-block';
      p.textContent = block.trim();
      fb.appendChild(p);
    });
    card.appendChild(fb);
  }

  if (!passed) {
    const continueBtn = document.createElement('button');
    continueBtn.className = 'btn btn-secondary er-continue-btn';
    continueBtn.textContent = 'Continue working';
    continueBtn.addEventListener('click', function() {
      continueBtn.remove();
    });
    card.appendChild(continueBtn);
  } else {
    hideExerciseBar();
  }

  feed.appendChild(card);
  scrollFeed();
}

// Queue-prompt: inject an instruction into the active run's developer queue.
function showQueuePromptForm() {
  if (!currentRunId || !isRunning) return;

  const card = document.createElement('div');
  card.className = 'queue-prompt-card fade-in';

  const header = document.createElement('div');
  header.className = 'queue-prompt-header';
  header.innerHTML =
    '<span class="queue-prompt-icon">&#128172;</span>' +
    '<span class="queue-prompt-title">Inject instruction into next developer iteration</span>';
  card.appendChild(header);

  const textRow = document.createElement('div');
  textRow.className = 'queue-prompt-text-row';
  const textArea = document.createElement('textarea');
  textArea.className = 'clarify-text';
  textArea.placeholder = 'Type instructions for the developer…';
  textArea.rows = 2;
  textRow.appendChild(textArea);
  card.appendChild(textRow);

  const actions = document.createElement('div');
  actions.className = 'queue-prompt-actions';

  const queueSendBtn = document.createElement('button');
  queueSendBtn.className = 'btn btn-primary';
  queueSendBtn.textContent = 'Queue';

  const queueCancelBtn = document.createElement('button');
  queueCancelBtn.className = 'btn btn-secondary';
  queueCancelBtn.textContent = 'Cancel';

  actions.appendChild(queueSendBtn);
  actions.appendChild(queueCancelBtn);
  card.appendChild(actions);

  function dismiss() { card.remove(); }

  queueSendBtn.addEventListener('click', function() {
    const prompt = textArea.value.trim();
    if (!prompt) { textArea.focus(); return; }
    const summary = document.createElement('div');
    summary.className = 'clarify-summary';
    summary.innerHTML =
      '<div class="cs-q">Queued instruction</div>' +
      '<div class="cs-a">' + escHtml(prompt) + '</div>';
    card.replaceWith(summary);
    vscode.postMessage({ command: 'queuePrompt', runId: currentRunId, prompt });
    scrollFeed();
  });

  queueCancelBtn.addEventListener('click', dismiss);

  textArea.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); queueSendBtn.click(); }
    if (e.key === 'Escape') dismiss();
  });

  feed.appendChild(card);
  scrollFeed();
  textArea.focus();
}

function newChat() {
  if (isRunning && currentRunId) {
    newChatConfirmEl.style.display = 'flex';
    return;
  }
  doNewChat();
}

function doNewChat() {
  if (isRunning && currentRunId) {
    vscode.postMessage({ command: 'cancelRun', runId: currentRunId });
  }

  currentRunId = null;
  generatedFiles = [];
  projectDir = null;
  currentIteration = 0;
  lastAgent = null;
  _lastRowAc = null;
  promptHistory = [];
  historyIndex = -1;
  historyDraft = '';
  clearSlashCommand();
  closeSuggestions();
  unlockMode();
  setJameTitle(_funMode);
  if (ws) { ws.close(); ws = null; }

  setRunning(false);
  clearProgress();
  resetFilesPanel();
  inputEl.value = '';
  inputEl.style.height = 'auto';

  hideExerciseBar();
  feed.innerHTML = '';
  const emptyDiv = document.createElement('div');
  emptyDiv.className = 'empty';
  emptyDiv.id = 'emptyState';
  emptyDiv.innerHTML =
    '<svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">' +
    '<path stroke-linecap="round" stroke-linejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 0 1-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 0 1 4.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0 1 12 15a9.065 9.065 0 0 0-6.23-.693L5 14.5m14.8.8 1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0 1 12 21a48.25 48.25 0 0 1-8.135-.687c-1.718-.293-2.3-2.379-1.067-3.61L5 14.5" /></svg>' +
    '<p>Describe what you want to build. The multi-agent pipeline will architect, code, and validate it.</p>';
  feed.appendChild(emptyDiv);

  vscode.setState(null);
  inputEl.focus();
}

// WebSocket / extension host message events
window.addEventListener('message', async (event) => {
  const msg = event.data;

  if (msg.command === 'system') {
    addSysMsg(msg.message, 'info');
    return;
  }

  if (msg.command === 'error') {
    addSysMsg('Error: ' + msg.message, 'err');
    setRunning(false);
    return;
  }

  if (msg.command === 'backendUrlResolved') {
    window._resolvedBackendUrl = msg.backendUrl;
    return;
  }

  if (msg.command === 'clearChat') {
    // Backend restarted (new instance_id) — clear stale session state
    if (!isRunning) {
      doNewChat();
    }
    return;
  }

  if (msg.command === 'runCreated') {
    currentRunId = msg.runId;
    addSysMsg('Run started [' + msg.runId.substring(0, 8) + ']', 'info');

    const effectiveUrl = window._resolvedBackendUrl || msg.backendUrl;
    const wsUrl = effectiveUrl
      .replace('http://', 'ws://')
      .replace('https://', 'wss://') + '/ws/runs/' + msg.runId;

    ws = new WebSocket(wsUrl);

    ws.onmessage = (evt) => {
      let data;
      try {
        data = JSON.parse(evt.data);
      } catch (e) {
        console.warn('[JAME] bad WS frame:', evt.data, e);
        return;
      }
      try {
        handleServerEvent(data);
      } catch (e) {
        console.error('[JAME] handleServerEvent threw:', e, data);
      }
    };

    ws.onclose = () => { setRunning(false); };

    ws.onerror = (e) => {
      console.error('[JAME] WebSocket error', e);
      addSysMsg('WebSocket error. Is the backend running?', 'err');
      setRunning(false);
    };
  }

  if (msg.command === 'filesSaved') {
    addSysMsg('Files saved to ' + msg.destDir, 'ok');
    return;
  }

  if (msg.command === 'allAccepted') {
    addSysMsg('All proposed changes accepted to workspace.', 'ok');
    return;
  }

  if (msg.command === 'allDiscarded') {
    addSysMsg('All proposed changes discarded.', 'warn');
    return;
  }

  if (msg.command === 'submitResult') {
    showSubmitResult(!!msg.passed, msg.score ?? 0, msg.feedback ?? '');
    return;
  }

  if (msg.command === 'specsFilePath') {
    // view.ts confirmed the specs MD file was written — store path so revise btn can read it
    _specsFilePath = msg.filePath;
    return;
  }

  if (msg.command === 'specsFileContent') {
    // view.ts read back the edited specs MD — resolve the pending revise
    if (_pendingReviseFromFile) {
      const { originalSpecs, settle } = _pendingReviseFromFile;
      _pendingReviseFromFile = null;
      const content = msg.content || '';
      // Use the edited content as feedback; if unchanged, note that
      const feedback = content.trim() === (originalSpecs || '').trim()
        ? 'Revision requested — no textual changes detected in the specs file.'
        : content;
      settle('revise', feedback);
    }
    return;
  }
});

const AGENT_DISPLAY = {
  'architect':          'Architect',
  'developer':          'Developer',
  'delivery_engineer':  'Delivery',
  'quality_engineer':   'Quality Engineer',
  'devops':             'DevOps',
  'exercise_generator': 'Exercise',
};

function handleServerEvent(data) {
  if (!data || typeof data !== 'object') return;
  const event = String(data.event || '');
  const rawAgent = String(data.agent || '');
  const agentDisplay = AGENT_DISPLAY[rawAgent] || rawAgent || 'System';

  console.log('[JAME event]', event, rawAgent, data.message);

  {
    const ts = new Date().toISOString().substring(11, 23);
    const phase = data.phase ? '[' + String(data.phase).toUpperCase() + ']' : '';
    const agent = rawAgent ? '[' + rawAgent.toUpperCase() + ']' : '';
    vscode.postMessage({ command: 'logEvent', line: ts + ' ' + agent + phase + ' ' + (data.message || event) });
  }

  if (event === 'run_started') {
    vscode.postMessage({ command: 'clearLogs' });
    vscode.postMessage({ command: 'logEvent', line: '\u2500'.repeat(60) });
    vscode.postMessage({ command: 'logEvent', line: '[JAME] Run started at ' + new Date().toISOString() });
    vscode.postMessage({ command: 'logEvent', line: '\u2500'.repeat(60) });
    addSysMsg('Orchestration started', 'info');
    tcAutoApprove = false;
    return;
  }

  if (event === 'tool_call') {
    showToolCallCard(data);
    return;
  }

  if (event === 'agent_update') {
    const msg = String(data.message || '').trim();
    const payload = (data.payload && typeof data.payload === 'object') ? data.payload : {};
    const thinking = String(payload.thinking || '');
    const phase = data.phase || payload.phase || null;

    // Tool result - append to the matching tool-call card
    if (payload.tool_result && typeof payload.tool_result === 'object') {
      const tr = payload.tool_result;
      const toolName = String(tr.tool_name || '');
      const exitCode = tr.exit_code;
      const output   = String(tr.output || '');
      let targetCard = null;
      feed.querySelectorAll('.tool-call-card').forEach((c) => {
        if (c.dataset.toolName === toolName) { targetCard = c; }
      });
      const resultEl = document.createElement('div');
      resultEl.className = 'tc-result' + (exitCode !== 0 ? ' fail' : '');
      resultEl.textContent =
        (exitCode === 0 ? '\u2705 Passed' : '\u274c Failed (exit ' + exitCode + ')') +
        (output ? '\n' + output.slice(0, 900) : '');
      if (targetCard) { targetCard.appendChild(resultEl); }
      scrollFeed();
      return;
    }

    if (!msg) return;

    if (
      (rawAgent === 'developer') &&
      /^Generated \d+ file\(s\)/i.test(msg)
    ) {
      return;
    }

    if (
      lastAgent &&
      lastAgent !== agentDisplay &&
      (lastAgent === 'Quality Engineer' || lastAgent === 'QA' || lastAgent === 'quality_engineer') &&
      (agentDisplay === 'Developer' || rawAgent === 'developer')
    ) {
      currentIteration++;
      _lastRowAc = null;
      addIterSep('QA feedback - revision ' + currentIteration);
    }

    lastAgent = agentDisplay;
    addTlRow(agentDisplay, msg, thinking, phase);
    return;
  }

  if (event === 'file_generated') {
    const p = data.payload;
    if (p && p.path) {
      if (currentMode === 'junior') {
        // Junior mode: only show files produced by the exercise packager (after stripping).
        // Developer node files are hidden — the junior sees only the stub version.
        if (data.agent === 'exercise_generator') {
          generatedFiles.push(p.path);
          addFileToPanelExercise(p.path, p.content || '');
        }
        // else: silently ignore developer-node file events in junior mode
      } else {
        generatedFiles.push(p.path);
        vscode.postMessage({ command: 'openProposedChange', filePath: p.path, fileContent: p.content || '' });
        addFileToPanel(p.path, p.content || '');
      }
    }
    return;
  }

  if (event === 'files_ready') {
    const p = data.payload || {};
    if (Array.isArray(p.generated_files) && p.generated_files.length > 0) {
      filesPanel.classList.add('visible');
    }
    return;
  }

  if (event === 'exercise_ready') {
    const p = data.payload || {};
    const objectives = Array.isArray(p.learning_objectives) ? p.learning_objectives : [];
    addSysMsg('Learning exercise ready' + (objectives.length ? ' (' + objectives.length + ' objective(s)).' : '.'), 'ok');
    // Open the files panel so the junior can see and work on the exercise stubs
    filesPanel.classList.add('visible');
    // Unlock the UI — junior run is in AWAITING_SUBMISSION, not COMPLETED
    setRunning(false);
    // Show a submit card so the junior can submit their implementation
    showSubmitExerciseCard(objectives);
    return;
  }

  if (event === 'clarification_request') {
    const p = data.payload || {};
    const question = p.question || data.message || 'Please clarify:';
    const options = p.options || [];
    showClarificationCard(question, options);
    return;
  }

  if (event === 'specs_review_request') {
    const p = data.payload || {};
    const question = p.question || data.message || 'Review specifications:';
    const specs = p.specs || '';
    showSpecsReviewCard(question, specs);
    return;
  }

  if (event === 'iteration_review_request') {
    const p = data.payload || {};
    const question = p.question || data.message || 'QA iteration review:';
    showIterationReviewCard(question);
    return;
  }

  if (event === 'prompt_queued') {
    addSysMsg('Instruction queued for next developer iteration.', 'ok');
    return;
  }

  if (event === 'run_completed') {
    const p = data.payload || {};
    projectDir = p.project_dir || null;
    const qaPassed = !!p.qa_passed;
    setFilesPanelVerdict(qaPassed);
    setRunning(false);
    clearProgress();
    if (ws) ws.close();
    return;
  }

  if (event === 'run_failed') {
    const err = (data.payload && data.payload.error) || data.message || 'Unknown error';
    const firstLine = err.split('\n')[0];
    addSysMsg('Build failed: ' + firstLine, 'err');
    setRunning(false);
    if (ws) ws.close();
    return;
  }

  if (event === 'run_cancelled') {
    addSysMsg('Build cancelled.', 'warn');
    setRunning(false);
    if (ws) ws.close();
    return;
  }

  if (event === 'awaiting_submission') {
    addSysMsg(data.message || 'Pipeline complete - waiting for your implementation.', 'ok');
    setRunning(false);
    clearProgress();
    if (ws) ws.close();
    return;
  }

  console.warn('[JAME] unrecognized event:', event, data);
}

// Focus on load
inputEl.focus();
