'use strict';

// ── Session constants ──────────────────────────────
const MAX_TURNS = 15;   // show WhatsApp card after this many user turns (if no booking)
const WA_NUMBER = '447341650417';
const WA_TEXT   = encodeURIComponent('Hi! I need help with my property enquiry.');

// ── Session ID (persists across page loads) ────────
function _getOrCreateSessionId() {
  let id = localStorage.getItem('ep_sid');
  if (!id) {
    id = 'ep_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 8);
    localStorage.setItem('ep_sid', id);
  }
  return id;
}
const SESSION_ID = _getOrCreateSessionId();

// ── State ──────────────────────────────────────────
let conversationHistory = [];
let selectedImage = null;
let turnCount   = parseInt(localStorage.getItem('ep_turns') || '0', 10);
let bookingDone = localStorage.getItem('ep_booked') === '1';

// ── Persist state to localStorage ─────────────────
function _persist() {
  try {
    localStorage.setItem('ep_history', JSON.stringify(conversationHistory));
    localStorage.setItem('ep_turns',   String(turnCount));
    localStorage.setItem('ep_booked',  bookingDone ? '1' : '0');
  } catch (e) { /* storage full — ignore */ }
}

// ── Clear session / start new chat ─────────────────
function clearSession() {
  ['ep_sid', 'ep_history', 'ep_turns', 'ep_booked'].forEach(k => localStorage.removeItem(k));
  location.reload();
}

// ── Voice input (Web Speech API — free, Chrome/Edge) ─
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
let isRecording = false;

function initVoice() {
  const btn = document.getElementById('micBtn');
  if (!SpeechRecognition) { btn.style.display = 'none'; return; }

  recognition = new SpeechRecognition();
  recognition.lang           = 'en-GB';
  recognition.continuous     = false;
  recognition.interimResults = true;

  recognition.onstart = () => {
    isRecording = true;
    btn.classList.add('recording');
    document.getElementById('messageInput').placeholder = '🎙️ Listening…';
  };
  recognition.onresult = (e) => {
    const transcript = Array.from(e.results).map(r => r[0].transcript).join('');
    const input = document.getElementById('messageInput');
    input.value = transcript;
    autoResize(input);
  };
  recognition.onend = () => {
    isRecording = false;
    btn.classList.remove('recording');
    document.getElementById('messageInput').placeholder = 'Describe your property issue or ask a question…';
    const input = document.getElementById('messageInput');
    if (input.value.trim()) input.focus();
  };
  recognition.onerror = (e) => {
    isRecording = false;
    btn.classList.remove('recording');
    document.getElementById('messageInput').placeholder = 'Describe your property issue or ask a question…';
    if (e.error === 'not-allowed')
      alert('Microphone access denied. Please allow microphone in your browser settings and try again.');
  };
}

function toggleVoice() {
  if (!recognition) return;
  if (isRecording) recognition.stop();
  else { document.getElementById('messageInput').value = ''; recognition.start(); }
}

// ── Auto-resize textarea ───────────────────────────
function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

// ── Enter to send (Shift+Enter = new line) ─────────
function handleKeyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}

// ── Image handling ─────────────────────────────────
function handleImageSelect(e) {
  const file = e.target.files[0];
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) {
    alert('Image is too large (max 20 MB). Please compress it and try again.');
    e.target.value = '';
    return;
  }
  const reader = new FileReader();
  reader.onload = (ev) => {
    const dataUrl = ev.target.result;
    selectedImage = { base64: dataUrl.split(',')[1], mimeType: file.type || 'image/jpeg', dataUrl };
    document.getElementById('previewImg').src = dataUrl;
    document.getElementById('imagePreview').classList.add('visible');
  };
  reader.readAsDataURL(file);
}

function removeImage() {
  selectedImage = null;
  document.getElementById('imagePreview').classList.remove('visible');
  document.getElementById('imageInput').value = '';
}

// ── Date / Slot pickers ────────────────────────────
function removePickers() {
  ['datePicker', 'slotPicker'].forEach(id => { const el = document.getElementById(id); if (el) el.remove(); });
}

