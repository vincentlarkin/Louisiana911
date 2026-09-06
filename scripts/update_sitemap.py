"""Regenerate the sitemap from indexable canonical HTML pages and Git dates.

Run after content changes, before committing. No database or network access.
"""
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = 'http://www.sitemaps.org/schemas/sitemap/0.9'


class PageMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonical = ''
        self.noindex = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'link' and attrs.get('rel') == 'canonical':
            self.canonical = attrs.get('href', '')
        if tag == 'meta' and attrs.get('name') == 'robots':
            self.noindex = 'noindex' in attrs.get('content', '')


def indexable_pages():
    pages = {}
    for path in sorted((ROOT / 'public').glob('*.html')):
        metadata = PageMetadata()
        metadata.feed(path.read_text(encoding='utf-8'))
        url = metadata.canonical
        if metadata.noindex or not url.startswith('https://louisiana911.com/'):
            continue
        if url in pages:
            raise ValueError(f'Duplicate canonical URL: {url}')
        pages[url] = path
    return dict(sorted(pages.items()))


def last_modified(path):
    relative = path.relative_to(ROOT).as_posix()
    dirty = subprocess.check_output(
        ['git', 'status', '--porcelain', '--', relative], cwd=ROOT, text=True
    ).strip()
    previous = subprocess.run(['git', 'show', f'HEAD:{relative}'], cwd=ROOT,
                              text=True, encoding='utf-8', capture_output=True)
    # Bumping the tracker URL alone is not a significant page update.
    without_tracker_version = lambda text: re.sub(r'/analytics\.js\?v=[\d.]+', '/analytics.js', text)
    content_changed = previous.returncode != 0 or (
        without_tracker_version(previous.stdout) != without_tracker_version(path.read_text(encoding='utf-8'))
    )
    if dirty and content_changed:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date().isoformat()
    return subprocess.check_output(
        ['git', 'log', '-1', '--format=%cs', '--', relative], cwd=ROOT, text=True
    ).strip()


def main():
    ET.register_namespace('', NAMESPACE)
    root = ET.Element(f'{{{NAMESPACE}}}urlset')
    for url, path in indexable_pages().items():
        entry = ET.SubElement(root, 'url')
        ET.SubElement(entry, 'loc').text = url
        modified = last_modified(path)
        if modified:
            ET.SubElement(entry, 'lastmod').text = modified
    ET.indent(root, space='  ')
    ET.ElementTree(root).write(ROOT / 'public/sitemap.xml', encoding='utf-8', xml_declaration=True)
    print(f'Updated sitemap: {len(root)} canonical pages')


if __name__ == '__main__':
    main()
