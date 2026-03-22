// ── Hive markdown rendering ──
// Requires: marked.js, DOMPurify (loaded before this script)

const IFRAME_ALLOW = [
  'youtube.com', 'www.youtube.com',
  'youtube-nocookie.com', 'www.youtube-nocookie.com',
  'player.vimeo.com',
  'play.3speak.tv',
  'www.instagram.com',
  'www.skatehype.com',
  'odysee.com',
  'rumble.com',
  'open.spotify.com',
  'w.soundcloud.com',
  'emb.d.tube',
  'lbry.tv',
];

const ALLOWED_CSS_PROPS = new Set([
  'position', 'padding-bottom', 'height', 'overflow',
  'top', 'left', 'width', 'max-width',
]);

// Global delegated error handler for image proxy fallback.
// onerror function properties don't survive innerHTML serialization,
// so we use data-direct-src attributes + capture-phase listener instead.
document.addEventListener('error', function(e) {
  const img = e.target;
  if (img.tagName === 'IMG' && img.dataset.directSrc) {
    img.src = img.dataset.directSrc;
    delete img.dataset.directSrc;
  }
}, true);

// Raw-text HTML elements that break marked's parsing by swallowing all
// subsequent content as literal text until their closing tag appears.
const _RAW_TEXT_TAGS = /<\/?(?:textarea|xmp|plaintext|style|noscript|noembed|noframes|title|script|html|head|body)\b[^>]*>/gi;

function normalizeMarkdown(md) {
  // Strip raw-text HTML element tags before marked sees them,
  // but only outside code fences so code examples are preserved.
  let inFence = false;
  const fenced = [];
  md = md.split('\n').map(line => {
    if (/^```/.test(line.trim())) inFence = !inFence;
    fenced.push(inFence);
    return inFence ? line : line.replace(_RAW_TEXT_TAGS, '');
  }).join('\n');
  // Also strip structural tags split across lines (e.g. <html\n>)
  // that start HTML blocks in marked. Only outside code fences.
  md = md.replace(/<\/?\s*(?:html|head|body)\b[^>]*(?:\n[^>]*)*>/gi, (m, offset) => {
    const lineNum = md.slice(0, offset).split('\n').length - 1;
    return fenced[lineNum] ? m : '';
  });
  // @mentions → links
  md = md.replace(/(^|[\s(])@([a-z0-9][a-z0-9.-]{1,15})(?=[\s,.;:!?)}\]]|$)/gim,
    '$1[@$2](https://peakd.com/@$2)');
  // Bare image URLs with [Source] suffix
  md = md.replace(/(https?:\/\/[^\s<>"\[\]]+\.(?:jpe?g|png|gif|webp|svg)(?:\?[^\s<>"\[\]]*)?)\[(?:Source|source)\]\(([^)]+)\)/gim,
    '![]($1)');
  // Bare image URLs on their own line
  md = md.replace(/(^|>|\n)\s*(https?:\/\/[^\s<>"]+\.(?:jpe?g|png|gif|webp|svg)(?:\?[^\s<>"]*)?)\s*(?=<|\n|$)/gim,
    '$1\n![]($2)\n');
  // Convert all markdown images to <img> before marked parses.
  // Also fixes images inside HTML blocks (e.g. <center>) which marked treats as raw text.
  md = md.replace(/!\[([^\]]*)\]\((https?:\/\/[^)]+)\)/gim, '<img alt="$1" src="$2">');
  // Convert [<img src="...">](url) to <a><img></a>
  md = md.replace(/\[(<img\s[^>]*>)\]\((https?:\/\/[^)]+)\)/gim, '<a href="$2">$1</a>');
  // Convert remaining markdown links to <a> — marked skips these inside HTML blocks
  md = md.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/gim, '<a href="$2">$1</a>');
  return md;
}

function sanitizeContent(html) {
  DOMPurify.addHook('uponSanitizeElement', (node, data) => {
    if (data.tagName === 'iframe') {
      const src = node.getAttribute('src') || '';
      try {
        const host = new URL(src).hostname;
        if (!IFRAME_ALLOW.some(d => host === d || host.endsWith('.' + d))) {
          node.remove();
        }
      } catch {
        node.remove();
      }
    }
  });
  const clean = DOMPurify.sanitize(html, {
    ALLOWED_TAGS: [
      'p','br','strong','b','em','i','u','s','del','strike',
      'h1','h2','h3','h4','h5','h6',
      'ul','ol','li',
      'blockquote','pre','code',
      'a','img','iframe',
      'table','thead','tbody','tr','th','td',
      'hr','div','span','sub','sup',
      'center',
    ],
    ALLOWED_ATTR: ['href','src','alt','title','class','id','width','height',
                   'style','allowfullscreen','frameborder'],
    ALLOW_DATA_ATTR: false,
    ADD_ATTR: ['target', 'data-direct-src'],
  });
  DOMPurify.removeAllHooks();
  return clean;
}

function proxyImages(div) {
  div.querySelectorAll('img').forEach(img => {
    const src = img.getAttribute('src') || '';
    if (src && !src.startsWith('data:')) {
      const fixedSrc = src.replace(/^https?:\/\/(?:cdn\.)?steemitimages\.com\//, 'https://images.hive.blog/');
      if (fixedSrc !== src) img.src = fixedSrc;
      img.dataset.directSrc = fixedSrc;
      if (!/images\.hive\.blog/.test(fixedSrc)) {
        img.src = 'https://images.hive.blog/768x0/' + fixedSrc;
      }
    }
    img.removeAttribute('width');
    img.removeAttribute('height');
    img.loading = 'lazy';
  });
}

function embedVideos(div) {
  const embeddedVideos = new Set();
  div.querySelectorAll('a').forEach(a => {
    const href = a.getAttribute('href') || '';
    const ytMatch = href.match(/(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]+)/);
    if (ytMatch) {
      const videoKey = `yt:${ytMatch[1]}`;
      if (embeddedVideos.has(videoKey)) {
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer');
        return;
      }
      embeddedVideos.add(videoKey);
      const embed = document.createElement('div');
      embed.className = 'video-embed';
      const iframe = document.createElement('iframe');
      iframe.src = `https://www.youtube.com/embed/${encodeURIComponent(ytMatch[1])}`;
      iframe.allowFullscreen = true;
      iframe.allow = 'autoplay';
      iframe.loading = 'lazy';
      embed.appendChild(iframe);
      a.replaceWith(embed);
      return;
    }
    const tsMatch = href.match(/3speak\.tv\/watch\?v=([\w.-]+)\/([\w-]+)/);
    if (tsMatch) {
      const videoKey = `3s:${tsMatch[1]}/${tsMatch[2]}`;
      if (embeddedVideos.has(videoKey)) {
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer');
        return;
      }
      embeddedVideos.add(videoKey);
      const embed = document.createElement('div');
      embed.className = 'video-embed';
      const iframe = document.createElement('iframe');
      iframe.src = `https://play.3speak.tv/watch?v=${encodeURIComponent(tsMatch[1])}/${encodeURIComponent(tsMatch[2])}&mode=iframe&layout=desktop`;
      iframe.allowFullscreen = true;
      iframe.allow = 'autoplay';
      iframe.loading = 'lazy';
      iframe.sandbox = 'allow-scripts allow-same-origin allow-popups';
      embed.appendChild(iframe);
      a.replaceWith(embed);
      return;
    }
    const igMatch = href.match(/instagram\.com\/reel\/([\w-]+)/);
    if (igMatch) {
      const videoKey = `ig:${igMatch[1]}`;
      if (embeddedVideos.has(videoKey)) {
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer');
        return;
      }
      embeddedVideos.add(videoKey);
      const embed = document.createElement('div');
      embed.className = 'video-embed ig-embed';
      const iframe = document.createElement('iframe');
      iframe.src = `https://www.instagram.com/reel/${encodeURIComponent(igMatch[1])}/embed/`;
      iframe.allowFullscreen = true;
      iframe.loading = 'lazy';
      iframe.sandbox = 'allow-scripts allow-same-origin allow-popups';
      embed.appendChild(iframe);
      a.replaceWith(embed);
      return;
    }
    if (href.startsWith('#')) {
      // Anchor link — keep as-is (delegated handler on modal-body scrolls to target)
      a.removeAttribute('target');
      a.removeAttribute('rel');
      return;
    }
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
  });
}