function getNext8WorkingDays() {
  const SHORT_DAY = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const SHORT_MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const FULL_DAY  = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
  const FULL_MON  = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  const result = [];
  const d = new Date();
  d.setDate(d.getDate() + 1);
  while (result.length < 8) {
    if (d.getDay() !== 0) {
      const dd   = String(d.getDate()).padStart(2, '0');
      const mm   = String(d.getMonth() + 1).padStart(2, '0');
      const yyyy = d.getFullYear();
      result.push({
        day:   SHORT_DAY[d.getDay()],
        short: `${d.getDate()} ${SHORT_MON[d.getMonth()]}`,
        full:  `${FULL_DAY[d.getDay()]}, ${dd} ${FULL_MON[d.getMonth()]} ${yyyy}`,
        iso:   `${yyyy}-${mm}-${dd}`
      });
    }
    d.setDate(d.getDate() + 1);
  }
  return result;
}

function showDatePicker() {
  if (document.getElementById('datePicker')) return;
  const chatArea = document.getElementById('chatArea');
  const row = document.createElement('div');
  row.id = 'datePicker';
  row.className = 'picker-row';
  getNext8WorkingDays().forEach(d => {
    const btn = document.createElement('button');
    btn.className = 'picker-btn';
    btn.innerHTML = `<span class="picker-main">${d.day}</span><span class="picker-sub">${d.short}</span>`;
    btn.onclick = () => { removePickers(); document.getElementById('messageInput').value = d.full; sendMessage(); };
    row.appendChild(btn);
  });
  const other = document.createElement('button');
  other.className = 'picker-btn picker-other';
  other.innerHTML = `<span class="picker-main">✏️</span><span class="picker-sub">Other</span>`;
  other.onclick = () => { removePickers(); document.getElementById('messageInput').focus(); };
  row.appendChild(other);
  chatArea.appendChild(row);
  scrollToBottom();
}

function showSlotPicker(slots) {
  removePickers();
  const chatArea = document.getElementById('chatArea');
  const row = document.createElement('div');
  row.id = 'slotPicker';
  row.className = 'picker-row';
  slots.forEach(slot => {
    const [h, m] = slot.split(':');
    const hour = parseInt(h);
    const ampm = hour >= 12 ? 'PM' : 'AM';
    const h12  = hour % 12 || 12;
    const btn  = document.createElement('button');
    btn.className = 'picker-btn slot-btn';
    btn.innerHTML = `<span class="picker-main">${h12}:${m}</span><span class="picker-sub">${ampm}</span>`;
    btn.onclick = () => { removePickers(); document.getElementById('messageInput').value = slot; sendMessage(); };
    row.appendChild(btn);
  });
  chatArea.appendChild(row);
  scrollToBottom();
}

// ── Quick suggestion chips ─────────────────────────
function sendChip(el) {
  const chips = document.getElementById('quickChips');
  if (chips) chips.style.display = 'none';
  const msg = el.dataset.msg || el.textContent.replace(/^[\p{Emoji}\s]+/u, '').trim();
  document.getElementById('messageInput').value = msg;
  sendMessage();
}

// ── Scroll to bottom ───────────────────────────────
function scrollToBottom() {
  const area = document.getElementById('chatArea');
  area.scrollTop = area.scrollHeight;
}

// ── Create a bot bubble for streaming ──────────────
function createStreamingBubble() {
  const chatArea = document.getElementById('chatArea');
  const row = document.createElement('div');
  row.className = 'message-row';
  const avatar = document.createElement('div');
  avatar.className = 'bot-avatar';
  avatar.innerHTML = '<span>EP</span>';
  const bubble = document.createElement('div');
  bubble.className = 'message-bubble bot-bubble';
  bubble.innerHTML = '<span class="cursor-blink">▍</span>';
  row.appendChild(avatar);
  row.appendChild(bubble);
  chatArea.appendChild(row);
  scrollToBottom();
  return bubble;
}

