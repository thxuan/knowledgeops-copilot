"""Check local documentation integrity without starting project services."""
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlsplit, unquote
import hashlib
import json

ROOT = Path(__file__).resolve().parent

class Page(HTMLParser):
    def __init__(self, path):
        super().__init__()
        self.links, self.ids, self.duplicate_ids = [], set(), []
        self.h1 = self.title = 0
        self.feed(path.read_text(encoding='utf-8'))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        ident = attrs.get('id')
        if ident:
            if ident in self.ids:
                self.duplicate_ids.append(ident)
            self.ids.add(ident)
        self.h1 += tag == 'h1'
        self.title += tag == 'title'
        for attr in ('href','src'):
            if attrs.get(attr):
                self.links.append(attrs[attr])

pages = {p.resolve(): Page(p) for p in ROOT.rglob('*.html')}
errors = []
links = 0
for path, page in pages.items():
    if page.h1 != 1 or page.title != 1:
        errors.append(f'{path.name}: h1={page.h1}, title={page.title}')
    if page.duplicate_ids:
        errors.append(f'{path.name}: duplicate IDs {page.duplicate_ids}')
    for link in page.links:
        parts = urlsplit(link)
        if parts.scheme or parts.netloc:
            errors.append(f'Unexpected external dependency/link: {link}')
            continue
        links += 1
        target = (path.parent / unquote(parts.path)).resolve() if parts.path else path
        if not target.exists():
            errors.append(f'{path.name}: missing {link}')
        elif parts.fragment and target in pages and unquote(parts.fragment) not in pages[target].ids:
            errors.append(f'{path.name}: missing anchor {link}')

manifest = json.loads((ROOT / 'source-manifest.json').read_text(encoding='utf-8'))
for row in manifest['files']:
    source = ROOT.parents[1] / row['path']
    if hashlib.sha256(source.read_bytes()).hexdigest() != row['sha256']:
        errors.append(f'Stale source snapshot: {row["path"]}')

if errors:
    raise SystemExit('\n'.join(errors))
print(json.dumps(dict(html_pages=len(pages), checked_links=links,
    verified_source_files=len(manifest['files']), errors=0),ensure_ascii=False))
