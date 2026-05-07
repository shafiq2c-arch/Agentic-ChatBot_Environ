'use strict';

let conversationHistory = [];
let selectedImage = null;

// ── Auto-resize textarea ──────────────────────────
function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

// ── Enter to send (Shift+Enter = new line) ────────
function handleKeyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

// ── Image handling ────────────────────────────────
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
    selectedImage = {
      base64: dataUrl.split(',')[1],
      mimeType: file.type || 'image/jpeg',
      dataUrl,
    };
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

// ── Quick suggestion chips ────────────────────────
function sendChip(el) {
  const chips = document.getElementById('quickChips');
  if (chips) chips.style.display = 'none';
  const msg = el.dataset.msg || el.textContent.replace(/^[\p{Emoji}\s]+/u, '').trim();
  document.getElementById('messageInput').value = msg;
  sendMessage();
}

// ── Scroll to bottom ──────────────────────────────
function scrollToBottom() {
  const area = document.getElementById('chatArea');
  area.scrollTop = area.scrollHeight;
}

// ── Create a bot bubble for streaming ────────────
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

// ── Append a complete message bubble ─────────────
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
    if (content) {
      const p = document.createElement('p');
      p.textContent = content;
      bubble.appendChild(p);
    }
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

// ── Typing indicator (while waiting for first token) ──
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

// ── Markdown renderer ─────────────────────────────
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

  function closeList() {
    if (listType) { out.push(`</${listType}>`); listType = null; }
  }

  for (const line of lines) {
    if (line.startsWith('### ')) {
      closeList();
      out.push(`<h4>${applyInline(line.slice(4))}</h4>`);
      continue;
    }
    if (line.startsWith('## ')) {
      closeList();
      out.push(`<h3>${applyInline(line.slice(3))}</h3>`);
      continue;
    }
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
    line.trim() === ''
      ? out.push('<br>')
      : out.push(`<p>${applyInline(line)}</p>`);
  }

  closeList();
  return out.join('');
}

// ── Send message ──────────────────────────────────
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

  showTyping();

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: effectiveMessage, ...imgPayload, history: conversationHistory }),
    });

    if (!res.ok) throw new Error(`Server error ${res.status}`);

    hideTyping();

    // Create streaming bubble
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
      buffer = lines.pop(); // keep incomplete line in buffer

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const payload = line.slice(6).trim();
        if (payload === '[DONE]') continue;

        try {
          const parsed = JSON.parse(payload);
          if (parsed.error) throw new Error(parsed.error);
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

    // Finalise — remove cursor, render clean markdown
    bubble.innerHTML = renderMarkdown(fullText);

    // Update history
    conversationHistory.push({ role: 'user', content: effectiveMessage });
    conversationHistory.push({ role: 'assistant', content: fullText });
    if (conversationHistory.length > 20) conversationHistory = conversationHistory.slice(-20);

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