// ── Append a complete message bubble ───────────────
function appendMessage(role, content, imageDataUrl = null) {
  const chatArea = document.getElementById('chatArea');
  const row = document.createElement('div');
  row.className = 'message-row' + (role === 'user' ? ' user-row' : '');
  if (role === 'assistant') {
    const avatar = document.createElement('div');
    avatar.className = 'bot-avatar';
    avatar.innerHTML = '<span>EP</span>';
    row.appendChild(avatar);
  }
  const bubble = document.createElement('div');
  bubble.className = 'message-bubble ' + (role === 'user' ? 'user-bubble' : 'bot-bubble');
  if (imageDataUrl) {
    const img = document.createElement('img');
    img.src = imageDataUrl;
    img.className = 'chat-image';
    img.alt = 'Attached property photo';
    bubble.appendChild(img);
    if (content) { const p = document.createElement('p'); p.textContent = content; bubble.appendChild(p); }
  } else if (role === 'assistant') {
    bubble.innerHTML = renderMarkdown(content);
  } else {
    const p = document.createElement('p');
    p.textContent = content;
    bubble.appendChild(p);
  }
  row.appendChild(bubble);
  chatArea.appendChild(row);
  scrollToBottom();
  return bubble;
}

// ── Typing indicator ───────────────────────────────
function showTyping() {
  const chatArea = document.getElementById('chatArea');
  const row = document.createElement('div');
  row.className = 'typing-row';
  row.id = 'typingIndicator';
  const avatar = document.createElement('div');
  avatar.className = 'bot-avatar';
  avatar.innerHTML = '<span>EP</span>';
  const bubble = document.createElement('div');
  bubble.className = 'typing-bubble';
  bubble.innerHTML = '<span></span><span></span><span></span>';
  row.appendChild(avatar);
  row.appendChild(bubble);
  chatArea.appendChild(row);
  scrollToBottom();
}

function hideTyping() {
  const el = document.getElementById('typingIndicator');
  if (el) el.remove();
}

// ── Markdown renderer ──────────────────────────────
function applyInline(text) {
  return text
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.*?)\*/g, '<em>$1</em>')
    .replace(
      /(\b0[0-9]{2,4}[\s]?[0-9]{3,4}[\s]?[0-9]{3,4}\b)/g,
      '<a href="tel:$1" class="phone-link">$1</a>'
    );
}

function renderMarkdown(raw) {
  let text = raw
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  const lines = text.split('\n');
  const out = [];
  let listType = null;
  function closeList() { if (listType) { out.push(`</${listType}>`); listType = null; } }
  for (const line of lines) {
    if (line.startsWith('### ')) { closeList(); out.push(`<h4>${applyInline(line.slice(4))}</h4>`); continue; }
    if (line.startsWith('## '))  { closeList(); out.push(`<h3>${applyInline(line.slice(3))}</h3>`); continue; }
    if (/^[-•*] /.test(line)) {
      if (listType !== 'ul') { closeList(); out.push('<ul>'); listType = 'ul'; }
      out.push(`<li>${applyInline(line.replace(/^[-•*] /, ''))}</li>`);
      continue;
    }
    if (/^\d+\.\s/.test(line)) {
      if (listType !== 'ol') { closeList(); out.push('<ol>'); listType = 'ol'; }
      out.push(`<li>${applyInline(line.replace(/^\d+\.\s/, ''))}</li>`);
      continue;
    }
    closeList();
    line.trim() === '' ? out.push('<br>') : out.push(`<p>${applyInline(line)}</p>`);
  }
  closeList();
  return out.join('');
}

// ── WhatsApp escalation card ───────────────────────
function showWhatsAppEscalation() {
  if (document.getElementById('waEscalation')) return;
  const chatArea = document.getElementById('chatArea');
  const row = document.createElement('div');
  row.className = 'message-row';
  row.id = 'waEscalation';
  const avatar = document.createElement('div');
  avatar.className = 'bot-avatar';
  avatar.innerHTML = '<span>EP</span>';
  const bubble = document.createElement('div');
  bubble.className = 'message-bubble bot-bubble wa-bubble';
  bubble.innerHTML = `
    <p>💬 <strong>Prefer to speak with someone?</strong><br>Our team is available Mon–Sat, 9 AM–6 PM.</p>
    <a class="wa-btn" href="https://wa.me/${WA_NUMBER}?text=${WA_TEXT}" target="_blank" rel="noopener noreferrer">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z"/></svg>
      Chat on WhatsApp
    </a>
  `;
  row.appendChild(avatar);
  row.appendChild(bubble);
  chatArea.appendChild(row);
  scrollToBottom();
}