function sandboxIframes(div) {
  div.querySelectorAll('iframe').forEach(iframe => {
    iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-popups');
    iframe.loading = 'lazy';
  });
}

function sanitizeStyles(div) {
  div.querySelectorAll('[style]').forEach(el => {
    const raw = el.getAttribute('style');
    const safe = raw.split(';').map(s => s.trim()).filter(s => {
      const prop = s.split(':')[0]?.trim().toLowerCase();
      return prop && ALLOWED_CSS_PROPS.has(prop);
    }).join('; ');
    if (safe) {
      el.setAttribute('style', safe);
    } else {
      el.removeAttribute('style');
    }
  });
}

// Tags allowed through to DOMPurify (must match sanitizeContent's ALLOWED_TAGS)
const _ALLOWED_HTML = new Set(['p','br','strong','b','em','i','u','s','del','strike','h1','h2','h3','h4','h5','h6','ul','ol','li','blockquote','pre','code','a','img','iframe','table','thead','tbody','tr','th','td','hr','div','span','sub','sup','center']);

function renderHiveBody(raw) {
  const md = normalizeMarkdown(raw);
  let html = marked.parse(md, { breaks: true, gfm: true });
  // Add slugified IDs to headings for anchor links
  html = html.replace(/<h([1-6])>(.*?)<\/h\1>/gi, (m, level, inner) => {
    const tmp = document.createElement('span');
    tmp.innerHTML = inner.replace(/<[^>]+>/g, '');
    const slug = (tmp.textContent || '').toLowerCase().trim()
      .replace(/[^\w\s-]/g, '').replace(/\s/g, '-');
    return `<h${level} id="${slug}">${inner}</h${level}>`;
  });
  // Strip non-allowed HTML tags at string level before DOM parsing.
  html = html.replace(/<\/?([a-z][a-z0-9]*)\b[^>]*>/gi, (m, tag) =>
    _ALLOWED_HTML.has(tag.toLowerCase()) ? m : '');
  // Close unclosed <iframe> tags so they can't swallow subsequent content
  html = html.replace(/<iframe\b[^>]*>/gi, '$&</iframe>');
  const clean = sanitizeContent(html);
  const div = document.createElement('div');
  div.innerHTML = clean;
  sandboxIframes(div);
  sanitizeStyles(div);
  proxyImages(div);
  embedVideos(div);
  return div.innerHTML;
}