// ── Send message ───────────────────────────────────
async function sendMessage() {
  const input = document.getElementById('messageInput');
  const message = input.value.trim();
  if (!message && !selectedImage) return;

  const sendBtn = document.getElementById('sendBtn');
  sendBtn.disabled = true;

  // Hide chips
  const chips = document.getElementById('quickChips');
  if (chips) chips.style.display = 'none';

  // Show user message
  appendMessage('user', message || '', selectedImage?.dataUrl);

  // Capture & clear image
  const imgPayload = selectedImage
    ? { image_base64: selectedImage.base64, image_mime_type: selectedImage.mimeType }
    : {};
  removeImage();

  // Clear input
  input.value = '';
  input.style.height = 'auto';

  const effectiveMessage = message || 'I have attached a photo of my property issue. Please analyse it.';

  // Increment turn counter and persist
  turnCount++;
  _persist();

  showTyping();

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: effectiveMessage,
        ...imgPayload,
        history: conversationHistory,
        session_id: SESSION_ID
      }),
    });

    if (!res.ok) throw new Error(`Server error ${res.status}`);

    hideTyping();

    const bubble = createStreamingBubble();
    let fullText = '';
    let buffer = '';

    const reader = res.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const payload = line.slice(6).trim();
        if (payload === '[DONE]') continue;
        try {
          const parsed = JSON.parse(payload);
          if (parsed.error) throw new Error(parsed.error);
          if (parsed.ui === 'slots') { showSlotPicker(parsed.slots); continue; }
          if (parsed.token) {
            fullText += parsed.token;
            bubble.innerHTML = renderMarkdown(fullText) + '<span class="cursor-blink">▍</span>';
            scrollToBottom();
          }
        } catch (parseErr) {
          if (parseErr.message && !parseErr.message.startsWith('JSON')) throw parseErr;
        }
      }
    }

    // Finalise bubble
    bubble.innerHTML = renderMarkdown(fullText);

    // Show date picker if bot asks for a day
    const DATE_TRIGGERS = ['which day','what day','when would you','day works','day suits',
                           'prefer a day','choose a day','pick a day','what date','which date'];
    if (DATE_TRIGGERS.some(t => fullText.toLowerCase().includes(t))) showDatePicker();

    // Booking confirmed — remove pickers, mark done
    if (fullText.includes("You're booked") || fullText.includes("you're booked") || fullText.includes('✅')) {
      removePickers();
      bookingDone = true;
    }

    // Update history — tag image messages so context is preserved in follow-up turns
    const historyContent = imgPayload.image_base64
      ? `[Photo attached] ${effectiveMessage}`
      : effectiveMessage;
    conversationHistory.push({ role: 'user', content: historyContent });
    conversationHistory.push({ role: 'assistant', content: fullText });
    if (conversationHistory.length > 20) conversationHistory = conversationHistory.slice(-20);
    _persist();

    // Show WhatsApp escalation after MAX_TURNS if no booking yet
    if (turnCount >= MAX_TURNS && !bookingDone) showWhatsAppEscalation();

  } catch (err) {
    hideTyping();
    appendMessage('assistant',
      '**Sorry, I ran into an error.** Please try again or reach us directly:\n\n' +
      '📞 **Phone:** 0203 935 1596\n' +
      '💬 **WhatsApp:** +44 7341 650417\n' +
      '✉️ **Email:** service@environpropertyservices.co.uk'
    );
    console.error('Chat error:', err);
  }

  sendBtn.disabled = false;
  input.focus();
}

// ── Restore previous session from localStorage ─────
function restoreSession() {
  const raw = localStorage.getItem('ep_history');
  if (!raw) return;
  try {
    const history = JSON.parse(raw);
    if (!history.length) return;
    conversationHistory = history;
    // Hide quick chips — returning user
    const chips = document.getElementById('quickChips');
    if (chips) chips.style.display = 'none';
    // Render all previous messages
    history.forEach(m => appendMessage(m.role === 'user' ? 'user' : 'assistant', m.content));
    // Re-show escalation card if limit was already hit
    if (turnCount >= MAX_TURNS && !bookingDone) showWhatsAppEscalation();
    scrollToBottom();
  } catch (e) {
    localStorage.removeItem('ep_history');
  }
}

// ── Initialise ─────────────────────────────────────
restoreSession();
initVoice();
